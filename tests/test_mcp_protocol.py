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
        """MR2: 未知 method → -32601 (zcode auto 探测的 legacy 回落信号, 勿改)"""
        responses = self._run_main([
            json.dumps({"jsonrpc": "2.0", "id": 9, "method": "server/discover"}),
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
