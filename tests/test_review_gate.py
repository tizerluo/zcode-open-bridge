"""
test_review_gate.py — zcode-review-gate 组件单测 (全 fake)

subprocess.run 与 urllib.request.urlopen 一律 mock 掉: 不碰真实网络、
真实 git、真实 ~/.config / ~/.local。state/clone_root 用临时目录。

覆盖:
  - token 级联 (env 优先 / gh fallback / 全灭 None)
  - pulls 分页拼装 + per_page/page 参数 + Authorization 头
  - 错误分类 (403 限流 → RateLimited / 404 → RepoHardError / 5xx → RetryableError)
  - 去重状态机 (同 sha 跳过 / 新 sha 重审 / 退避未到跳过 / gave_up 跳过 / 新 sha 复活)
  - 退避数学 min(base*2**(attempts-1), max) + 超限 gave_up
  - verdict 解析 (三段齐 / 缺一段 None / p0>0 concerns / 全 0 pass / None concerns)
  - 评论 body 超 max_body 截断且含标注
  - state 原子写 (os.replace 被调) + 损坏恢复 + 往返
  - 配置默认值 + env 覆盖
  - git 命令带 token 时含 http.extraHeader、无 token 不含
  - --once 全链路 (1 个 open PR 新 sha → clone/fetch/审查/评论/state 落盘;
    第二轮同 sha 不重复审不重复评论; 第三轮新 sha 重审且 attempts 清零)

运行: python3 tests/test_review_gate.py
依赖: 仅 Python 标准库
"""

import email.message
import json
import os
import re
import shutil
import tempfile
import types
import unittest
import urllib.error
from unittest import mock

GATE_PATH = os.path.join(
    os.path.dirname(__file__), "..", "packages", "review-gate", "zcode-review-gate"
)


def _load_gate_module():
    """exec 加载无后缀组件 (跳过 __main__ 尾巴), 与 test_security_review 同惯例。"""
    mod = types.ModuleType("zcode_review_gate")
    mod.__file__ = GATE_PATH
    with open(GATE_PATH) as f:
        code = f.read()
    code_no_main = code.split('if __name__ == "__main__":')[0]
    exec(code_no_main, mod.__dict__)
    return mod


