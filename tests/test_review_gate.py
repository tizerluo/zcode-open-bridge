"""
test_review_gate.py — zcode-review-gate 组件单测 (全 fake)

subprocess.run 与 urllib.request.urlopen 一律 mock 掉: 不碰真实网络、
真实 git、真实 ~/.config / ~/.local。state/clone_root 用临时目录。

覆盖:
  - token 级联 (env 优先 / gh fallback / 全灭 None)
  - pulls 分页拼装 + per_page/page 参数 + Authorization 头
  - 错误分类 (403+remaining=0 / 429 / 403+Retry-After → RateLimited;
    404 → RepoHardError; 5xx/网络/401/422 → RetryableError)
  - 去重状态机 (同 sha 跳过 / 新 sha 重审 / failed 退避 / comment_failed 退避
    / gave_up 跳过 / 新 sha 复活)
  - 退避数学 min(base*2**(attempts-1), max) + 超限 gave_up + comment_failed 保 verdict
  - verdict 解析 (issue #16: zob-verdict 结构化标记优先 / 正文正则兜底;
    三段齐 / 缺一段 None / 枚举格式 None / 无关数字 fail-safe 钉住
    / p0>0 concerns / 全 0 pass / None unresolved"需人工核对")
  - 评论 body 超 max_body 截断 + max_body 钳下限
  - state 原子写 (os.replace 被调) + 损坏恢复 + 往返
  - 配置默认值 + env 覆盖 + 下限钳制
  - git: token 经 GIT_CONFIG_* env 注入且 argv 无 token / GitError 不带 token
  - ensure_clone: 半成品重建 / clone 失败清理 / web 宿主推导
  - run_review: 坏 JSON / 空报告 / OSError / TimeoutExpired / 超时透传 / stderr 尾部
  - mcp_server 解析: PATH 命中 / ~/.local/bin 回退 / 不可执行不用 / 原样兜底
  - --once 全链路 (新 sha → 审+评+落盘; 同 sha 不重复; 新 sha 重审 attempts 清零)
  - 评论失败两路径: RetryableError → comment_failed+退避+缓存, 下轮只补评论;
    RateLimited → 不烧 attempts 有缓存, 下轮只补评论
  - CLI: --repo 覆盖配置 repos; --pr 过滤生效; 有 PR 失败 --once 仍 exit 0

运行: python3 tests/test_review_gate.py
依赖: 仅 Python 标准库
"""

import base64
import email.message
import json
import os
import re
import shutil
import subprocess
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


