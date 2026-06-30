"""
test_polling_failure.py — 轮询/停滞收尾的 turn 失败检测单测

修复 PR#2 遗留 (issue #3 子项3d): 轮询路径 _run_polling_turn 和事件路径停滞收尾
都不感知 turn.failed (zcode 只在事件 payload 给 resultType, projection/messages 无失败标志,
实测确认)。修复方式: turn "完成"(idle)但无任何有效输出 (text/tool/patch) 时, 疑似失败,
返回 -32603 而非静默 end_turn。

用增强 FakeBackend (按方法路由响应) + patch time.sleep 模拟 turn 流程, 不真跑 zcode。

  PF0 轮询: turn 完成(idle)有正常输出 → end_turn (原有行为不破坏)
  PF1 轮询: turn 完成(idle)但无输出 (失败) → -32603 (修复后)
  PF2 轮询: turn 从未启动 → -32603 "未启动"

运行: python3 tests/test_polling_failure.py
依赖: 仅 Python 标准库 + acp-bridge 模块
"""

import os
import types
import unittest

BRIDGE_PATH = os.path.join(
    os.path.dirname(__file__), "..", "packages", "acp-bridge", "zcode-acp-bridge"
)


def _load_bridge_module():
    mod = types.ModuleType("acp_bridge")
    with open(BRIDGE_PATH) as f:
        code = f.read()
    code_no_main = code.split('if __name__ == "__main__":')[0]
    exec(code_no_main, mod.__dict__)
    return mod


class _RoutingBackend:
    """按 method 路由响应的 FakeBackend; 可记录调用次数。

    responses: {method: response_dict} 或 {method: [resp1, resp2, ...]} (序列)
    """

    def __init__(self, responses):
        self.responses = responses
        self.calls = []
        self._seq_idx = {}

    def request(self, msg_id, method, params=None, timeout=30):
        self.calls.append(method)
        resp = self.responses.get(method)
        if isinstance(resp, list):
            i = self._seq_idx.get(method, 0)
            self._seq_idx[method] = i + 1
            return (resp[i] if i < len(resp) else (resp[-1] if resp else {"result": {}})), []
        return (resp if resp is not None else {"result": {}}), []

    def send(self, msg):
        pass


class TestPollingFailureDetection(unittest.TestCase):
    """轮询/停滞收尾的 turn 失败检测 (子项3d)"""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_bridge_module()
        cls.Bridge = cls.mod.ACPBridge
        # bridge 用 `import time; time.sleep(...)` 和 `time.time()`。
        # patch 全局 time.sleep 为 no-op + time.time 快速推进, 否则轮询循环
        # 的 120s 超时会真跑满 (测试会卡 120s/用例)。
        import time as _time_mod
        cls._real_sleep = _time_mod.sleep
        cls._real_time = _time_mod.time
        _t = [0.0]
        _time_mod.sleep = lambda _s: None
        # time.time 每次调用推进 0.6s (≈轮询间隔), 让 120s 超时在 ~200 次瞬时迭代后触发,
        # 而非真实等待 120s。PF0/PF1 只需 2~3 次迭代就能走完 running→idle。
        _time_mod.time = lambda: (_t.__setitem__(0, _t[0] + 0.6), _t[0])[1]

    @classmethod
    def tearDownClass(cls):
        import time as _time_mod
        _time_mod.sleep = cls._real_sleep
        _time_mod.time = cls._real_time

    def _new_bridge(self, responses):
        b = self.Bridge()
        b.backend = _RoutingBackend(responses)
        return b

    def _run_polling(self, bridge, zcode_sid="sess_test", projection_seq=None,
                     messages=None):
        """直接调 _run_polling_turn (跳过 _on_session_prompt 的前置)。"""
        acp_sid = zcode_sid
        bridge.session_map[acp_sid] = zcode_sid
        msg_id = 1
        turn = {"zcode_sid": zcode_sid, "cancelled": False, "perms_responses": {}}
        bridge.pending_turns[msg_id] = turn
        differ = bridge._get_or_create_differ(zcode_sid)
        return bridge._run_polling_turn(acp_sid, zcode_sid, msg_id, turn,
                                        chunk_msg_id="chunk_1", differ=differ)

    def test_pf0_polling_success_with_output(self):
        """PF0: 轮询 turn 完成且有文本输出 → 正常 end_turn (原有行为不破坏)"""
        bridge = self._new_bridge({
            # session/read: 先 running 再 idle (poll_once 调 read)
            "session/read": [
                {"result": {"projection": {"status": "running", "totalTokenCount": 100,
                                           "contextWindow": 1000000}}},
                {"result": {"projection": {"status": "idle", "totalTokenCount": 100,
                                           "contextWindow": 1000000}}},
            ],
            # session/messages: 返回有文本的 assistant 消息
            "session/messages": {"result": {"messages": [
                {"info": {"role": "user"}, "parts": [{"type": "text", "text": "hi"}]},
                {"info": {"role": "assistant"}, "parts": [{"type": "text", "text": "你好"}]},
            ], "todos": []}},
        })
        resp = self._run_polling(bridge)
        self.assertNotIn("error", resp, "有输出应正常 end_turn")
        self.assertEqual(resp["result"]["stopReason"], "end_turn")

    def test_pf1_polling_failure_no_output(self):
        """PF1: 轮询 turn 完成但无任何输出 (失败) → -32603 (修复后不再静默 end_turn)"""
        bridge = self._new_bridge({
            "session/read": [
                {"result": {"projection": {"status": "running", "totalTokenCount": 100,
                                           "contextWindow": 1000000}}},
                {"result": {"projection": {"status": "idle", "totalTokenCount": 100,
                                           "contextWindow": 1000000}}},
            ],
            # messages 只有 step-start 半成品 (失败 turn 的典型表现, 实测确认)
            "session/messages": {"result": {"messages": [
                {"info": {"role": "user"}, "parts": [{"type": "text", "text": "hi"}]},
                {"info": {"role": "assistant"}, "parts": [{"type": "step-start"}]},
            ], "todos": []}},
        })
        resp = self._run_polling(bridge)
        self.assertIn("error", resp, "无输出应判定疑似失败, 返回 error")
        self.assertEqual(resp["error"]["code"], -32603)
        self.assertIn("无输出", resp["error"]["message"])

    def test_pf2_polling_turn_never_started(self):
        """PF2: turn 从未启动 (status 一直非 running) → -32603 '未启动'"""
        bridge = self._new_bridge({
            # status 始终 idle (turn 没启动过)
            "session/read": {"result": {"projection": {"status": "idle",
                                                       "totalTokenCount": 0,
                                                       "contextWindow": 1000000}}},
            "session/messages": {"result": {"messages": [], "todos": []}},
        })
        resp = self._run_polling(bridge)
        self.assertIn("error", resp)
        self.assertIn("未启动", resp["error"]["message"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
