"""
test_mcp_protocol.py — MCP 协议层单测 (2025-11-25 对齐, 2026-08-08)

覆盖:
  - initialize 版本协商: 支持列表内回显 / 列表外回最高版 / 无 params 回最高版
  - 主循环拒收非对象消息: batch 数组 (-32600) / 裸值 (-32600)
  - 未知 method 回 -32601 (zcode auto 探测依赖的 legacy 回落信号, 勿改)
  - tools/list: 四个 tool 带 title + annotations, 顺序稳定

运行: python3 tests/test_mcp_protocol.py
依赖: 仅 Python 标准库 + zcode-mcp-server 模块
"""

import io
import json
import os
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


class TestVersionNegotiation(unittest.TestCase):
    """initialize 版本协商 (legacy lifecycle 规则)"""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_mcp_module()

    def _initialize(self, requested=None):
        params = {} if requested is None else {"protocolVersion": requested}
        return self.mod.handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": params})

    def test_vn0_supported_version_echoed(self):
        """VN0: 请求支持列表内的版本 → 原样回显"""
        for v in ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"):
            resp = self._initialize(v)
            self.assertEqual(resp["result"]["protocolVersion"], v,
                             f"请求 {v} 应回显")

    def test_vn1_unsupported_falls_back_to_latest(self):
        """VN1: 请求不认识的版本 → 回我们的最高版"""
        resp = self._initialize("1999-01-01")
        self.assertEqual(resp["result"]["protocolVersion"], "2025-11-25")

    def test_vn2_no_params_returns_latest(self):
        """VN2: 无 params → 回最高版"""
        resp = self._initialize(None)
        self.assertEqual(resp["result"]["protocolVersion"], "2025-11-25")

    def test_vn3_future_version_falls_back(self):
        """VN3: 请求比我们还新的版本 → 回我们的最高版 (client 自行决定断开)"""
        resp = self._initialize("2026-07-28")
        self.assertEqual(resp["result"]["protocolVersion"], "2025-11-25")


class _MainLoopCase(unittest.TestCase):
    """驱动 main() 主循环: 喂 stdin 行, 收 stdout 响应。"""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_mcp_module()

    def _run_main(self, lines):
        mod = self.mod
        saved_stdin, saved_stdout = sys.stdin, sys.stdout
        sys.stdin = io.StringIO("".join(item + "\n" for item in lines))
        out = io.StringIO()
        sys.stdout = out
        try:
            mod.main()
        finally:
            sys.stdin, sys.stdout = saved_stdin, saved_stdout
        return [json.loads(x) for x in out.getvalue().splitlines() if x.strip()]


class TestMainLoopRejection(_MainLoopCase):
    """非对象消息的拒收行为"""

    def test_mr0_batch_array_rejected(self):
        """MR0: JSON-RPC batch 数组 → -32600 (2025-06-18 起 server 必须拒绝)"""
        responses = self._run_main([
            json.dumps([{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}]),
        ])
        self.assertEqual(len(responses), 1)
        self.assertEqual(responses[0]["error"]["code"], -32600)

    def test_mr1_bare_value_rejected(self):
        """MR1: 裸值 (非 dict) → -32600"""
        responses = self._run_main(["42"])
        self.assertEqual(len(responses), 1)
        self.assertEqual(responses[0]["error"]["code"], -32600)

    def test_mr2_unknown_method_32601(self):
        """MR2: 未知 method → -32601 (zcode auto 探测的 legacy 回落信号, 勿改。
        注: server/discover 自 1.4.0 起正常应答, 不再属于'未知 method')"""
        responses = self._run_main([
            json.dumps({"jsonrpc": "2.0", "id": 9, "method": "bogus/method"}),
        ])
        self.assertEqual(responses[0]["error"]["code"], -32601)

    def test_mr3_malformed_json_32700(self):
        """MR3: 非法 JSON → -32700 Parse error"""
        responses = self._run_main(["{not json"])
        self.assertEqual(responses[0]["error"]["code"], -32700)

    def test_mr4_normal_flow_unaffected(self):
        """MR4: 正常 initialize + tools/list 流程不受拒收逻辑影响"""
        responses = self._run_main([
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                        "params": {"protocolVersion": "2025-11-25"}}),
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
        ])
        self.assertEqual(len(responses), 2, "notification 不应有响应")
        self.assertEqual(responses[0]["result"]["protocolVersion"], "2025-11-25")
        self.assertEqual(len(responses[1]["result"]["tools"]), 4)


class TestToolsListShape(_MainLoopCase):
    """tools/list 的元数据与确定性"""

    def test_tl0_all_tools_have_title_and_annotations(self):
        """TL0: 每个 tool 都有 title + annotations 五要素 (2025-03-26/06-18 增量)"""
        for t in self.mod.TOOLS:
            self.assertIn("title", t, f"{t['name']} 缺 title")
            ann = t.get("annotations", {})
            for key in ("title", "readOnlyHint", "destructiveHint",
                        "idempotentHint", "openWorldHint"):
                self.assertIn(key, ann, f"{t['name']} annotations 缺 {key}")

    def test_tl1_review_tools_read_only(self):
        """TL1: 三个 review tool 必须标 readOnlyHint (只读是卖点, 标错即事故)"""
        for t in self.mod.TOOLS:
            if t["name"].startswith("zcode_"):
                self.assertTrue(t["annotations"]["readOnlyHint"],
                                f"{t['name']} readOnlyHint 应为 True")
                self.assertFalse(t["annotations"]["destructiveHint"])

    def test_tl2_order_deterministic(self):
        """TL2: 两次 tools/list 响应顺序一致 (走完整主循环路径, 2025-11-25 SHOULD 级)"""
        req = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        r1 = self._run_main([req])
        r2 = self._run_main([req])
        n1 = [t["name"] for t in r1[0]["result"]["tools"]]
        n2 = [t["name"] for t in r2[0]["result"]["tools"]]
        self.assertEqual(n1, n2)
        self.assertEqual(len(n1), len(set(n1)), "tool 名不得重复")

    def test_tl3_tool_naming_convention(self):
        """TL3: tool 名符合 2025-11-25 命名规范 (1-128 字符, A-Za-z0-9_-.)"""
        for t in self.mod.TOOLS:
            self.assertRegex(t["name"], r"^[A-Za-z0-9_\-.]{1,128}$",
                             f"{t['name']} 不符合命名规范")


