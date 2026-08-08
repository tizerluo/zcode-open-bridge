"""
test_security_review.py — review 只读护栏 + zcode_security_review 管线单测

覆盖 2026-08-08 重构 (告别 --mode plan):
  - _build_review_cmd: yolo + 写/执行工具全黑名单 + --json, 不含 plan
  - review prompt 含"只审不修"约束
  - _extract_response: --json 输出提取 response / 非 JSON 原样返回
  - _find_mimosa_root: env 覆盖 / 找不到返回 None
  - MimosaMcpClient: stdio JSON-RPC 握手 + tools/call (假 server 实测)
  - _mimosa_quick_scan: content 提取 / isError 抛异常
  - tool_zcode_security_review: mimosa 缺失报错 / 扫描失败报错 / 正常管线

运行: python3 tests/test_security_review.py
依赖: 仅 Python 标准库 + zcode-mcp-server 模块
"""

import json
import os
import subprocess
import sys
import types
import unittest

MCP_PATH = os.path.join(
    os.path.dirname(__file__), "..", "packages", "mcp-server", "zcode-mcp-server"
)


def _load_mcp_module():
    mod = types.ModuleType("zcode_mcp_server")
    mod.__file__ = MCP_PATH
    with open(MCP_PATH) as f:
        code = f.read()
    code_no_main = code.split('if __name__ == "__main__":')[0]
    exec(code_no_main, mod.__dict__)
    return mod


class _FakeCompletedProcess:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


# 假 mimosa MCP server (stdio JSON-RPC): initialize 回握手, tools/call 回固定文本
_FAKE_SERVER = r"""
import json, sys
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    req = json.loads(line)
    method = req.get("method")
    if method == "initialize":
        resp = {"jsonrpc": "2.0", "id": req["id"],
                "result": {"protocolVersion": "2024-11-05", "capabilities": {},
                           "serverInfo": {"name": "fake-mimosa", "version": "0"}}}
        sys.stdout.write(json.dumps(resp) + "\n"); sys.stdout.flush()
    elif method == "tools/call":
        resp = {"jsonrpc": "2.0", "id": req["id"],
                "result": {"content": [{"type": "text", "text": "FINDINGS-OK"}]}}
        sys.stdout.write(json.dumps(resp) + "\n"); sys.stdout.flush()
    # notification (notifications/initialized): 无响应
"""


class _EnvGuard(unittest.TestCase):
    """保存/恢复本文件用到的环境变量。"""

    ENV_KEYS = ("ZCODE_BRIDGE_REVIEW_LOCK", "ZCODE_BRIDGE_MIMOSA_ROOT",
                "ZCODE_BRIDGE_REVIEW_TIMEOUT", "ZCODE_BRIDGE_MIMOSA_SCAN_ROOT")

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in self.ENV_KEYS}

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class TestReviewCmd(_EnvGuard):
    """只读护栏: 命令构造 + prompt 约束"""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_mcp_module()

    def _capture_cmd(self, **kwargs):
        """patch subprocess.run 捕获 cmd, 返回 (cmd, result)。"""
        mod = self.mod
        captured = {}

        def fake_run(cmd, *a, **kw):
            captured["cmd"] = cmd
            return _FakeCompletedProcess(returncode=0, stdout="OK", stderr="")

        saved = mod.subprocess.run
        mod.subprocess.run = fake_run
        os.environ["ZCODE_BRIDGE_REVIEW_LOCK"] = "0"
        try:
            result = mod.tool_zcode_review({"code": "print('x')", **kwargs})
        finally:
            mod.subprocess.run = saved
        return captured["cmd"], result

    def test_rc0_yolo_not_plan(self):
        """RC0: 用 --mode yolo, 绝不再出现 plan"""
        cmd, _ = self._capture_cmd()
        self.assertIn("--mode", cmd)
        self.assertEqual(cmd[cmd.index("--mode") + 1], "yolo")
        self.assertNotIn("plan", cmd)

    def test_rc1_denylist_blocks_write_and_repl(self):
        """RC1: 黑名单含写工具 + Bash + Node REPL 一族 (防 execSync 打穿)"""
        cmd, _ = self._capture_cmd()
        self.assertIn("--disallowed-tools", cmd)
        deny = cmd[cmd.index("--disallowed-tools") + 1]
        for tool in ("Write", "Edit", "MultiEdit", "ApplyPatch", "Bash",
                     "js", "js_reset", "js_add_node_module_dir",
                     "mcp__node_repl__js", "mcp__node_repl__js_reset",
                     "mcp__node_repl__js_add_node_module_dir"):
            self.assertIn(tool, deny.split(), f"黑名单缺 {tool}")

    def test_rc2_json_output(self):
        """RC2: 带 --json (结构化输出)"""
        cmd, _ = self._capture_cmd()
        self.assertIn("--json", cmd)

    def test_rc3_prompt_pins_review_only(self):
        """RC3: prompt 钉死"只审不修"职责"""
        cmd, _ = self._capture_cmd()
        prompt = cmd[cmd.index("--prompt") + 1]
        self.assertIn("绝对不要修改", prompt)
        self.assertIn("只读工具", prompt)

    def test_rc4_timeout_env_configurable(self):
        """RC4: 单次超时由 ZCODE_BRIDGE_REVIEW_TIMEOUT 控制 (默认 300)"""
        mod = self.mod
        self.assertEqual(mod._review_timeout(), 300)
        os.environ["ZCODE_BRIDGE_REVIEW_TIMEOUT"] = "60"
        self.assertEqual(mod._review_timeout(), 60)
        os.environ["ZCODE_BRIDGE_REVIEW_TIMEOUT"] = "1"  # clamp 下限 30
        self.assertEqual(mod._review_timeout(), 30)


