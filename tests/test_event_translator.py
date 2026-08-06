"""
test_event_translator.py — EventTranslator 翻译逻辑单测 (0.16.1 新事件模型)

0.16.1 起事件统一走 session/event 通知, 信封为
{seq, eventId, timestamp, traceId, payload} (规格书 §4 实测)。payload 类型实测到:
  turn.started (turnNumber/input/messageId)
  model.streaming (kind:"text_delta", delta, assistantMessageId) → ACP agent_message_chunk
  session.updated (model/modelRef/iteration)
  turn.completed (response 全文 + usage 完整 token 明细) → turn 结束
  session.titleUpdated
另有 rewind.triggered 与平行的 state.updated / v4/telemetry/event 通知 (桥不消费)。
事实来源: docs/upgrade-0.16.1-spec.md §4; 0.15.0 时代的 tool.* / turn.failed
payload 在 0.16.1 实测中未出现, 旧覆盖随之移除, 待后续实测补充。

  N0  信封元数据不影响翻译 (seq/eventId/timestamp/traceId 变化 → 同输出)
  N1  turn.started (turnNumber/input/messageId) → 无 ACP 事件, 置 turn_started
  N2  model.streaming text_delta → TextDelta (下游映射 ACP agent_message_chunk)
  N3  空 delta 过滤 (无 delta → 无事件)
  N4  model.streaming 未知 kind → 无事件
  N5  多段流式逐段输出, 不合并不乱序
  N6  session.updated (model/modelRef/iteration) → 无事件
  N7  session.titleUpdated → 无事件
  N8  turn.completed 含 usage 完整明细 → turn_done + UsageDelta (used 取 totalTokens)
  N9  turn.completed 缺 usage → turn_done, 无 UsageDelta, 不炸
  N10 完整 turn 序列 (started → 2×text_delta → completed)
  N11 未消费 payload 类型 (rewind.triggered / 未知类型) → 无事件不炸

假设 (规格书未明示, 待复审对齐): payload 判别字段为 "type" —— "kind" 已被
model.streaming 的子类型 (text_delta) 占用; translate() 入参为 session/event
通知的 params 整体 (与旧版 seam 一致)。

运行: python3 tests/test_event_translator.py
依赖: 仅 Python 标准库 + 本项目的 acp-bridge 模块
"""

import os
import types
import unittest

# 把 acp-bridge 模块加载进来 (单文件, 用 exec 导入类定义, 跳过 main)
BRIDGE_PATH = os.path.join(
    os.path.dirname(__file__), "..", "packages", "acp-bridge", "zcode-acp-bridge"
)


def _load_bridge_module():
    """加载 acp-bridge 的类定义 (跳过 if __name__ 块)"""
    mod = types.ModuleType("acp_bridge")
    with open(BRIDGE_PATH) as f:
        code = f.read()
    code_no_main = code.split('if __name__ == "__main__":')[0]
    # 提供 exec 所需的 log 占位 (模块级函数)
    exec(code_no_main, mod.__dict__)
    return mod


def _session_event(payload, seq=1, event_id=None, timestamp=1754460000000,
                   trace_id="trace_test"):
    """构造一条 0.16.1 session/event 通知的 params (规格书 §4 信封)。"""
    return {
        "seq": seq,
        "eventId": event_id or f"evt_{seq}",
        "timestamp": timestamp,
        "traceId": trace_id,
        "payload": payload,
    }


def _turn_started(input_text="你好", turn_number=1):
    """turn.started payload (规格书 §4: turnNumber/input/messageId)"""
    return {"type": "turn.started", "turnNumber": turn_number,
            "input": input_text, "messageId": "msg_1"}


def _text_delta(delta, assistant_message_id="am_1"):
    """model.streaming text_delta payload (规格书 §4: kind/delta/assistantMessageId)"""
    return {"type": "model.streaming", "kind": "text_delta",
            "delta": delta, "assistantMessageId": assistant_message_id}


def _turn_completed(response="完整回复", usage=None):
    """turn.completed payload (规格书 §4: response 全文 + usage 完整 token 明细)。

    usage 明细字段名规格书未逐项列出, 沿用 0.15.0 投影风格假设
    (inputTokens/outputTokens/totalTokens/contextWindow), 待复审对齐。
    """
    if usage is None:
        usage = {"inputTokens": 120, "outputTokens": 80,
                 "totalTokens": 200, "contextWindow": 200000}
    return {"type": "turn.completed", "response": response, "usage": usage}