def _modern_req(method, rid=1, params=None, version="2026-07-28"):
    """构造一条 modern 纪元请求 (带 _meta 信封)。"""
    p = dict(params or {})
    p["_meta"] = {
        "io.modelcontextprotocol/protocolVersion": version,
        "io.modelcontextprotocol/clientCapabilities": {},
    }
    return json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": p})


class TestModernEra(_MainLoopCase):
    """2026-07-28 新纪元 (dual-era) 行为"""

    def test_de0_discover_probe(self):
        """DE0: server/discover 探针 → supportedVersions 含 2026-07-28, 带缓存提示"""
        responses = self._run_main([_modern_req("server/discover")])
        result = responses[0]["result"]
        self.assertIn("2026-07-28", result["supportedVersions"])
        self.assertIn("2024-11-05", result["supportedVersions"])
        self.assertEqual(result["resultType"], "complete")
        self.assertIn("ttlMs", result)
        self.assertEqual(result["cacheScope"], "public")
        self.assertIn("io.modelcontextprotocol/serverInfo", result["_meta"])

    def test_de1_modern_tools_list(self):
        """DE1: modern tools/list → resultType + ttlMs/cacheScope + serverInfo 戳"""
        responses = self._run_main([_modern_req("tools/list")])
        result = responses[0]["result"]
        self.assertEqual(len(result["tools"]), 4)
        self.assertEqual(result["resultType"], "complete")
        self.assertIn("ttlMs", result)
        self.assertIn("cacheScope", result)

    def test_de2_modern_tools_call(self):
        """DE2: modern tools/call → 结果带 resultType 与 serverInfo 戳"""
        responses = self._run_main([_modern_req(
            "tools/call",
            params={"name": "get_zcode_capabilities", "arguments": {"section": "ecosystem"}})])
        result = responses[0]["result"]
        self.assertEqual(result["resultType"], "complete")
        self.assertIn("zcode-mcp-server", result["content"][0]["text"])
        self.assertIn("io.modelcontextprotocol/serverInfo", result["_meta"])

    def test_de3_missing_envelope_32602(self):
        """DE3: 已进入 modern 后缺信封的请求 → -32602"""
        responses = self._run_main([
            _modern_req("server/discover"),          # 不定纪元
            _modern_req("tools/list"),               # 定 modern
            json.dumps({"jsonrpc": "2.0", "id": 7,   # 无信封
                        "method": "tools/list", "params": {}}),
        ])
        self.assertEqual(responses[2]["error"]["code"], -32602)

    def test_de4_unsupported_version_32022(self):
        """DE4: modern 信封里版本不认识 → -32022 带 supported/requested"""
        responses = self._run_main([_modern_req("tools/list", version="2099-01-01")])
        err = responses[0]["error"]
        self.assertEqual(err["code"], -32022)
        self.assertIn("2026-07-28", err["data"]["supported"])
        self.assertEqual(err["data"]["requested"], "2099-01-01")

    def test_de5_probe_then_legacy_initialize(self):
        """DE5: 先探 discover 再走 legacy initialize → 两纪元并存 (dual-era 灵活性)"""
        responses = self._run_main([
            _modern_req("server/discover"),
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "initialize",
                        "params": {"protocolVersion": "2025-11-25"}}),
            json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/list"}),
        ])
        self.assertIn("supportedVersions", responses[0]["result"])
        self.assertEqual(responses[1]["result"]["protocolVersion"], "2025-11-25")
        # legacy tools/list 不带 resultType (老 client 预期旧形状)
        self.assertNotIn("resultType", responses[2]["result"])

    def test_de6_pure_legacy_flow_unaffected(self):
        """DE6: 纯 legacy 流程 (initialize 开场) 完全不受 modern 影响"""
        responses = self._run_main([
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                        "params": {"protocolVersion": "2025-06-18"}}),
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
        ])
        self.assertEqual(responses[0]["result"]["protocolVersion"], "2025-06-18")
        self.assertNotIn("resultType", responses[1]["result"])
        self.assertNotIn("ttlMs", responses[1]["result"])

    def test_de7_modern_unknown_method_32601(self):
        """DE7: modern 纪元未知 method → -32601"""
        responses = self._run_main([_modern_req("resources/list")])
        self.assertEqual(responses[0]["error"]["code"], -32601)

    def test_de8_modern_notification_silent(self):
        """DE8: modern notification (cancelled) 静默吞掉不应答"""
        responses = self._run_main([
            _modern_req("tools/list"),
            json.dumps({"jsonrpc": "2.0", "method": "notifications/cancelled",
                        "params": {"requestId": 1, "reason": "user"}}),
        ])
        self.assertEqual(len(responses), 1, "notification 不应有响应")


if __name__ == "__main__":
    unittest.main(verbosity=2)