class TestExtractResponse(unittest.TestCase):
    """--json 输出的 response 提取"""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_mcp_module()

    def test_ex0_json_response_extracted(self):
        out = json.dumps({"response": "审查结论正文", "usage": {}}, ensure_ascii=False)
        self.assertEqual(self.mod._extract_response(out), "审查结论正文")

    def test_ex1_non_json_passthrough(self):
        self.assertEqual(self.mod._extract_response("纯文本结论"), "纯文本结论")

    def test_ex2_json_without_response_passthrough(self):
        raw = json.dumps({"error": "something"})
        self.assertEqual(self.mod._extract_response(raw), raw)

    def test_ex3_end_to_end_via_tool(self):
        """tool_zcode_review 成功路径返回的是提取后的 response, 不是整个 JSON"""
        mod = self.mod
        payload = json.dumps({"response": "提取后的正文"}, ensure_ascii=False)
        saved = mod.subprocess.run
        mod.subprocess.run = lambda *a, **kw: _FakeCompletedProcess(0, payload, "")
        os.environ["ZCODE_BRIDGE_REVIEW_LOCK"] = "0"
        try:
            result = mod.tool_zcode_review({"code": "x"})
        finally:
            mod.subprocess.run = saved
            os.environ.pop("ZCODE_BRIDGE_REVIEW_LOCK", None)
        self.assertNotIn("isError", result)
        self.assertEqual(result["content"][0]["text"], "提取后的正文")


class TestFindMimosaRoot(_EnvGuard):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_mcp_module()

    def test_fm0_env_override(self):
        """FM0: ZCODE_BRIDGE_MIMOSA_ROOT 指向合法根 → 采用"""
        import tempfile
        d = tempfile.mkdtemp(prefix="mimosa-root-")
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        os.makedirs(os.path.join(d, "payload", "dist", "mcp"))
        open(os.path.join(d, "payload", "dist", "mcp", "server.js"), "w").close()
        os.environ["ZCODE_BRIDGE_MIMOSA_ROOT"] = d
        self.assertEqual(self.mod._find_mimosa_root(), d)

    def test_fm1_env_invalid_returns_none_or_fallback(self):
        """FM1: env 指向无效目录 → 不被采用 (落回探测或 None)"""
        os.environ["ZCODE_BRIDGE_MIMOSA_ROOT"] = "/nonexistent/mimosa"
        root = self.mod._find_mimosa_root()
        self.assertNotEqual(root, "/nonexistent/mimosa")
        if root is not None:  # 本机若真装了 mimosa 则必须合法
            self.assertTrue(os.path.exists(
                os.path.join(root, "payload", "dist", "mcp", "server.js")))