def _make_http_error(code, headers=None):
    hdrs = email.message.Message()
    for k, v in (headers or {}).items():
        hdrs[k] = v
    return urllib.error.HTTPError("https://api.github.com/x", code,
                                  "err", hdrs, None)


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

    def _assert_raises(self, err, exc_type):
        def fake_urlopen(req, timeout=None, **kw):
            raise err
        with mock.patch.object(self.mod.urllib.request, "urlopen", fake_urlopen):
            with self.assertRaises(exc_type):
                self.mod.list_open_prs("tok", "https://api.github.com", "o", "r")

    def test_rate_limited_403_remaining_0(self):
        err = _make_http_error(403, {"X-RateLimit-Remaining": "0",
                                     "X-RateLimit-Reset": "1893456000"})
        def fake_urlopen(req, timeout=None, **kw):
            raise err
        with mock.patch.object(self.mod.urllib.request, "urlopen", fake_urlopen):
            with self.assertRaises(self.mod.RateLimited) as cm:
                self.mod.list_open_prs("tok", "https://api.github.com", "o", "r")
        self.assertEqual(cm.exception.reset_at, 1893456000)

    def test_rate_limited_429_bare(self):
        self._assert_raises(_make_http_error(429), self.mod.RateLimited)

    def test_rate_limited_403_retry_after(self):
        err = _make_http_error(403, {"Retry-After": "120"})
        def fake_urlopen(req, timeout=None, **kw):
            raise err
        import time as _time
        before = int(_time.time())
        with mock.patch.object(self.mod.urllib.request, "urlopen", fake_urlopen):
            with self.assertRaises(self.mod.RateLimited) as cm:
                self.mod.list_open_prs("tok", "https://api.github.com", "o", "r")
        # Retry-After 是相对秒, reset 应落在 now+120 附近
        self.assertGreaterEqual(cm.exception.reset_at, before + 119)
        self.assertLessEqual(cm.exception.reset_at, before + 125)

    def test_404_is_repo_hard_error(self):
        self._assert_raises(_make_http_error(404), self.mod.RepoHardError)

    def test_5xx_is_retryable(self):
        self._assert_raises(_make_http_error(502), self.mod.RetryableError)

    def test_401_422_are_retryable(self):
        # 非限流 4xx: 重试多半也失败, 但 gave_up 兜底, 方向 fail-safe
        self._assert_raises(_make_http_error(401), self.mod.RetryableError)
        self._assert_raises(_make_http_error(422), self.mod.RetryableError)

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

    def test_comment_failed_backoff_not_due_skips(self):
        e = {"head_sha": "sha1", "status": "comment_failed",
             "next_retry_at": self.NOW + 100}
        self.assertFalse(self.mod.needs_review(e, "sha1", self.NOW))

    def test_comment_failed_due_retries(self):
        e = {"head_sha": "sha1", "status": "comment_failed",
             "next_retry_at": 0.0}
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

    def test_comment_failed_status_and_verdict_kept(self):
        # 评论失败: status=comment_failed, 缓存的 verdict 不被清 (重试要用)
        cfg = self._cfg()
        entry = {"attempts": 0, "verdict": "pass", "report": "r"}
        self.mod.mark_failure(entry, "err", 0.0, cfg, status="comment_failed")
        self.assertEqual(entry["status"], "comment_failed")
        self.assertEqual(entry["verdict"], "pass")
        # 审查链路失败则清 verdict (旧结论不再可信)
        entry2 = {"attempts": 0, "verdict": "pass"}
        self.mod.mark_failure(entry2, "err", 0.0, cfg)
        self.assertIsNone(entry2["verdict"])


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

    def test_parse_enumeration_format_returns_none(self):
        # "P0/P1/P2 各 0/0/2" 枚举格式: P0 后紧跟 "/P1", 不排除 P 会把
        # P1 的数字算到 P0 头上 — 统一解析失败回 None (走人工核对标注),
        # 也不拿错数字
        text = "汇总: P0/P1/P2 各 0/0/2 条, findings 确认 1 条"
        self.assertIsNone(self.mod.parse_severity_counts(text))
        # 对应的 verdict 走向: None → unresolved (issue #16: 不再误标 concerns)
        self.assertEqual(self.mod.verdict_from_counts(None), "unresolved")

    def test_parse_unrelated_number_fail_safe(self):
        # "P0 级问题参见 2024 年报": 无关数字仍会被当作计数 (2024),
        # 钉住该行为 — 这是有意取舍: 解析到异常大数 → concerns,
        # 方向 fail-safe (宁误拦, 不漏放), 人工看评论即可分辨
        text = "P0 级问题参见 2024 年报; P1: 0 条; P2: 1 条"
        self.assertEqual(self.mod.parse_severity_counts(text), (2024, 0, 1))
        self.assertEqual(
            self.mod.verdict_from_counts(
                self.mod.parse_severity_counts(text)), "concerns")

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

    def test_verdict_none_unresolved(self):
        # issue #16: 解析失败 → "需人工核对" 而非 concerns — 假红灯曾致
        # 下游指挥 agent 看到表头 P0×2 停工等人工, 实际正文判定可合并
        self.assertEqual(self.mod.verdict_from_counts(None), "unresolved")

    def test_marker_priority_over_prose(self):
        # issue #16: zob-verdict 结构化标记优先 — 正文有误导性计数也不采信
        text = ("汇总: P0: 2 条, P1: 2 条, P2: 3 条\n详情...\n"
                '<!-- zob-verdict:{"P0":0,"P1":0,"P2":5,"merge":true} -->')
        self.assertEqual(self.mod.parse_severity_counts(text), (0, 0, 5))

    def test_marker_malformed_falls_back_to_prose(self):
        # 标记残缺 (缺 P1/P2/merge 字段) → 不匹配, 退回正文正则
        text = '汇总: P0: 1 条, P1: 2 条, P2: 3 条\nzob-verdict:{"P0":9}'
        self.assertEqual(self.mod.parse_severity_counts(text), (1, 2, 3))

    def test_marker_only_no_prose_summary(self):
        # 正文无 prose 汇总, 仅靠标记也能解析 (对报告格式变化免疫)
        text = ('逐条详述...\n'
                '<!-- zob-verdict:{"P0":1,"P1":0,"P2":2,"merge":false} -->')
        self.assertEqual(self.mod.parse_severity_counts(text), (1, 0, 2))


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

    def test_unresolved_parse_failure_body(self):
        # issue #16: 解析失败 → ❓ 需人工核对, 不显示 concerns 假红灯
        body = self.mod.build_comment_body(
            "unresolved", None, self.SHA, "r", 60000)
        self.assertIn("❓ 需人工核对", body)
        self.assertIn("解析失败", body)
        self.assertIn("请人工核对报告正文", body)
        self.assertNotIn("concerns", body)

    def test_truncation_over_max_body(self):
        report = "报" * 100000
        body = self.mod.build_comment_body(
            "concerns", (1, 0, 0), self.SHA, report, 60000)
        self.assertLessEqual(len(body), 60000)
        self.assertIn("报告超长已截断", body)
        # 头尾结构完整 (只截报告正文)
        self.assertIn("## ZCode Review Gate", body)
        self.assertIn("同一 head 不重复审", body)

    def test_max_body_clamped_to_floor(self):
        # 极端配置 max_body=10 (< 模板开销) → 钳到 2000, 不至于截出残破 markdown
        body = self.mod.build_comment_body(
            "pass", (0, 0, 0), self.SHA, "短报告", 10)
        self.assertIn("## ZCode Review Gate", body)
        self.assertIn("短报告", body)
        self.assertNotIn("截断", body)
        long_body = self.mod.build_comment_body(
            "pass", (0, 0, 0), self.SHA, "报" * 5000, 10)
        self.assertLessEqual(len(long_body), 2000)
        self.assertIn("报告超长已截断", long_body)


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

    def test_lower_bound_clamps(self):
        cfg = self.mod.GateConfig({
            "poll_interval_seconds": 5,          # → 30
            "retry": {"base_seconds": 0,         # → 1
                      "max_seconds": 0,          # → 钳到 ≥ base
                      "max_attempts": 0}})       # → 1
        self.assertEqual(cfg.poll_interval, 30)
        self.assertEqual(cfg.retry_base, 1)
        self.assertGreaterEqual(cfg.retry_max, cfg.retry_base)
        self.assertEqual(cfg.retry_max_attempts, 1)


