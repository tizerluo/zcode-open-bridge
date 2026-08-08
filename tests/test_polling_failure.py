"""
test_polling_failure.py — 轮询降级分支的 turn 失败检测单测 (0.16.1)

0.16.1 事件/轮询两模式: 事件驱动 (session/subscribe + 必传 deliveryKind) 优先,
失败自动降级轮询 (session/read + session/messages 均在 0.16.1 存活, 规格书
§2 存活清单; 降级仅限 ≤0.15 旧协议, v16 subscribe 门禁见
test_app_server_methods.py PM4)。轮询路径不感知 turn 失败: turn.failed 只在
事件通道推送 (reviewer-1 0.16.1 真机捕获, 事件分支的判定见
test_event_translator.py F1/F2), 轮询降级路径收不到事件,
projection/messages 又无失败标志, 故沿用 0.15.0
的修复 (PR#2 遗留 issue #3 子项3d): turn "完成"(idle) 但无任何有效输出
(text/tool/patch) 时判疑似失败, 返回 -32603 而非静默 end_turn。

用增强 FakeBackend (按方法路由响应) + patch time.sleep 模拟 turn 流程, 不真跑
zcode; prompt 全流程用线程限时兜底 (流程卡住时快速失败)。

  PF0 轮询: turn 完成(idle)有正常输出 → end_turn (原有行为不破坏)
  PF1 轮询: turn 完成(idle)但无输出 (疑似失败) → -32603
  PF2 轮询: turn 从未启动 → -32603 "未启动"
  PF3 事件/轮询模式选择 · 轮询分支: subscribe 失败 → 自动降级走
      session/read 轮询 (事件分支见 test_app_server_methods.py EV1)

运行: python3 tests/test_polling_failure.py
依赖: 仅 Python 标准库 + acp-bridge 模块
"""

import json
import os
import threading
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


def _run_with_guard(fn, timeout=20):
    """在线程中执行 fn 并限时取结果; 超时/异常都显式失败 (防流程卡死)。"""
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
        raise AssertionError(f"调用 {timeout}s 内未完成 (实现未对齐或流程卡住)")
    if "exc" in box:
        raise box["exc"]
    return box.get("result")


class _RoutingBackend:
    """按 method 路由响应的 FakeBackend; 记录调用的 method/params。

    responses: {method: response_dict} 或 {method: [resp1, resp2, ...]} (序列)
    """

    def __init__(self, responses):
        self.responses = responses
        self.calls = []
        self._seq_idx = {}

    def request(self, msg_id, method, params=None, timeout=30):
        self.calls.append({"method": method, "params": params or {}})
        resp = self.responses.get(method)
        if isinstance(resp, list):
            i = self._seq_idx.get(method, 0)
            self._seq_idx[method] = i + 1
            resp = resp[i] if i < len(resp) else (resp[-1] if resp else {"result": {}})
        return (resp if resp is not None else {"result": {}}), []

    def send(self, msg):
        pass

    def methods_called(self):
        return [c["method"] for c in self.calls]


def _projection(status, total_tokens=100):
    """session/read 的投影响应 (0.16.1 存活, 结构未见变更 → 沿用旧字段)。"""
    return {"result": {"projection": {"status": status,
                                      "totalTokenCount": total_tokens,
                                      "contextWindow": 1000000}}}


def _messages_with_text(text="你好"):
    """session/messages 响应: 含文本的 assistant 消息 (0.16.1 存活方法)。"""
    return {"result": {"messages": [
        {"info": {"role": "user"}, "parts": [{"type": "text", "text": "hi"}]},
        {"info": {"role": "assistant"}, "parts": [{"type": "text", "text": text}]},
    ], "todos": []}}