class TestMimosaMcpClient(_EnvGuard):
    """MimosaMcpClient 协议层 (假 stdio server 实测握手 + tools/call)"""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_mcp_module()
        import tempfile
        cls._fake = os.path.join(
            tempfile.mkdtemp(prefix="fake-mcp-"), "fake_server.py")
        with open(cls._fake, "w") as f:
            f.write(_FAKE_SERVER)

    def _make_client(self):
        """绕过 __init__ (不起 node), 直接挂上假 server 子进程。"""
        client = object.__new__(self.mod.MimosaMcpClient)
        client._proc = subprocess.Popen(
            [sys.executable, self._fake],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        client.timeout = 10
        client._id = 0
        return client

    def test_mc0_handshake_and_call(self):
        """MC0: initialize + tools/call 全流程"""
        client = self._make_client()
        try:
            client.initialize()  # 不抛异常即握手成功
            result = client.call_tool("security_scan", {"path": "/tmp"})
            texts = [c["text"] for c in result["content"]]
            self.assertIn("FINDINGS-OK", texts)
        finally:
            client.close()

    def test_mc1_close_idempotent(self):
        """MC1: close 后可再 close (幂等, 不崩)"""
        client = self._make_client()
        client.close()
        client.close()


class _StubMimosaClient:
    """_mimosa_quick_scan 的 stub (不起真子进程)。"""

    #: 类属性, 测试可覆盖 (返回的 content 文本 / 是否 isError)
    response_text = "**Mimosa deep security scan:**\n- findings: 1"
    is_error = False

    def __init__(self, root, cwd, timeout=120):
        self.args_received = None

    def initialize(self):
        pass

    def call_tool(self, name, arguments):
        self.args_received = (name, arguments)
        if self.is_error:
            return {"isError": True,
                    "content": [{"type": "text", "text": "engine boom"}]}
        return {"content": [{"type": "text", "text": self.response_text}]}

    def close(self):
        pass


class TestMimosaQuickScan(_EnvGuard):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_mcp_module()

    def _run_scan(self, stub_cls=_StubMimosaClient):
        mod = self.mod
        saved = mod.MimosaMcpClient
        mod.MimosaMcpClient = stub_cls
        try:
            return mod._mimosa_quick_scan("/fake/root", "/fake/proj")
        finally:
            mod.MimosaMcpClient = saved

    def test_ms0_summary_returned(self):
        """MS0: 摘要文本原样返回; 无 scanDir 时 findings 为空"""
        body, findings = self._run_scan()
        self.assertIn("Mimosa", body)
        self.assertEqual(findings, [])

    def test_ms1_is_error_raises(self):
        """MS1: isError 响应 → 抛 RuntimeError"""
        class ErrClient(_StubMimosaClient):
            is_error = True
        with self.assertRaises(RuntimeError) as ctx:
            self._run_scan(ErrClient)
        self.assertIn("engine boom", str(ctx.exception))

    def test_ms2_scandir_findings_read_back(self):
        """MS2: 摘要含 scanDir → 回读 findings.json 全量"""
        import tempfile
        scan_root = tempfile.mkdtemp(prefix="mimosa-scans-")
        self.addCleanup(lambda: __import__("shutil").rmtree(scan_root, ignore_errors=True))
        scan_dir = os.path.join(scan_root, "project-x", "scan-1")
        os.makedirs(scan_dir)
        findings_payload = {
            "schemaVersion": "mimosa-security-scan-findings/v1",
            "findings": [
                {"identity": {"publicClass": "sql-injection"},
                 "severity": "high", "cwe": ["CWE-89"],
                 "location": {"path": "app.py", "line": 11},
                 "title": "SQL 注入", "message": "拼接查询"},
            ],
        }
        with open(os.path.join(scan_dir, "findings.json"), "w") as f:
            json.dump(findings_payload, f)
        # scanDir 校验要求落在扫描历史根下 (狗食 review P1-1), 用 env 指定测试根
        os.environ["ZCODE_BRIDGE_MIMOSA_SCAN_ROOT"] = scan_root

        class ScanDirClient(_StubMimosaClient):
            response_text = f"**Mimosa deep security scan:**\n- scanDir: `{scan_dir}`\n- findings: 1"

        body, findings = self._run_scan(ScanDirClient)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["severity"], "high")

    def test_ms2b_scandir_outside_root_rejected(self):
        """MS2b: scanDir 越界 (不在扫描历史根下) → 拒绝回读 (狗食 review P1-1)"""
        import tempfile
        outside = tempfile.mkdtemp(prefix="mimosa-outside-")
        self.addCleanup(lambda: __import__("shutil").rmtree(outside, ignore_errors=True))
        with open(os.path.join(outside, "findings.json"), "w") as f:
            json.dump({"findings": [{"severity": "high", "title": "不该被读到"}]}, f)
        # scan root 指向另一个空目录 → outside 不在其下
        scan_root = tempfile.mkdtemp(prefix="mimosa-scans-")
        self.addCleanup(lambda: __import__("shutil").rmtree(scan_root, ignore_errors=True))
        os.environ["ZCODE_BRIDGE_MIMOSA_SCAN_ROOT"] = scan_root

        class EvilClient(_StubMimosaClient):
            response_text = f"**Mimosa**\n- scanDir: `{outside}`\n- findings: 1"

        body, findings = self._run_scan(EvilClient)
        self.assertEqual(findings, [], "越界 scanDir 不应回读任何 findings")

    def test_ms2c_symlink_findings_rejected(self):
        """MS2c: scanDir 合法但 findings.json 是指向根外的 symlink → 拒绝 (复审 P1-A/B)"""
        import tempfile
        scan_root = tempfile.mkdtemp(prefix="mimosa-scans-")
        outside = tempfile.mkdtemp(prefix="mimosa-outside-")
        for d in (scan_root, outside):
            self.addCleanup(lambda d=d: __import__("shutil").rmtree(d, ignore_errors=True))
        secret = os.path.join(outside, "secret.json")
        with open(secret, "w") as f:
            json.dump({"findings": [{"severity": "high", "title": "不该被读到"}]}, f)
        scan_dir = os.path.join(scan_root, "project-x", "scan-1")
        os.makedirs(scan_dir)
        os.symlink(secret, os.path.join(scan_dir, "findings.json"))
        os.environ["ZCODE_BRIDGE_MIMOSA_SCAN_ROOT"] = scan_root

        class SymlinkClient(_StubMimosaClient):
            response_text = f"**Mimosa**\n- scanDir: `{scan_dir}`\n- findings: 1"

        body, findings = self._run_scan(SymlinkClient)
        self.assertEqual(findings, [], "symlink 指向根外的 findings.json 不应被读")

    def test_ms3_compact_projection(self):
        """MS3: _compact_findings 投影保留复核所需字段"""
        findings = [{
            "findingId": "finding:x", "occurrenceId": "occurrence:y",
            "identity": {"publicClass": "hardcoded-credential", "anchor": "sha256:a"},
            "kind": "static", "severity": "high", "cwe": ["CWE-798"],
            "location": {"path": "a.py", "line": 4, "endLine": 4},
            "title": "硬编码凭据", "message": "密钥写在源码",
            "proofGaps": [],
        }]
        compact = self.mod._compact_findings(findings)
        self.assertEqual(len(compact), 1)
        c = compact[0]
        self.assertEqual(c["class"], "hardcoded-credential")
        self.assertEqual(c["path"], "a.py")
        self.assertEqual(c["line"], 4)
        self.assertNotIn("anchor", c, "哈希等内部字段应被投影掉")