# ============================================================
# git token 注入 (env 方式) / GitError 不含 token
# ============================================================
class TestGitTokenHeader(_GateCase):
    def _capture(self, token):
        seen = {}

        def fake_run(cmd, *a, **kw):
            seen["cmd"] = list(cmd)
            seen["env"] = kw.get("env")
            return _CP(returncode=0, stdout="ok", stderr="")

        with mock.patch.object(self.mod.subprocess, "run", fake_run):
            self.mod.git(["fetch", "origin"], token=token)
        return seen

    def test_token_injected_via_env_not_argv(self):
        seen = self._capture("secret-tok")
        # argv 里绝不允许出现 token (ps 对本机所有用户可见)
        self.assertNotIn("secret-tok", " ".join(seen["cmd"]))
        # env 注入 GIT_CONFIG_* 三件套 (git≥2.31)
        env = seen["env"]
        self.assertEqual(env["GIT_CONFIG_COUNT"], "1")
        self.assertEqual(env["GIT_CONFIG_KEY_0"], "http.extraHeader")
        self.assertTrue(
            env["GIT_CONFIG_VALUE_0"].startswith("Authorization: Basic "))

    def test_basic_header_decodes_to_x_access_token(self):
        # GC-8G 实测: GitHub git smart-HTTP 拒绝 OAuth token 的 Bearer 形式,
        # 必须 Basic (x-access-token:<token> 的 base64); REST 侧 Bearer 不受影响
        seen = self._capture("secret-tok")
        value = seen["env"]["GIT_CONFIG_VALUE_0"]
        scheme, b64 = value.split(" ", 2)[1:]
        self.assertEqual(scheme, "Basic")
        decoded = base64.b64decode(b64).decode("utf-8")
        self.assertEqual(decoded, "x-access-token:secret-tok")

    def test_no_token_no_env_override(self):
        seen = self._capture(None)
        self.assertIsNone(seen["env"])  # 不传 env → 子进程原样继承

    def test_git_error_message_excludes_token(self):
        # 红线: GitError 消息/异常 repr 不能带 env 内容
        def fake_run(cmd, *a, **kw):
            return _CP(returncode=128, stdout="", stderr="fatal: auth failed")
        with mock.patch.object(self.mod.subprocess, "run", fake_run):
            with self.assertRaises(self.mod.GitError) as cm:
                self.mod.git(["fetch"], token="secret-tok")
        self.assertNotIn("secret-tok", str(cm.exception))
        self.assertNotIn("secret-tok", repr(cm.exception))


