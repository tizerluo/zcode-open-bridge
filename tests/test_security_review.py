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
                "ZCODE_BRIDGE_REVIEW_TIMEOUT")

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
        for tool in ("Write", "Edit", "ApplyPatch", "Bash",
                     "js", "mcp__node_repl__js"):
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
        scan_dir = tempfile.mkdtemp(prefix="mimosa-scan-")
        self.addCleanup(lambda: __import__("shutil").rmtree(scan_dir, ignore_errors=True))
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

        class ScanDirClient(_StubMimosaClient):
            response_text = f"**Mimosa deep security scan:**\n- scanDir: `{scan_dir}`\n- findings: 1"

        body, findings = self._run_scan(ScanDirClient)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["severity"], "high")

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
        mod = self.mod
        if findings is None:
            findings = [{"identity": {"publicClass": "sql-injection"},
                         "severity": "high", "cwe": ["CWE-89"],
                         "location": {"path": "app.py", "line": 11},
                         "title": "SQL 注入", "message": "拼接查询"}]
        saved = {
            "find_root": mod._find_mimosa_root,
            "scan": mod._mimosa_quick_scan,
            "run": mod.subprocess.run,
        }
        mod._find_mimosa_root = lambda: "/fake/mimosa"
        mod._mimosa_quick_scan = lambda root, path: (summary, findings)
        os.environ["ZCODE_BRIDGE_REVIEW_LOCK"] = "0"
        return mod, saved

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
        mod, saved = self._patch_common()

        def boom(root, path):
            raise RuntimeError("engine boom")

        mod._mimosa_quick_scan = boom
        zcode_called = {"n": 0}

        def fake_run(*a, **kw):
            zcode_called["n"] += 1
            return _FakeCompletedProcess(0, "OK", "")

        mod.subprocess.run = fake_run
        try:
            result = mod.tool_zcode_security_review({"path": "/tmp"})
        finally:
            self._restore_common(mod, saved)
        self.assertTrue(result.get("isError"))
        self.assertIn("mimosa 扫描失败", result["content"][0]["text"])
        self.assertEqual(zcode_called["n"], 0, "扫描失败不应再调 zcode")

    def test_sr2_happy_path_pipeline(self):
        """SR2: 正常管线 — findings 作附件, zcode 只读复核, 返回提取的 response"""
        mod, saved = self._patch_common()
        captured = {}

        def fake_run(cmd, *a, **kw):
            captured["cmd"] = cmd
            return _FakeCompletedProcess(
                0, json.dumps({"response": "安全报告正文"}, ensure_ascii=False), "")

        mod.subprocess.run = fake_run
        try:
            result = mod.tool_zcode_security_review(
                {"path": "/tmp/proj", "focus": "注入类"})
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

    def test_sr3_findings_tmpfile_cleaned(self):
        """SR3: findings 临时文件用后被清理"""
        mod, saved = self._patch_common()
        captured = {}
        mod.subprocess.run = lambda cmd, *a, **kw: (
            captured.update(cmd=cmd),
            _FakeCompletedProcess(0, "OK", ""))[1]
        try:
            mod.tool_zcode_security_review({"path": "/tmp/proj"})
        finally:
            self._restore_common(mod, saved)
        attach_path = captured["cmd"][captured["cmd"].index("--attach") + 1]
        self.assertFalse(os.path.exists(attach_path), "临时 findings 文件应被清理")


if __name__ == "__main__":
    unittest.main(verbosity=2)