class _CP:
    """假 subprocess.CompletedProcess"""

    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _FakeResp:
    """假 urlopen 响应: 支持 with 语境 + read()"""

    def __init__(self, payload):
        self._raw = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _EnvGuard(unittest.TestCase):
    """保存/恢复本文件用到的环境变量 (与 test_security_review 同惯例)。"""

    ENV_KEYS = ("GITHUB_TOKEN", "GH_TOKEN", "GATE_CONFIG", "GATE_STATE_FILE",
                "GATE_CLONE_ROOT", "GATE_MCP_SERVER", "GATE_POLL_INTERVAL")

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in self.ENV_KEYS}

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class _GateCase(_EnvGuard):
    """公共基座: 加载模块 + 临时目录。"""

    def setUp(self):
        super().setUp()
        self.mod = _load_gate_module()
        self.tmp = tempfile.mkdtemp(prefix="zrg-test-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        super().tearDown()


# ============================================================
# token 级联
# ============================================================
class TestTokenCascade(_GateCase):
    def setUp(self):
        super().setUp()
        os.environ.pop("GITHUB_TOKEN", None)
        os.environ.pop("GH_TOKEN", None)

    def test_github_token_priority(self):
        os.environ["GITHUB_TOKEN"] = "t1"
        os.environ["GH_TOKEN"] = "t2"
        self.assertEqual(self.mod.resolve_token(), "t1")

    def test_gh_token_fallback(self):
        os.environ["GH_TOKEN"] = "t2"
        self.assertEqual(self.mod.resolve_token(), "t2")

    def test_gh_cli_fallback(self):
        def fake_run(cmd, *a, **kw):
            self.assertEqual(cmd[:2], ["gh", "auth"])
            return _CP(returncode=0, stdout="ghp_xyz\n", stderr="")
        with mock.patch.object(self.mod.subprocess, "run", fake_run):
            self.assertEqual(self.mod.resolve_token(), "ghp_xyz")

    def test_all_missing_returns_none(self):
        def fake_run(cmd, *a, **kw):
            raise FileNotFoundError("gh: command not found")
        with mock.patch.object(self.mod.subprocess, "run", fake_run):
            self.assertIsNone(self.mod.resolve_token())


# ============================================================
# GitHub API: 分页 / 错误分类 / 头
# ============================================================
class TestGithubApi(_GateCase):
    def test_pagination_and_params(self):
        urls, seen_headers = [], []

        def fake_urlopen(req, timeout=None, **kw):
            urls.append(req.full_url)
            seen_headers.append(req.get_header("Authorization"))
            page = int(re.search(r"[?&]page=(\d+)", req.full_url).group(1))
            payload = {1: [{"number": i} for i in range(100)],
                       2: [{"number": 100}]}.get(page, [])
            return _FakeResp(payload)

        with mock.patch.object(self.mod.urllib.request, "urlopen", fake_urlopen):
            prs = self.mod.list_open_prs("tok", "https://api.github.com", "o", "r")
        self.assertEqual(len(prs), 101)
        self.assertEqual(len(urls), 2)
        self.assertIn("state=open", urls[0])
        self.assertIn("per_page=100", urls[0])
        self.assertIn("page=1", urls[0])
        self.assertIn("page=2", urls[1])
        self.assertEqual(seen_headers[0], "Bearer tok")

    def _raise_http(self, code, headers=None):
        hdrs = email.message.Message()
        for k, v in (headers or {}).items():
            hdrs[k] = v
        return urllib.error.HTTPError("https://api.github.com/x", code,
                                      "err", hdrs, None)

    def test_rate_limited_detected(self):
        err = self._raise_http(403, {"X-RateLimit-Remaining": "0",
                                     "X-RateLimit-Reset": "1893456000"})

        def fake_urlopen(req, timeout=None, **kw):
            raise err

        with mock.patch.object(self.mod.urllib.request, "urlopen", fake_urlopen):
            with self.assertRaises(self.mod.RateLimited) as cm:
                self.mod.list_open_prs("tok", "https://api.github.com", "o", "r")
        self.assertEqual(cm.exception.reset_at, 1893456000)

    def test_404_is_repo_hard_error(self):
        err = self._raise_http(404)

        def fake_urlopen(req, timeout=None, **kw):
            raise err

        with mock.patch.object(self.mod.urllib.request, "urlopen", fake_urlopen):
            with self.assertRaises(self.mod.RepoHardError):
                self.mod.list_open_prs("tok", "https://api.github.com", "o", "r")

    def test_5xx_is_retryable(self):
        err = self._raise_http(502)

        def fake_urlopen(req, timeout=None, **kw):
            raise err

        with mock.patch.object(self.mod.urllib.request, "urlopen", fake_urlopen):
            with self.assertRaises(self.mod.RetryableError):
                self.mod.list_open_prs("tok", "https://api.github.com", "o", "r")

    def test_network_error_is_retryable(self):
        def fake_urlopen(req, timeout=None, **kw):
            raise urllib.error.URLError("connection refused")

        with mock.patch.object(self.mod.urllib.request, "urlopen", fake_urlopen):
            with self.assertRaises(self.mod.RetryableError):
                self.mod.list_open_prs("tok", "https://api.github.com", "o", "r")


# ============================================================
# 去重状态机
# ============================================================
class TestNeedsReview(_GateCase):
    NOW = 1_000_000.0

    def test_first_seen(self):
        self.assertTrue(self.mod.needs_review(None, "sha1", self.NOW))

    def test_same_sha_reviewed_skips(self):
        e = {"head_sha": "sha1", "status": "reviewed"}
        self.assertFalse(self.mod.needs_review(e, "sha1", self.NOW))

    def test_new_sha_rereviews(self):
        e = {"head_sha": "sha1", "status": "reviewed"}
        self.assertTrue(self.mod.needs_review(e, "sha2", self.NOW))

    def test_failed_backoff_not_due_skips(self):
        e = {"head_sha": "sha1", "status": "failed",
             "next_retry_at": self.NOW + 100}
        self.assertFalse(self.mod.needs_review(e, "sha1", self.NOW))

    def test_failed_backoff_due_reviews(self):
        e = {"head_sha": "sha1", "status": "failed",
             "next_retry_at": self.NOW - 1}
        self.assertTrue(self.mod.needs_review(e, "sha1", self.NOW))

    def test_gave_up_skips_same_sha(self):
        e = {"head_sha": "sha1", "status": "gave_up"}
        self.assertFalse(self.mod.needs_review(e, "sha1", self.NOW))

    def test_gave_up_revives_on_new_sha(self):
        e = {"head_sha": "sha1", "status": "gave_up", "attempts": 5}
        self.assertTrue(self.mod.needs_review(e, "sha2", self.NOW))


# ============================================================
# 退避数学
# ============================================================
class TestBackoff(_GateCase):
    def _cfg(self, **retry):
        base = {"base_seconds": 300, "max_seconds": 3600, "max_attempts": 5}
        base.update(retry)
        return self.mod.GateConfig({"retry": base})

    def test_backoff_sequence(self):
        cfg = self._cfg()
        now = 1_000_000.0
        entry = {"attempts": 0}
        for i, delay in enumerate([300, 600, 1200, 2400], start=1):
            self.mod.mark_failure(entry, "err", now, cfg)
            self.assertEqual(entry["attempts"], i)
            self.assertEqual(entry["status"], "failed")
            self.assertEqual(entry["next_retry_at"] - now, delay)

    def test_gave_up_at_max_attempts(self):
        cfg = self._cfg()
        entry = {"attempts": 4}
        self.mod.mark_failure(entry, "err", 0.0, cfg)
        self.assertEqual(entry["status"], "gave_up")
        self.assertEqual(entry["attempts"], 5)
        self.assertEqual(entry["next_retry_at"], 0.0)

    def test_backoff_capped_at_max(self):
        cfg = self._cfg(max_seconds=500, max_attempts=9)
        entry = {"attempts": 2}
        self.mod.mark_failure(entry, "err", 0.0, cfg)
        # 300 * 2**2 = 1200 → 封顶 500
        self.assertEqual(entry["next_retry_at"], 500.0)


# ============================================================
# verdict 解析
# ============================================================
class TestVerdict(_GateCase):
    def test_parse_all_three(self):
        text = "汇总: P0: 1 条, P1: 2 条, P2: 3 条\n详情..."
        self.assertEqual(self.mod.parse_severity_counts(text), (1, 2, 3))

    def test_parse_cn_format(self):
        text = "P0 × 0 · P1 × 0 · P2 × 12"
        self.assertEqual(self.mod.parse_severity_counts(text), (0, 0, 12))

    def test_parse_missing_one_returns_none(self):
        text = "P0: 0 条, P1: 1 条"  # 缺 P2
        self.assertIsNone(self.mod.parse_severity_counts(text))

    def test_parse_only_scans_head(self):
        # 分布信息在 3000 字符以外 → 视为缺失 (防正文偶然命中)
        text = "没有分布" + "x" * 4000 + "P0: 9 P1: 9 P2: 9"
        self.assertIsNone(self.mod.parse_severity_counts(text))

    def test_verdict_p0_concerns(self):
        self.assertEqual(self.mod.verdict_from_counts((1, 0, 0)), "concerns")

    def test_verdict_p1_concerns(self):
        self.assertEqual(self.mod.verdict_from_counts((0, 2, 0)), "concerns")

    def test_verdict_all_zero_pass(self):
        self.assertEqual(self.mod.verdict_from_counts((0, 0, 5)), "pass")

    def test_verdict_none_concerns(self):
        self.assertEqual(self.mod.verdict_from_counts(None), "concerns")


# ============================================================
# 评论 body
# ============================================================
class TestCommentBody(_GateCase):
    SHA = "abcdef1234567890" + "0" * 24

    def test_pass_body(self):
        body = self.mod.build_comment_body(
            "pass", (0, 0, 1), self.SHA, "报告正文", 60000)
        self.assertIn("✅ pass", body)
        self.assertIn("`abcdef123456`", body)
        self.assertIn("P0 × 0 · P1 × 0 · P2 × 1", body)
        self.assertIn("未发现阻断问题，可以合并", body)
        self.assertIn("报告正文", body)
        self.assertIn("zcode_pr_review", body)
        self.assertNotIn("截断", body)

    def test_concerns_parse_failure_body(self):
        body = self.mod.build_comment_body(
            "concerns", None, self.SHA, "r", 60000)
        self.assertIn("⚠️ concerns", body)
        self.assertIn("解析失败", body)
        self.assertIn("合并前请处理", body)

    def test_truncation_over_max_body(self):
        report = "报" * 100000
        body = self.mod.build_comment_body(
            "concerns", (1, 0, 0), self.SHA, report, 60000)
        self.assertLessEqual(len(body), 60000)
        self.assertIn("报告超长已截断", body)
        # 头尾结构完整 (只截报告正文)
        self.assertIn("## ZCode Review Gate", body)
        self.assertIn("同一 head 不重复审", body)


# ============================================================
# state 文件
# ============================================================
class TestStateStore(_GateCase):
    def test_atomic_save_uses_os_replace(self):
        path = os.path.join(self.tmp, "sub", "state.json")
        store = self.mod.StateStore(path)
        store.put("o/r#1", {"head_sha": "x", "status": "reviewed"})
        with mock.patch.object(self.mod.os, "replace",
                               wraps=os.replace) as m_replace:
            store.save()
        m_replace.assert_called_once()
        with open(path) as f:
            data = json.load(f)
        self.assertEqual(data["prs"]["o/r#1"]["head_sha"], "x")
        self.assertFalse(os.path.exists(path + ".tmp"))  # tmp 不残留

    def test_corrupt_state_starts_fresh(self):
        path = os.path.join(self.tmp, "state.json")
        with open(path, "w") as f:
            f.write("{not json")
        store = self.mod.StateStore(path)
        self.assertEqual(store.data, {"prs": {}})

    def test_roundtrip(self):
        path = os.path.join(self.tmp, "state.json")
        s1 = self.mod.StateStore(path)
        s1.put("o/r#2", {"head_sha": "y"})
        s1.save()
        s2 = self.mod.StateStore(path)
        self.assertEqual(s2.get("o/r#2")["head_sha"], "y")


# ============================================================
# 配置
# ============================================================
class TestConfig(_GateCase):
    def test_defaults(self):
        cfg = self.mod.GateConfig({})
        self.assertEqual(cfg.poll_interval, 300)
        self.assertEqual(cfg.review_depth, "deep")
        self.assertEqual(cfg.retry_base, 300)
        self.assertEqual(cfg.retry_max_attempts, 5)
        self.assertTrue(cfg.comment_enabled)
        self.assertEqual(cfg.comment_max_body, 60000)
        self.assertTrue(
            cfg.state_file.endswith(".local/state/zcode-review-gate/state.json"))
        self.assertEqual(cfg.mcp_server, "zcode-mcp-server")

    def test_env_overrides(self):
        os.environ["GATE_STATE_FILE"] = "/tmp/x/state.json"
        os.environ["GATE_POLL_INTERVAL"] = "42"
        os.environ["GATE_MCP_SERVER"] = "/opt/mcp"
        cfg = self.mod.GateConfig({})
        cfg.apply_env_overrides()
        self.assertEqual(cfg.state_file, "/tmp/x/state.json")
        self.assertEqual(cfg.poll_interval, 42)
        self.assertEqual(cfg.mcp_server, "/opt/mcp")

    def test_bad_depth_falls_back(self):
        cfg = self.mod.GateConfig({"review": {"depth": "ultra"}})
        self.assertEqual(cfg.review_depth, "deep")


# ============================================================
# git token 注入
# ============================================================
class TestGitTokenHeader(_GateCase):
    def _capture(self, token):
        cmds = []

        def fake_run(cmd, *a, **kw):
            cmds.append(list(cmd))
            return _CP(returncode=0, stdout="ok", stderr="")

        with mock.patch.object(self.mod.subprocess, "run", fake_run):
            self.mod.git(["fetch", "origin"], token=token)
        return cmds[0]

    def test_token_injected_via_extraheader(self):
        cmd = self._capture("secret-tok")
        idx = cmd.index("-c")
        self.assertEqual(cmd[idx + 1],
                         "http.extraHeader=Authorization: Bearer secret-tok")

    def test_no_token_no_header(self):
        cmd = self._capture(None)
        self.assertNotIn("-c", cmd)
        self.assertNotIn("http.extraHeader", " ".join(cmd))


# ============================================================
# --once 全链路 (全 fake)
# ============================================================
class TestOnceEndToEnd(_GateCase):
    def setUp(self):
        super().setUp()
        os.environ["GITHUB_TOKEN"] = "fake-token-123"
        os.environ.pop("GH_TOKEN", None)
        self.pr_sha = "a" * 40
        self.api_calls = []    # (method, url, body)
        self.mcp_calls = []    # 审查 args dict
        self.git_calls = []    # git cmd list

    def _pr_payload(self):
        return [{"number": 5, "title": "test pr",
                 "head": {"sha": self.pr_sha}, "base": {"ref": "main"}}]

    def _fake_urlopen(self, req, timeout=None, **kw):
        url = req.full_url
        method = req.get_method()
        body = json.loads(req.data.decode("utf-8")) if req.data else None
        self.api_calls.append((method, url, body))
        if "/pulls?" in url:
            return _FakeResp(self._pr_payload())
        if url.endswith("/comments"):
            return _FakeResp(
                {"html_url": "https://github.com/octo/hello#issuecomment-1"})
        raise AssertionError(f"未预期 URL: {url}")

    def _fake_run(self, cmd, *a, **kw):
        if cmd[0] == "git":
            self.git_calls.append(list(cmd))
            if "clone" in cmd:
                # 假 clone 也要建出 .git, 否则第二轮会重复 clone
                os.makedirs(os.path.join(cmd[-1], ".git"), exist_ok=True)
            return _CP(returncode=0, stdout="", stderr="")
        if "--call" in cmd:
            idx = cmd.index("--call")
            self.assertEqual(cmd[idx + 1], "zcode_pr_review")
            self.mcp_calls.append(json.loads(cmd[idx + 2]))
            report = "汇总: P0: 0 条, P1: 0 条, P2: 2 条\n一切正常。"
            payload = {"ok": True, "result": {
                "content": [{"type": "text", "text": report}]}}
            return _CP(returncode=0, stdout=json.dumps(payload), stderr="")
        raise AssertionError(f"未预期命令: {cmd}")

    def _write_config(self):
        cfg = {"repos": ["octo/hello"],
               "state_file": os.path.join(self.tmp, "state.json"),
               "clone_root": os.path.join(self.tmp, "clones"),
               "review": {"depth": "deep", "focus": ""},
               "retry": {"base_seconds": 300, "max_seconds": 3600,
                         "max_attempts": 5},
               "comment": {"enabled": True, "max_body": 60000}}
        path = os.path.join(self.tmp, "config.json")
        with open(path, "w") as f:
            json.dump(cfg, f)
        return path

    def _run_once(self, cfg_path):
        with mock.patch.object(self.mod.subprocess, "run", self._fake_run), \
             mock.patch.object(self.mod.urllib.request, "urlopen",
                               self._fake_urlopen):
            return self.mod.main(["--once", "--config", cfg_path])

    def _state_entry(self):
        with open(os.path.join(self.tmp, "state.json")) as f:
            return json.load(f)["prs"]["octo/hello#5"]

    def test_full_cycle_dedup_and_new_sha(self):
        cfg_path = self._write_config()

        # 第一轮: 新 sha → clone/fetch/审查/评论/state 落盘
        self.assertEqual(self._run_once(cfg_path), 0)
        self.assertEqual(len(self.mcp_calls), 1)
        posts = [c for c in self.api_calls if c[0] == "POST"]
        self.assertEqual(len(posts), 1)
        self.assertIn("/repos/octo/hello/issues/5/comments", posts[0][1])

        # 审查参数: base 带 origin/ 前缀, head 为 sha, depth 透传
        call_args = self.mcp_calls[0]
        self.assertEqual(call_args["base"], "origin/main")
        self.assertEqual(call_args["head"], "a" * 40)
        self.assertEqual(call_args["depth"], "deep")
        self.assertTrue(call_args["path"].endswith("octo__hello"))

        # 评论体: pass + 严重度分布 + 署名
        body = posts[0][2]["body"]
        self.assertIn("✅ pass", body)
        self.assertIn("P0 × 0 · P1 × 0 · P2 × 2", body)
        self.assertIn("同一 head 不重复审", body)

        # git: clone 的 URL 不含 token; token 只走 -c http.extraHeader
        clone_cmd = next(c for c in self.git_calls if "clone" in c)
        url_arg = next(a for a in clone_cmd if a.startswith("https://"))
        self.assertNotIn("fake-token-123", url_arg)
        self.assertIn("http.extraHeader=Authorization: Bearer fake-token-123",
                      " ".join(clone_cmd))

        # state 落盘: reviewed/pass
        entry = self._state_entry()
        self.assertEqual(entry["status"], "reviewed")
        self.assertEqual(entry["verdict"], "pass")
        self.assertEqual(entry["head_sha"], "a" * 40)
        self.assertEqual(entry["attempts"], 0)
        self.assertTrue(entry["comment_url"])

        # 第二轮同 sha: 不重复审查、不重复评论
        self.assertEqual(self._run_once(cfg_path), 0)
        self.assertEqual(len(self.mcp_calls), 1)
        self.assertEqual(len([c for c in self.api_calls if c[0] == "POST"]), 1)

        # 第三轮新 sha: 重审 + 重评论, attempts 从 0 重新计
        self.pr_sha = "b" * 40
        self.assertEqual(self._run_once(cfg_path), 0)
        self.assertEqual(len(self.mcp_calls), 2)
        self.assertEqual(len([c for c in self.api_calls if c[0] == "POST"]), 2)
        entry = self._state_entry()
        self.assertEqual(entry["head_sha"], "b" * 40)
        self.assertEqual(entry["attempts"], 0)
        self.assertEqual(entry["status"], "reviewed")

    def test_review_failure_goes_backoff_then_skip(self):
        """审查失败 → failed + 退避; 第二轮退避未到 → 跳过不再调审查"""
        cfg_path = self._write_config()
        fail_payload = {"ok": False, "result": {
            "content": [{"type": "text", "text": "炸了"}], "isError": True}}

        def failing_run(cmd, *a, **kw):
            if "--call" in cmd:
                return _CP(returncode=2, stdout=json.dumps(fail_payload),
                           stderr="")
            return self._fake_run(cmd, *a, **kw)

        with mock.patch.object(self.mod.subprocess, "run", failing_run), \
             mock.patch.object(self.mod.urllib.request, "urlopen",
                               self._fake_urlopen):
            self.assertEqual(self.mod.main(["--once", "--config", cfg_path]), 0)
            entry = self._state_entry()
            self.assertEqual(entry["status"], "failed")
            self.assertEqual(entry["attempts"], 1)
            self.assertGreater(entry["next_retry_at"], 0)
            # 评论不应发出 (审查就失败了)
            self.assertEqual(
                len([c for c in self.api_calls if c[0] == "POST"]), 0)

            # 第二轮: 退避未到, 不再调审查 (git/API 还会照常轮询)
            self.assertEqual(self.mod.main(["--once", "--config", cfg_path]), 0)
            entry = self._state_entry()
            self.assertEqual(entry["attempts"], 1)  # 没增加


if __name__ == "__main__":
    unittest.main()