# ============================================================
# ensure_clone: 自愈 / 清理 / web 宿主推导
# ============================================================
class TestEnsureClone(_GateCase):
    def _cfg(self, api="https://api.github.com"):
        return self.mod.GateConfig({
            "clone_root": os.path.join(self.tmp, "clones"),
            "github_api": api})

    def test_incomplete_clone_rebuilt(self):
        """半成品 clone (有 .git 目录但 rev-parse 不过) → 删除重建"""
        cfg = self._cfg()
        dest = os.path.join(self.tmp, "clones", "o__r")
        os.makedirs(os.path.join(dest, ".git"))
        calls = []

        def fake_run(cmd, *a, **kw):
            calls.append(list(cmd))
            if "rev-parse" in cmd:
                return _CP(returncode=128, stdout="", stderr="not a git repo")
            if "clone" in cmd:
                os.makedirs(os.path.join(cmd[-1], ".git"), exist_ok=True)
                return _CP(returncode=0, stdout="", stderr="")
            return _CP(returncode=0, stdout="", stderr="")

        with mock.patch.object(self.mod.subprocess, "run", fake_run):
            out = self.mod.ensure_clone(cfg, "tok", "o", "r")
        self.assertEqual(out, dest)
        self.assertTrue(any("clone" in c for c in calls))  # 触发了重建

    def test_clone_failure_cleans_up(self):
        """clone 失败 (留了半个目录) → GitError 且残留被清理"""
        cfg = self._cfg()
        dest = os.path.join(self.tmp, "clones", "o__r")

        def fake_run(cmd, *a, **kw):
            if "clone" in cmd:
                os.makedirs(os.path.join(cmd[-1], ".git"), exist_ok=True)
                return _CP(returncode=128, stdout="", stderr="boom")
            return _CP(returncode=0, stdout="", stderr="")

        with mock.patch.object(self.mod.subprocess, "run", fake_run):
            with self.assertRaises(self.mod.GitError):
                self.mod.ensure_clone(cfg, "tok", "o", "r")
        self.assertFalse(os.path.exists(dest))

    def test_revparse_transient_keeps_clone(self):
        """rev-parse 超时/OSError (瞬时故障) → GitError 上抛, 健康 clone 不被误删"""
        cfg = self._cfg()
        dest = os.path.join(self.tmp, "clones", "o__r")
        os.makedirs(os.path.join(dest, ".git"))

        def fake_run(cmd, *a, **kw):
            if "rev-parse" in cmd:
                raise subprocess.TimeoutExpired(cmd=list(cmd), timeout=120)
            if "clone" in cmd:  # 不应走到: 瞬时故障不触发重建
                raise AssertionError("瞬时故障不应删除重建")
            return _CP(returncode=0, stdout="", stderr="")

        with mock.patch.object(self.mod.subprocess, "run", fake_run):
            with self.assertRaises(self.mod.GitError) as cm:
                self.mod.ensure_clone(cfg, "tok", "o", "r")
        self.assertTrue(cm.exception.transient)
        self.assertTrue(os.path.isdir(os.path.join(dest, ".git")))  # 未被删

    def test_git_base_url_derivation(self):
        self.assertEqual(self.mod._git_base_url("https://api.github.com"),
                         "https://github.com")
        self.assertEqual(self.mod._git_base_url("https://ghe.example.com/api/v3"),
                         "https://ghe.example.com")
        self.assertEqual(self.mod._git_base_url("https://weird.example.com"),
                         "https://github.com")

    def test_clone_uses_derived_url(self):
        cfg = self._cfg("https://ghe.example.com/api/v3")
        cmds = []

        def fake_run(cmd, *a, **kw):
            cmds.append(list(cmd))
            if "clone" in cmd:
                os.makedirs(os.path.join(cmd[-1], ".git"), exist_ok=True)
            return _CP(returncode=0, stdout="", stderr="")

        with mock.patch.object(self.mod.subprocess, "run", fake_run):
            self.mod.ensure_clone(cfg, "tok", "o", "r")
        clone_cmd = next(c for c in cmds if "clone" in c)
        self.assertIn("https://ghe.example.com/o/r.git", clone_cmd)


