"""
test_prompt_enhance.py — prompt/enhance* 删除后的 -32601 降级单测 (0.16.1)

0.16.1 起 prompt/enhance、prompt/enhance/start、prompt/enhance/cancel 三个方法
从 app-server bundle 消失 (规格书 §2, 字符串级实测, 无替代)。ACP 侧再调这些
方法时, bridge 应把 zcode 的 -32601 Method not found 映射为「该 ZCode 版本不
支持此能力」(规格书 §7) —— 而不是旧版 (App 3.3.0 时代) 的异步 job 等待、
-32603 透传或 120s 空等。旧版的异步路由/超时测试 (PE8~PE18) 随方法删除整体
作废, 本文件重写为降级行为测试。

  G1  prompt/enhance        → -32601 + 「不支持」文案
  G2  prompt/enhance/start  → -32601 + 「不支持」文案, 且不得进入异步等待
  G3  prompt/enhance/cancel → -32601 + 「不支持」文案
  G4  缺参调用同样报能力缺失 (-32601, 不得退化成 -32602 参数错误)
  G5  降级响应不得是 -32603 (README 旧描述作废, 规格书 §7)

事实来源: docs/upgrade-0.16.1-spec.md §2/§7。短路与透传映射两条实现路径都
接受, 测试只断言最终 ACP 响应。

运行: python3 tests/test_prompt_enhance.py
依赖: 仅 Python 标准库 + 本项目的 acp-bridge 模块
"""

import os
import threading
import types
import unittest

BRIDGE_PATH = os.path.join(
    os.path.dirname(__file__), "..", "packages", "acp-bridge", "zcode-acp-bridge"
)

# 0.16.1 已删除的三个 prompt/enhance 方法 (规格书 §2)
DELETED_ENHANCE_METHODS = (
    "prompt/enhance",
    "prompt/enhance/start",
    "prompt/enhance/cancel",
)


def _load_bridge_module():
    """加载 acp-bridge 的类定义 (跳过 if __name__ 块)"""
    mod = types.ModuleType("acp_bridge")
    with open(BRIDGE_PATH) as f:
        code = f.read()
    code_no_main = code.split('if __name__ == "__main__":')[0]
    exec(code_no_main, mod.__dict__)
    return mod


def _run_with_guard(fn, timeout=5):
    """在线程中执行 fn 并限时取结果; 超时即失败。

    G2 用: 若 bridge 仍残留旧版异步 job 等待 (旧实现最长 120s), 限时快速
    失败而不是卡死套件。
    """
    box = {}

    def _run():
        try:
            box["result"] = fn()
        except Exception as exc:
            box["exc"] = exc

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise AssertionError(
            f"调用 {timeout}s 内未完成 —— 疑似仍走旧版异步等待, 0.16.1 已删除")
    if "exc" in box:
        raise box["exc"]
    return box.get("result")


class FakeBackend:
    """替代真实 ZCodeBackend: 按方法脚本化响应, 记录调用。

    对已删方法统一脚本化返回 zcode 侧实测响应
    {"error": {"code": -32601, "message": "Method not found"}} (规格书 §2/§7)。
    """

    def __init__(self):
        self.calls = []
        self.sent = []

    def request(self, msg_id, method, params=None, timeout=30):
        self.calls.append({
            "id": msg_id, "method": method,
            "params": params or {}, "timeout": timeout,
        })
        if method in DELETED_ENHANCE_METHODS:
            return {"error": {"code": -32601, "message": "Method not found"}}, []
        return {"result": {"ok": True}}, []

    def send(self, msg):
        self.sent.append(msg)


class TestPromptEnhanceDeleted(unittest.TestCase):
    """prompt/enhance* (0.16.1 已删除) 的 -32601 降级行为单测"""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_bridge_module()
        cls.Bridge = cls.mod.ACPBridge

    def _new_bridge(self):
        b = self.Bridge()
        fake = FakeBackend()
        b.backend = fake
        return b, fake

    def _call(self, bridge, method, params=None, msg_id=1):
        req = {"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params or {}}
        return bridge.handle_acp(req)

    def _assert_friendly_32601(self, resp, method):
        self.assertEqual(resp.get("error", {}).get("code"), -32601,
                         f"{method} 应返回 -32601, 实际: {resp}")
        self.assertIn("不支持", resp["error"]["message"],
                      f"{method} 的 -32601 应映射为「该 ZCode 版本不支持此能力」"
                      f"文案 (规格书 §7), 实际: {resp['error']['message']}")

    def test_g1_enhance_sync_deleted(self):
        """G1: prompt/enhance → -32601 + 不支持文案"""
        bridge, _ = self._new_bridge()
        resp = self._call(bridge, "prompt/enhance",
                          {"workspacePath": "/p", "prompt": "写个函数"})
        self._assert_friendly_32601(resp, "prompt/enhance")

    def test_g2_enhance_start_deleted_no_async_wait(self):
        """G2: prompt/enhance/start → -32601, 不得进入旧版异步 job 等待"""
        bridge, _ = self._new_bridge()
        resp = _run_with_guard(lambda: self._call(
            bridge, "prompt/enhance/start",
            {"workspacePath": "/p", "prompt": "x", "requestId": "r1"}))
        self._assert_friendly_32601(resp, "prompt/enhance/start")

    def test_g3_enhance_cancel_deleted(self):
        """G3: prompt/enhance/cancel → -32601 + 不支持文案"""
        bridge, _ = self._new_bridge()
        resp = self._call(bridge, "prompt/enhance/cancel", {"requestId": "r1"})
        self._assert_friendly_32601(resp, "prompt/enhance/cancel")

    def test_g4_deleted_missing_params_still_32601(self):
        """G4: 缺参调用已删方法也报能力缺失 (-32601), 不退化成 -32602"""
        for m in DELETED_ENHANCE_METHODS:
            with self.subTest(method=m):
                bridge, _ = self._new_bridge()
                resp = self._call(bridge, m, {})
                self.assertEqual(resp.get("error", {}).get("code"), -32601,
                                 f"{m} 缺参也应报 -32601 能力缺失, 实际: {resp}")

    def test_g5_deleted_not_32603(self):
        """G5: 已删方法不得透传为 -32603 (README 旧描述作废, 规格书 §7)"""
        for m in DELETED_ENHANCE_METHODS:
            with self.subTest(method=m):
                bridge, _ = self._new_bridge()
                resp = self._call(bridge, m,
                                  {"workspacePath": "/p", "prompt": "x",
                                   "requestId": "r1"})
                code = resp.get("error", {}).get("code")
                self.assertNotEqual(code, -32603,
                                    f"{m} 的 -32601 不得被吞成 -32603")


if __name__ == "__main__":
    unittest.main(verbosity=2)