class TestSecurityReviewTool(_EnvGuard):
    """tool_zcode_security_review 管线"""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_mcp_module()

    def _patch_common(self, summary="摘要: findings 1",
                      findings=None):
        import tempfile
        mod = self.mod
        if findings is None:
            findings = [{"identity": {"publicClass": "sql-injection"},
                         "severity": "high", "cwe": ["CWE-89"],
                         "location": {"path": "app.py", "line": 11},
                         "title": "SQL 注入", "message": "拼接查询"}]
        # 扫描目录真实存在 (P2-8 校验), 用临时目录充当被扫项目
        proj = tempfile.mkdtemp(prefix="zcode-scan-proj-")
        self.addCleanup(lambda: __import__("shutil").rmtree(proj, ignore_errors=True))
        saved = {
            "find_root": mod._find_mimosa_root,
            "scan": mod._mimosa_quick_scan,
            "run": mod.subprocess.run,
        }
        mod._find_mimosa_root = lambda: "/fake/mimosa"
        mod._mimosa_quick_scan = lambda root, path: (summary, findings)
        os.environ["ZCODE_BRIDGE_REVIEW_LOCK"] = "0"
        return mod, saved, proj

    def _restore_common(self, mod, saved):
        mod._find_mimosa_root = saved["find_root"]
        mod._mimosa_quick_scan = saved["scan"]
        mod.subprocess.run = saved["run"]

    def test_sr0_mimosa_missing_clean_error(self):
        """SR0: mimosa 未安装 → 明确报错并建议 fallback (不崩)"""
        mod = self.mod
        saved = mod._find_mimosa_root
        mod._find_mimosa_root = lambda: None
        try:
            result = mod.tool_zcode_security_review({"path": "/tmp"})
        finally:
            mod._find_mimosa_root = saved
        self.assertTrue(result.get("isError"))
        self.assertIn("mimosa", result["content"][0]["text"])
        self.assertIn("zcode_review", result["content"][0]["text"])

    def test_sr1_scan_failure_clean_error(self):
        """SR1: mimosa 扫描失败 → 明确报错, 不再调 zcode"""
        mod, saved, proj = self._patch_common()

        def boom(root, path):
            raise RuntimeError("engine boom")

        mod._mimosa_quick_scan = boom
        zcode_called = {"n": 0}

        def fake_run(*a, **kw):
            zcode_called["n"] += 1
            return _FakeCompletedProcess(0, "OK", "")

        mod.subprocess.run = fake_run
        try:
            result = mod.tool_zcode_security_review({"path": proj})
        finally:
            self._restore_common(mod, saved)
        self.assertTrue(result.get("isError"))
        self.assertIn("mimosa 扫描失败", result["content"][0]["text"])
        self.assertEqual(zcode_called["n"], 0, "扫描失败不应再调 zcode")

    def test_sr2_happy_path_pipeline(self):
        """SR2: 正常管线 — findings 作附件, zcode 只读复核, 返回提取的 response"""
        mod, saved, proj = self._patch_common()
        captured = {}

        def fake_run(cmd, *a, **kw):
            captured["cmd"] = cmd
            return _FakeCompletedProcess(
                0, json.dumps({"response": "安全报告正文"}, ensure_ascii=False), "")

        mod.subprocess.run = fake_run
        try:
            result = mod.tool_zcode_security_review(
                {"path": proj, "focus": "注入类"})
        finally:
            self._restore_common(mod, saved)
        self.assertNotIn("isError", result)
        self.assertEqual(result["content"][0]["text"], "安全报告正文")
        cmd = captured["cmd"]
        # yolo + 黑名单 + --json + 恰好一个 --attach (findings 临时文件)
        self.assertEqual(cmd[cmd.index("--mode") + 1], "yolo")
        self.assertIn("--disallowed-tools", cmd)
        self.assertIn("--json", cmd)
        attach_idx = [i for i, v in enumerate(cmd) if v == "--attach"]
        self.assertEqual(len(attach_idx), 1)
        self.assertIn("zcode-mimosa-findings-", cmd[attach_idx[0] + 1])
        # prompt 是安全复核口径 + 含额外重点
        prompt = cmd[cmd.index("--prompt") + 1]
        self.assertIn("mimosa", prompt)
        self.assertIn("确认漏洞", prompt)
        self.assertIn("注入类", prompt)
        self.assertIn("绝对不要修改", prompt)

    def test_sr3b_scan_path_missing_clean_error(self):
        """SR3b: 扫描目录不存在 → 明确报错 (狗食 review P2-8), 不调 mimosa/zcode"""
        mod = self.mod
        result = mod.tool_zcode_security_review({"path": "/nonexistent/xyz"})
        self.assertTrue(result.get("isError"))
        self.assertIn("不存在", result["content"][0]["text"])

    def test_sr3_findings_tmpfile_cleaned(self):
        """SR3: findings 临时文件用后被清理"""
        mod, saved, proj = self._patch_common()
        captured = {}
        mod.subprocess.run = lambda cmd, *a, **kw: (
            captured.update(cmd=cmd),
            _FakeCompletedProcess(0, "OK", ""))[1]
        try:
            mod.tool_zcode_security_review({"path": proj})
        finally:
            self._restore_common(mod, saved)
        attach_path = captured["cmd"][captured["cmd"].index("--attach") + 1]
        self.assertFalse(os.path.exists(attach_path), "临时 findings 文件应被清理")