class TestPollingFailureDetection(unittest.TestCase):
    """轮询降级分支的 turn 失败检测 (0.16.1)"""

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
        # time.time 每次调用推进 0.6s (≈轮询间隔), 让 120s 超时在 ~200 次瞬时
        # 迭代后触发, 而非真实等待 120s。PF0/PF1 只需 2~3 次迭代就能走完
        # running→idle。
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

    def _run_polling(self, bridge, zcode_sid="sess_test"):
        """直接调 _run_polling_turn (跳过 prompt 前置; seam 已按冻结实现核对)。"""
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
            "session/read": [_projection("running"), _projection("idle")],
            "session/messages": _messages_with_text("你好"),
        })
        resp = self._run_polling(bridge)
        self.assertNotIn("error", resp, "有输出应正常 end_turn")
        self.assertEqual(resp["result"]["stopReason"], "end_turn")

    def test_pf1_polling_failure_no_output(self):
        """PF1: 轮询 turn 完成但无任何输出 (疑似失败) → -32603 (不再静默 end_turn)"""
        bridge = self._new_bridge({
            "session/read": [_projection("running"), _projection("idle")],
            # messages 只有 step-start 半成品 (失败 turn 的典型表现)
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
            "session/read": _projection("idle", total_tokens=0),
            "session/messages": {"result": {"messages": [], "todos": []}},
        })
        resp = self._run_polling(bridge)
        self.assertIn("error", resp)
        self.assertIn("未启动", resp["error"]["message"])

    def test_pf3_subscribe_failure_falls_back_to_polling(self):
        """PF3: 事件/轮询模式选择 · 轮询分支 — subscribe 失败 → 降级 session/read 轮询

        事件分支 (subscribe 带 deliveryKind 成功) 见 test_app_server_methods.py EV1;
        v16 模式下 subscribe 失败不降级 (直接报错) 见同文件 PM4。
        """
        bridge = self._new_bridge({
            # subscribe 被拒 (如旧 server 不认识 deliveryKind) → 降级轮询
            "session/subscribe": {"error": {"code": -32602,
                                            "message": "deliveryKind required"}},
            "session/send": {"result": {"accepted": True, "stateRevision": 1}},
            "session/read": [_projection("running"), _projection("idle")],
            "session/messages": _messages_with_text("降级轮询的答案"),
        })
        bridge.session_map["acp_pf3"] = "sess_pf3"
        req = {"jsonrpc": "2.0", "id": 1, "method": "session/prompt",
               "params": {"sessionId": "acp_pf3", "prompt": "hi"}}
        resp = _run_with_guard(lambda: bridge.handle_acp(req))
        self.assertNotIn("error", resp, "降级轮询成功应正常 end_turn")
        self.assertEqual(resp["result"]["stopReason"], "end_turn")

        called = bridge.backend.methods_called()
        # 降级路径: subscribe (尝试, 带 deliveryKind) → send → read 轮询
        self.assertIn("session/subscribe", called, "应先尝试事件分支 subscribe")
        self.assertIn("session/send", called, "prompt 应经 session/send 发出 (§2 rename)")
        self.assertIn("session/read", called, "subscribe 失败后应降级 session/read 轮询")
        self.assertLess(called.index("session/subscribe"), called.index("session/read"),
                        "应先试事件分支再降级轮询")
        sub_params = next(c["params"] for c in bridge.backend.calls
                          if c["method"] == "session/subscribe")
        self.assertIn("deliveryKind", sub_params,
                      "subscribe 必传 deliveryKind (规格书 §4), 即使本次降级")

    def test_pf4_dead_backend_poll_converges_early(self):
        """PF4 (整体 review P1-3): 连续 20 次 poll 无响应且子进程已死 → 立即 -32603

        此前 consecutive_none 只 log 不收敛, 子进程已死这种确定性故障会让调用方
        空等耗满 120s 预算; 修复后超阈值探测 proc.poll(), 已死立即报错。
        """
        bridge = self._new_bridge({
            # session/read 永远 error → projection 恒 None
            "session/read": {"error": {"message": "connection reset"}},
        })
        # 子进程已退出 (poll() 返回退出码, 非 None)
        bridge.backend.proc = types.SimpleNamespace(poll=lambda: 1)
        resp = self._run_polling(bridge)
        self.assertIn("error", resp)
        self.assertEqual(resp["error"]["code"], -32603)
        self.assertIn("已退出", resp["error"]["message"])
        reads = bridge.backend.methods_called().count("session/read")
        self.assertEqual(reads, 20,
                         "应在第 20 次连续失败时探测到进程已死并提前收敛, 不等 120s")

    def test_pf5_live_backend_no_early_converge(self):
        """PF5 (P1-3 边界): 连续 poll 失败但子进程活着 → 不提前收敛, 走原语义"""
        bridge = self._new_bridge({
            "session/read": {"error": {"message": "timeout"}},
        })
        # 子进程活着 (poll() 返回 None): hang 而非死, 保持等满超时的原行为
        bridge.backend.proc = types.SimpleNamespace(poll=lambda: None)
        resp = self._run_polling(bridge)
        self.assertIn("error", resp)
        self.assertIn("未启动", resp["error"]["message"],
                      "子进程活着时不提前收敛, 耗满 120s 后走「turn 未启动」原语义")

    def test_pf6_fetch_last_reply_drains_inbox_cancel(self):
        """PF6 (整体 review P1-6): _fetch_last_reply 重试间隙 drain inbox

        兜底取回复最多 4 次重试 (间隔 sleep), 此前这期间不 drain, 到达的
        session/cancel 会卡住; 修复后重试间隙 drain, cancel 可生效。
        """
        bridge = self._new_bridge({
            # session/messages 始终 error → 走满 4 次重试
            "session/messages": {"error": {"message": "boom"}},
        })
        zcode_sid = "sess_pf6"
        bridge.session_map[zcode_sid] = zcode_sid
        turn = {"zcode_sid": zcode_sid, "cancelled": False, "perms_responses": {}}
        bridge.pending_turns[99] = turn
        bridge._inbox.put(json.dumps({"method": "session/cancel",
                                      "params": {"sessionId": zcode_sid}}))
        out = bridge._fetch_last_reply(zcode_sid)
        self.assertEqual(out, "", "始终 error → 重试耗尽返回空串")
        self.assertTrue(turn["cancelled"], "重试间隙应 drain inbox 让 cancel 生效")

    def test_pf7_fetch_last_reply_skips_nondict_message(self):
        """PF7 (整体 review P2-9): messages 混入非 dict 元素 → 跳过不炸 (isinstance 守卫)"""
        bridge = self._new_bridge({
            "session/messages": {"result": {"messages": [
                "garbage", 42,
                {"info": {"role": "assistant"},
                 "parts": [{"type": "text", "text": "最终回复"}]},
            ]}},
        })
        out = bridge._fetch_last_reply("sess_pf7")
        self.assertEqual(out, "最终回复", "非 dict 元素跳过, 照常取到最后回复")


if __name__ == "__main__":
    unittest.main(verbosity=2)