# ============================================================
# mcp_server 解析 (PATH → ~/.local/bin 回退 → 原样)
# ============================================================
class TestResolveMcpServer(_GateCase):
    """GC-8G 实测: systemd --user 默认 PATH 不含 ~/.local/bin,
    纯命令名需回退 ~/.local/bin 找组件。"""

    ENV_KEYS = _EnvGuard.ENV_KEYS + ("HOME",)

    def _cfg(self, name="zcode-mcp-server"):
        return self.mod.GateConfig({"mcp_server": name})

    def _make_local_exe(self, mode=0o755):
        os.environ["HOME"] = self.tmp
        local_bin = os.path.join(self.tmp, ".local", "bin")
        os.makedirs(local_bin)
        exe = os.path.join(local_bin, "zcode-mcp-server")
        with open(exe, "w") as f:
            f.write("#!/bin/sh\n")
        os.chmod(exe, mode)
        return exe

    def test_which_hit_returns_name(self):
        with mock.patch.object(self.mod.shutil, "which",
                               return_value="/usr/bin/zcode-mcp-server"):
            self.assertEqual(self.mod._resolve_mcp_server(self._cfg()),
                             "zcode-mcp-server")

    def test_fallback_to_local_bin(self):
        """which 返回 None + ~/.local/bin 存在可执行文件 → 选用回退路径"""
        exe = self._make_local_exe()
        with mock.patch.object(self.mod.shutil, "which", return_value=None):
            self.assertEqual(self.mod._resolve_mcp_server(self._cfg()), exe)

    def test_fallback_requires_executable(self):
        """~/.local/bin 里文件存在但不可执行 → 不用, 按原样交给 subprocess"""
        self._make_local_exe(mode=0o644)
        with mock.patch.object(self.mod.shutil, "which", return_value=None):
            self.assertEqual(self.mod._resolve_mcp_server(self._cfg()),
                             "zcode-mcp-server")

    def test_missing_everywhere_returns_as_is(self):
        """PATH 与 ~/.local/bin 都没有 → 原样 (OSError 走既有错误路径)"""
        os.environ["HOME"] = self.tmp
        with mock.patch.object(self.mod.shutil, "which", return_value=None):
            self.assertEqual(self.mod._resolve_mcp_server(self._cfg()),
                             "zcode-mcp-server")

    def test_path_value_used_as_is(self):
        """含路径分隔符的值原样使用, 不做任何探测"""
        with mock.patch.object(self.mod.shutil, "which") as m_which:
            self.assertEqual(
                self.mod._resolve_mcp_server(self._cfg("/opt/mcp/server")),
                "/opt/mcp/server")
        m_which.assert_not_called()