class _StubDeepClient:
    """deep 异步管线 stub: start → status 序列 → completed/failed。"""

    statuses = ["running", "completed"]  # 测试可覆盖
    scan_dir = None
    instances = []

    def __init__(self, root, cwd, timeout=120):
        self.calls = []
        self._status_idx = 0
        _StubDeepClient.instances.append(self)

    def initialize(self):
        pass

    def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if name == "security_scan_start":
            job = {"jobId": "job-1", "status": "running"}
        elif name == "security_scan_status":
            st = self.statuses[min(self._status_idx, len(self.statuses) - 1)]
            self._status_idx += 1
            job = {"jobId": "job-1", "status": st}
            if st == "completed":
                job["result"] = {"scanId": "s1", "scanDir": self.scan_dir,
                                 "seal": "sha256:x", "findingCount": 1,
                                 "hypotheses": [], "dependencySummary": {}}
            if st == "failed":
                job["error"] = {"message": "engine exploded"}
        elif name == "security_scan_cancel":
            job = {"jobId": "job-1", "status": "cancel_requested"}
        else:
            raise AssertionError(f"未预期的 tool: {name}")
        text = json.dumps(
            {"schemaVersion": "mimosa-mcp-security-scan-job/v1", "job": job})
        return {"content": [{"type": "text", "text": text}]}

    def close(self):
        pass