class TestEventTranslator(unittest.TestCase):
    """EventTranslator 的事件翻译单测 (0.16.1 新 payload)"""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_bridge_module()
        cls.Translator = cls.mod.EventTranslator

    def _new_translator(self):
        return self.Translator()

    # ---------- N0: 信封元数据不影响翻译 ----------
    def test_n0_envelope_metadata_ignored(self):
        """N0: seq/eventId/timestamp/traceId 不同的同 payload → 相同翻译结果"""
        t = self._new_translator()
        p = _text_delta("OK")
        e1 = t.translate(_session_event(p, seq=1, event_id="a", timestamp=1, trace_id="t1"))
        e2 = t.translate(_session_event(p, seq=99, event_id="b", timestamp=2, trace_id="t2"))
        self.assertEqual(e1, e2, "信封元数据不应影响 payload 翻译")
        self.assertEqual(len(e1), 1)

    # ---------- N1: turn.started ----------
    def test_n1_turn_started(self):
        """N1: turn.started → 不产出 ACP 事件, 仅置 turn_started 标记"""
        t = self._new_translator()
        self.assertFalse(t.turn_started)
        events = t.translate(_session_event(_turn_started()))
        self.assertEqual(len(events), 0, "turn.started 不直接产出 ACP 事件")
        self.assertTrue(t.turn_started)
        self.assertFalse(t.turn_done)

    # ---------- N2: 流式文本 ----------
    def test_n2_text_delta(self):
        """N2: model.streaming text_delta → TextDelta (下游映射 agent_message_chunk)"""
        t = self._new_translator()
        events = t.translate(_session_event(_text_delta("OK")))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["kind"], "TextDelta")
        self.assertEqual(events[0]["text"], "OK")

    # ---------- N3: 空 delta 过滤 ----------
    def test_n3_empty_delta(self):
        """N3: text_delta 但 delta 为空串 → 不产出事件"""
        t = self._new_translator()
        events = t.translate(_session_event(_text_delta("")))
        self.assertEqual(len(events), 0)

    # ---------- N4: 未知 streaming kind ----------
    def test_n4_unknown_streaming_kind(self):
        """N4: model.streaming 的未知 kind (0.16.1 实测只有 text_delta) → 无事件"""
        t = self._new_translator()
        payload = {"type": "model.streaming", "kind": "some_future_kind",
                   "delta": "x", "assistantMessageId": "am_1"}
        events = t.translate(_session_event(payload))
        self.assertEqual(len(events), 0, "未知 streaming kind 应被忽略而非误映")

    # ---------- N5: 多段流式 ----------
    def test_n5_multi_segment_streaming(self):
        """N5: 多条 text_delta → 多个 TextDelta (逐段, 不合并不乱序)"""
        t = self._new_translator()
        segments = ["第一段", "第二段", "第三段"]
        all_events = []
        for i, seg in enumerate(segments, start=1):
            all_events.extend(t.translate(_session_event(_text_delta(seg), seq=i)))
        self.assertEqual(len(all_events), 3)
        combined = "".join(e["text"] for e in all_events)
        self.assertEqual(combined, "第一段第二段第三段")

    # ---------- N6: session.updated ----------
    def test_n6_session_updated_ignored(self):
        """N6: session.updated (model/modelRef/iteration) → 不产出事件"""
        t = self._new_translator()
        payload = {"type": "session.updated", "model": "anthropic/GLM-5.2",
                   "modelRef": {"providerId": "anthropic", "modelId": "GLM-5.2"},
                   "iteration": 3}
        events = t.translate(_session_event(payload))
        self.assertEqual(len(events), 0)

    # ---------- N7: session.titleUpdated ----------
    def test_n7_title_updated_ignored(self):
        """N7: session.titleUpdated → 不产出事件"""
        t = self._new_translator()
        events = t.translate(_session_event(
            {"type": "session.titleUpdated", "title": "新标题"}))
        self.assertEqual(len(events), 0)

    # ---------- N8: turn.completed 含 usage ----------
    def test_n8_turn_completed_with_usage(self):
        """N8: turn.completed (response+usage) → turn_done + UsageDelta(used=totalTokens)"""
        t = self._new_translator()
        t.translate(_session_event(_turn_started()))
        events = t.translate(_session_event(_turn_completed()))
        self.assertTrue(t.turn_done)
        usage_events = [e for e in events if e["kind"] == "UsageDelta"]
        self.assertEqual(len(usage_events), 1, "turn.completed 应产出一条 UsageDelta")
        self.assertEqual(usage_events[0]["used"], 200)  # usage.totalTokens

    # ---------- N9: turn.completed 缺 usage ----------
    def test_n9_turn_completed_without_usage(self):
        """N9: turn.completed 缺 usage → 仍置 turn_done, 无 UsageDelta, 不炸"""
        t = self._new_translator()
        payload = {"type": "turn.completed", "response": "半截回复"}
        events = t.translate(_session_event(payload))
        self.assertTrue(t.turn_done)
        usage_events = [e for e in events if e["kind"] == "UsageDelta"]
        self.assertEqual(len(usage_events), 0)

    # ---------- N10: 完整 turn 序列 ----------
    def test_n10_full_turn_sequence(self):
        """N10: started → 2×text_delta → completed(usage) 的事件序列与 ACP 映射"""
        t = self._new_translator()
        all_events = []
        all_events.extend(t.translate(_session_event(_turn_started(), seq=1)))
        all_events.extend(t.translate(_session_event(_text_delta("我来"), seq=2)))
        all_events.extend(t.translate(_session_event(_text_delta("回答"), seq=3)))
        all_events.extend(t.translate(_session_event(_turn_completed(), seq=4)))

        kinds = [e["kind"] for e in all_events]
        self.assertEqual(kinds, [
            "TextDelta", "TextDelta",   # 2 段流式文本 → agent_message_chunk ×2
            "UsageDelta",               # turn.completed usage
        ])
        self.assertTrue(t.turn_started)
        self.assertTrue(t.turn_done)

    # ---------- N11: 未消费 payload 类型 ----------
    def test_n11_unconsumed_types_ignored(self):
        """N11: rewind.triggered (规格书 §2 提及) 与未知类型 → 无事件不炸"""
        t = self._new_translator()
        for payload in [
            {"type": "rewind.triggered", "checkpointId": "cp1"},
            {"type": "turn.steerQueued", "content": "插队"},   # §2 steer 相关事件
            {"type": "unknown.futureType", "foo": 1},
        ]:
            events = t.translate(_session_event(payload))
            self.assertEqual(len(events), 0,
                             f"{payload['type']} 不应产出 ACP 事件")


if __name__ == "__main__":
    unittest.main(verbosity=2)