# ============================================================
# run_review 异常分支 / 超时透传 / stderr 尾部
# ============================================================
class TestRunReview(_GateCase):
    def _cfg(self):
        return self.mod.GateConfig({})

    def _run_with(self, cp=None, exc=None):
        def fake_run(cmd, *a, **kw):
            if exc is not None:
                raise exc
            return cp
        with mock.patch.object(self.mod.subprocess, "run", fake_run):
            return self.mod.run_review(self._cfg(), "/clone", "main", "sha")

    def test_bad_json_output(self):
        ok, err = self._run_with(_CP(returncode=0, stdout="not json", stderr=""))
        self.assertFalse(ok)
        self.assertIn("非 JSON", err)

    def test_empty_report(self):
        payload = {"ok": True, "result": {"content": []}}
        ok, err = self._run_with(
            _CP(returncode=0, stdout=json.dumps(payload), stderr=""))
        self.assertFalse(ok)
        self.assertIn("空报告", err)

    def test_mcp_server_missing_oserror(self):
        ok, err = self._run_with(exc=OSError("No such file or directory"))
        self.assertFalse(ok)
        self.assertIn("无法执行", err)

    def test_subprocess_timeout(self):
        ok, err = self._run_with(
            exc=subprocess.TimeoutExpired(cmd="x", timeout=1))
        self.assertFalse(ok)
        self.assertIn("超时", err)

    def test_timeout_env_passthrough(self):
        """zcode 审查预算 (3600) 透传给 mcp-server; gate 总超时再多留
        120s 给 mimosa 扫描与收尾 (REVIEW_TIMEOUT = ZCODE_REVIEW_TIMEOUT + 120)"""
        seen = {}

        def fake_run(cmd, *a, **kw):
            seen.update(kw)
            payload = {"ok": True, "result": {
                "content": [{"type": "text", "text": "P0: 0 P1: 0 P2: 0"}]}}
            return _CP(returncode=0, stdout=json.dumps(payload), stderr="")

        with mock.patch.object(self.mod.subprocess, "run", fake_run):
            ok, _report = self.mod.run_review(self._cfg(), "/c", "main", "s")
        self.assertTrue(ok)
        self.assertEqual(self.mod.REVIEW_TIMEOUT,
                         self.mod.ZCODE_REVIEW_TIMEOUT + 120)
        self.assertEqual(seen["timeout"], self.mod.REVIEW_TIMEOUT)
        self.assertEqual(seen["env"]["ZCODE_BRIDGE_REVIEW_TIMEOUT"],
                         str(self.mod.ZCODE_REVIEW_TIMEOUT))

    def test_stderr_tail_in_error(self):
        payload = {"ok": False, "result": {
            "content": [{"type": "text", "text": "炸了"}], "isError": True}}
        ok, err = self._run_with(
            _CP(returncode=2, stdout=json.dumps(payload), stderr="x" * 600))
        self.assertFalse(ok)
        self.assertIn("stderr:", err)
        self.assertIn("x" * 100, err)  # stderr 尾部在错误里