class TestParseScanJob(unittest.TestCase):
    """_parse_scan_job: 异步响应 (嵌套 JSON 字符串) 解析"""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_mcp_module()

    def _result(self, text=None, is_error=False):
        r = {"content": [{"type": "text", "text": text}] if text is not None else []}
        if is_error:
            r["isError"] = True
        return r

    def test_pj0_valid(self):
        text = json.dumps({"schemaVersion": "mimosa-mcp-security-scan-job/v1",
                           "job": {"jobId": "j1", "status": "running"}})
        job = self.mod._parse_scan_job(self._result(text))
        self.assertEqual(job["jobId"], "j1")

    def test_pj1_is_error_raises(self):
        with self.assertRaises(RuntimeError):
            self.mod._parse_scan_job(self._result("boom", is_error=True))

    def test_pj2_non_json_raises(self):
        with self.assertRaises(RuntimeError) as ctx:
            self.mod._parse_scan_job(self._result("not json at all"))
        self.assertIn("非 JSON", str(ctx.exception))

    def test_pj3_missing_job_raises(self):
        with self.assertRaises(RuntimeError) as ctx:
            self.mod._parse_scan_job(self._result(json.dumps({"other": 1})))
        self.assertIn("job", str(ctx.exception))

    def test_pj4_empty_raises(self):
        with self.assertRaises(RuntimeError):
            self.mod._parse_scan_job(self._result(None))


class TestMimosaDeepScan(_EnvGuard):
    """_mimosa_deep_scan: start/status 轮询/回读/失败/超时"""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_mcp_module()

    def setUp(self):
        super().setUp()
        _StubDeepClient.instances = []
        _StubDeepClient.statuses = ["running", "completed"]
        _StubDeepClient.scan_dir = None

    def _run_deep(self, stub_cls=_StubDeepClient, focus_files=None):
        mod = self.mod
        saved_client = mod.MimosaMcpClient
        saved_sleep = mod.time.sleep
        mod.MimosaMcpClient = stub_cls
        mod.time.sleep = lambda s: None
        try:
            return mod._mimosa_deep_scan("/fake/root", "/fake/proj", focus_files)
        finally:
            mod.MimosaMcpClient = saved_client
            mod.time.sleep = saved_sleep

    def _make_scan_dir(self):
        import tempfile
        scan_root = tempfile.mkdtemp(prefix="mimosa-scans-")
        self.addCleanup(lambda: __import__("shutil").rmtree(scan_root, ignore_errors=True))
        scan_dir = os.path.join(scan_root, "project-x", "scan-1")
        os.makedirs(scan_dir)
        with open(os.path.join(scan_dir, "findings.json"), "w") as f:
            json.dump({"findings": [
                {"identity": {"publicClass": "sql-injection"},
                 "severity": "high", "cwe": ["CWE-89"],
                 "location": {"path": "app.py", "line": 11},
                 "title": "SQL 注入", "message": "拼接查询"}]}, f)
        os.environ["ZCODE_BRIDGE_MIMOSA_SCAN_ROOT"] = scan_root
        return scan_dir

    def test_ds0_happy_path(self):
        """DS0: start→running→completed, findings 回读, focusFiles 透传"""
        _StubDeepClient.scan_dir = self._make_scan_dir()
        summary, findings = self._run_deep(focus_files=["app.py"])
        self.assertIn("deep", summary)
        self.assertIn("job-1", summary)
        self.assertEqual(len(findings), 1)
        client = _StubDeepClient.instances[0]
        start_call = [a for n, a in client.calls if n == "security_scan_start"][0]
        self.assertEqual(start_call["depth"], "deep")
        self.assertEqual(start_call["focusFiles"], ["app.py"])
        status_calls = [n for n, _ in client.calls if n == "security_scan_status"]
        self.assertEqual(len(status_calls), 2, "running 一次 + completed 一次")

    def test_ds1_failed_raises_with_message(self):
        """DS1: status=failed → 抛错带 error.message"""
        _StubDeepClient.statuses = ["running", "failed"]
        with self.assertRaises(RuntimeError) as ctx:
            self._run_deep()
        self.assertIn("engine exploded", str(ctx.exception))
        self.assertIn("failed", str(ctx.exception))

    def test_ds2_timeout_cancels_job(self):
        """DS2: 一直 running + 超时 → TimeoutError 且 best-effort cancel"""
        mod = self.mod

        class RunningClient(_StubDeepClient):
            statuses = ["running"]

        saved_client = mod.MimosaMcpClient
        saved_sleep = mod.time.sleep
        saved_time = mod.time.time
        mod.MimosaMcpClient = RunningClient
        mod.time.sleep = lambda s: None
        real_t = saved_time()
        ticks = iter([real_t, real_t + 10000])  # t0, 首次检查即超 900s 预算
        mod.time.time = lambda: next(ticks, real_t + 10000)
        try:
            with self.assertRaises(TimeoutError):
                mod._mimosa_deep_scan("/fake/root", "/fake/proj")
        finally:
            mod.MimosaMcpClient = saved_client
            mod.time.sleep = saved_sleep
            mod.time.time = saved_time
        names = [n for n, _ in RunningClient.instances[0].calls]
        self.assertIn("security_scan_cancel", names, "超时应 best-effort cancel")

    def test_ds3_no_jobid_raises(self):
        """DS3: start 响应缺 jobId → 明确报错"""
        mod = self.mod

        class NoJobClient(_StubDeepClient):
            def call_tool(self, name, arguments):
                if name == "security_scan_start":
                    return {"content": [{"type": "text", "text": json.dumps(
                        {"job": {"status": "running"}})}]}
                return super().call_tool(name, arguments)

        saved_client = mod.MimosaMcpClient
        mod.MimosaMcpClient = NoJobClient
        try:
            with self.assertRaises(RuntimeError) as ctx:
                mod._mimosa_deep_scan("/fake/root", "/fake/proj")
            self.assertIn("jobId", str(ctx.exception))
        finally:
            mod.MimosaMcpClient = saved_client