# ============================================================
# --once 全链路 (全 fake)
# ============================================================
class TestOnceEndToEnd(_GateCase):
    def setUp(self):
        super().setUp()
        os.environ["GITHUB_TOKEN"] = "fake-token-123"
        os.environ.pop("GH_TOKEN", None)
        self.pr_sha = "a" * 40
        self.pr_list = [self._mkpr(5, self.pr_sha)]
        self.comment_behavior = "ok"     # ok | retryable | ratelimited
        self.api_calls = []    # (method, url, body)
        self.mcp_calls = []    # 审查 args dict
        self.git_calls = []    # (cmd list, env)

    def _mkpr(self, number, sha, base="main"):
        return {"number": number, "title": f"pr {number}",
                "head": {"sha": sha}, "base": {"ref": base}}

    def _fake_urlopen(self, req, timeout=None, **kw):
        url = req.full_url
        method = req.get_method()
        body = json.loads(req.data.decode("utf-8")) if req.data else None
        self.api_calls.append((method, url, body))
        if "/pulls?" in url:
            return _FakeResp(self.pr_list)
        if url.endswith("/comments"):
            if self.comment_behavior == "retryable":
                raise _make_http_error(500)
            if self.comment_behavior == "ratelimited":
                raise _make_http_error(403, {"X-RateLimit-Remaining": "0",
                                             "X-RateLimit-Reset": "1893456000"})
            return _FakeResp(
                {"html_url": "https://github.com/octo/hello#issuecomment-1"})
        raise AssertionError(f"未预期 URL: {url}")

    def _fake_run(self, cmd, *a, **kw):
        if cmd[0] == "git":
            self.git_calls.append((list(cmd), kw.get("env")))
            if "clone" in cmd:
                # 假 clone 也要建出 .git, 否则每轮都重复 clone
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

    def _write_config(self, repos=("octo/hello",)):
        cfg = {"repos": list(repos),
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

    def _run_once(self, cfg_path, extra_args=()):
        with mock.patch.object(self.mod.subprocess, "run", self._fake_run), \
             mock.patch.object(self.mod.urllib.request, "urlopen",
                               self._fake_urlopen):
            return self.mod.main(["--once", "--config", cfg_path,
                                  *extra_args])

    def _state(self):
        with open(os.path.join(self.tmp, "state.json")) as f:
            return json.load(f)["prs"]

    def _state_entry(self, key="octo/hello#5"):
        return self._state()[key]

    def _force_retry_due(self, key="octo/hello#5"):
        """模拟退避到点: 把 next_retry_at 拨回过去 (不等真实 300s)"""
        path = os.path.join(self.tmp, "state.json")
        with open(path) as f:
            st = json.load(f)
        st["prs"][key]["next_retry_at"] = 0
        with open(path, "w") as f:
            json.dump(st, f)

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

        # git: clone URL 与 argv 均无 token; token 只走 GIT_CONFIG_* env
        # (Basic header, 明文 token 本身也不在 env 值里 — 只有 base64 形态)
        clone_cmd, clone_env = next(
            (c, e) for c, e in self.git_calls if "clone" in c)
        url_arg = next(a for a in clone_cmd if a.startswith("https://"))
        self.assertNotIn("fake-token-123", url_arg)
        self.assertNotIn("fake-token-123", " ".join(clone_cmd))
        self.assertEqual(clone_env["GIT_CONFIG_VALUE_0"],
                         self.mod._basic_auth_header("fake-token-123"))

        # state 落盘: reviewed/pass, report 缓存已清
        entry = self._state_entry()
        self.assertEqual(entry["status"], "reviewed")
        self.assertEqual(entry["verdict"], "pass")
        self.assertEqual(entry["head_sha"], "a" * 40)
        self.assertEqual(entry["attempts"], 0)
        self.assertTrue(entry["comment_url"])
        self.assertIsNone(entry["report"])

        # 第二轮同 sha: 不重复审查、不重复评论
        self.assertEqual(self._run_once(cfg_path), 0)
        self.assertEqual(len(self.mcp_calls), 1)
        self.assertEqual(len([c for c in self.api_calls if c[0] == "POST"]), 1)

        # 第三轮新 sha: 重审 + 重评论, attempts 从 0 重新计
        self.pr_sha = "b" * 40
        self.pr_list = [self._mkpr(5, self.pr_sha)]
        self.assertEqual(self._run_once(cfg_path), 0)
        self.assertEqual(len(self.mcp_calls), 2)
        self.assertEqual(len([c for c in self.api_calls if c[0] == "POST"]), 2)
        entry = self._state_entry()
        self.assertEqual(entry["head_sha"], "b" * 40)
        self.assertEqual(entry["attempts"], 0)
        self.assertEqual(entry["status"], "reviewed")

    def test_review_failure_goes_backoff_then_skip(self):
        """审查失败 → failed + 退避; 第二轮退避未到 → 跳过不再调审查。
        同时钉住语义: 有 PR 失败时 --once 仍 exit 0 (失败记在 state, 不传染退出码)"""
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

            # 第二轮: 退避未到, 不再调审查, 退出码仍 0
            self.assertEqual(self.mod.main(["--once", "--config", cfg_path]), 0)
            entry = self._state_entry()
            self.assertEqual(entry["attempts"], 1)  # 没增加

    def test_comment_retryable_failure_caches_and_comment_only_retry(self):
        """审查成功+评论 RetryableError → comment_failed+退避+缓存;
        退避到点后的下一轮: 只补评论, 不重跑 mcp 审查"""
        cfg_path = self._write_config()
        self.comment_behavior = "retryable"   # 评论 500

        # 第一轮: 审查成功, 评论失败 → comment_failed + 缓存 + attempts=1
        self.assertEqual(self._run_once(cfg_path), 0)
        self.assertEqual(len(self.mcp_calls), 1)
        self.assertEqual(len([c for c in self.api_calls if c[0] == "POST"]), 1)
        entry = self._state_entry()
        self.assertEqual(entry["status"], "comment_failed")
        self.assertEqual(entry["attempts"], 1)
        self.assertGreater(entry["next_retry_at"], 0)
        self.assertTrue(entry["report"])          # 审查结果已缓存
        self.assertEqual(entry["verdict"], "pass")
        self.assertEqual(entry["counts"], [0, 0, 2])

        # 退避到点 (拨回 next_retry_at), 评论恢复 → 只补评论
        self.comment_behavior = "ok"
        self._force_retry_due()
        git_marker = len(self.git_calls)
        self.assertEqual(self._run_once(cfg_path), 0)
        self.assertEqual(len(self.mcp_calls), 1)  # 没有重跑审查
        self.assertEqual(len([c for c in self.api_calls if c[0] == "POST"]), 2)
        # 缓存路径不拉 PR ref (fetch_pr_refs 只在真实审查前跑)
        round2_git = self.git_calls[git_marker:]
        self.assertFalse(any(any("refs/pull/" in a for a in c)
                             for c, _e in round2_git))
        entry = self._state_entry()
        self.assertEqual(entry["status"], "reviewed")
        self.assertIsNone(entry["report"])        # reviewed 后缓存清掉
        self.assertEqual(entry["attempts"], 0)

    def test_comment_ratelimited_no_attempts_burn_comment_only_retry(self):
        """评论遇限流 → 不烧 attempts 但有缓存; 下轮只补评论"""
        cfg_path = self._write_config()
        self.comment_behavior = "ratelimited"

        # 第一轮: 审查成功, 评论限流 → comment_failed, attempts 不增
        self.assertEqual(self._run_once(cfg_path), 0)
        self.assertEqual(len(self.mcp_calls), 1)
        entry = self._state_entry()
        self.assertEqual(entry["status"], "comment_failed")
        self.assertEqual(entry["attempts"], 0)    # 限流不烧重试次数
        self.assertTrue(entry["report"])

        # 第二轮 (限流 reset 语义上下轮自然重试): 只补评论
        self.comment_behavior = "ok"
        self.assertEqual(self._run_once(cfg_path), 0)
        self.assertEqual(len(self.mcp_calls), 1)  # 仍未重跑审查
        self.assertEqual(len([c for c in self.api_calls if c[0] == "POST"]), 2)
        entry = self._state_entry()
        self.assertEqual(entry["status"], "reviewed")
        self.assertIsNone(entry["report"])

    def test_repo_flag_overrides_config_repos(self):
        """CLI --repo 覆盖配置文件 repos"""
        cfg_path = self._write_config(repos=("octo/hello",))
        rc = self._run_once(cfg_path, extra_args=("--repo", "octo/other"))
        self.assertEqual(rc, 0)
        gets = [u for m, u, _b in self.api_calls if m == "GET"]
        self.assertTrue(all("/repos/octo/other/" in u for u in gets))
        self.assertIn("octo/other#5", self._state())
        self.assertNotIn("octo/hello#5", self._state())

    def test_pr_filter(self):
        """--pr 只处理指定 PR"""
        self.pr_list = [self._mkpr(5, self.pr_sha), self._mkpr(6, "c" * 40)]
        cfg_path = self._write_config()
        rc = self._run_once(cfg_path, extra_args=("--pr", "6"))
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.mcp_calls), 1)
        posts = [c for c in self.api_calls if c[0] == "POST"]
        self.assertEqual(len(posts), 1)
        self.assertIn("issues/6/comments", posts[0][1])
        self.assertIn("octo/hello#6", self._state())
        self.assertNotIn("octo/hello#5", self._state())


if __name__ == "__main__":
    unittest.main()