class TestSecurityReviewDepth(_EnvGuard):
    """tool_zcode_security_review 的 depth/focus_files 参数"""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_mcp_module()

    def _patch(self):
        import tempfile
        mod = self.mod
        proj = tempfile.mkdtemp(prefix="zcode-scan-proj-")
        self.addCleanup(lambda: __import__("shutil").rmtree(proj, ignore_errors=True))
        saved = {
            "find_root": mod._find_mimosa_root,
            "quick": mod._mimosa_quick_scan,
            "deep": mod._mimosa_deep_scan,
            "run": mod.subprocess.run,
        }
        mod._find_mimosa_root = lambda: "/fake/mimosa"
        os.environ["ZCODE_BRIDGE_REVIEW_LOCK"] = "0"
        return mod, saved, proj

    def _restore(self, mod, saved):
        mod._find_mimosa_root = saved["find_root"]
        mod._mimosa_quick_scan = saved["quick"]
        mod._mimosa_deep_scan = saved["deep"]
        mod.subprocess.run = saved["run"]

    def test_dp0_invalid_depth_rejected(self):
        """DP0: 非法 depth → 明确报错"""
        mod = self.mod
        result = mod.tool_zcode_security_review(
            {"path": "/tmp", "depth": "turbo"})
        self.assertTrue(result.get("isError"))
        self.assertIn("depth", result["content"][0]["text"])

    def test_dp1_default_is_normal(self):
        """DP1: 不传 depth → 走 normal 快扫, 不碰 deep"""
        mod, saved, proj = self._patch()
        called = {"quick": 0, "deep": 0}
        mod._mimosa_quick_scan = lambda r, p: (called.update(quick=1), ("摘要", []))[1]

        def deep_should_not_run(r, p, f=None):
            called["deep"] += 1
            return ("", [])

        mod._mimosa_deep_scan = deep_should_not_run
        mod.subprocess.run = lambda *a, **kw: _FakeCompletedProcess(0, "OK", "")
        try:
            result = mod.tool_zcode_security_review({"path": proj})
        finally:
            self._restore(mod, saved)
        self.assertNotIn("isError", result)
        self.assertEqual(called["quick"], 1)
        self.assertEqual(called["deep"], 0)

    def test_dp2_deep_routes_with_focus_files(self):
        """DP2: depth=deep → 走异步管线, focus_files 透传, 附件标注 depth=deep"""
        mod, saved, proj = self._patch()
        captured = {}

        def quick_should_not_run(r, p):
            raise AssertionError("depth=deep 不应走 normal 快扫")

        mod._mimosa_quick_scan = quick_should_not_run

        def fake_deep(r, p, focus_files=None):
            captured["focus_files"] = focus_files
            return ("deep 摘要", [{"identity": {"publicClass": "x"},
                                 "severity": "high", "cwe": [],
                                 "location": {"path": "a.py", "line": 1},
                                 "title": "t", "message": "m"}])

        mod._mimosa_deep_scan = fake_deep

        def fake_run(cmd, *a, **kw):
            captured["cmd"] = cmd
            return _FakeCompletedProcess(
                0, json.dumps({"response": "deep 报告"}, ensure_ascii=False), "")

        mod.subprocess.run = fake_run
        try:
            result = mod.tool_zcode_security_review(
                {"path": proj, "depth": "deep", "focus_files": ["a.py"]})
        finally:
            self._restore(mod, saved)
        self.assertNotIn("isError", result)
        self.assertEqual(result["content"][0]["text"], "deep 报告")
        self.assertEqual(captured["focus_files"], ["a.py"])
        prompt = captured["cmd"][captured["cmd"].index("--prompt") + 1]
        self.assertIn("depth=deep", prompt)


if __name__ == "__main__":
    unittest.main(verbosity=2)
