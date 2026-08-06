"""
test_event_translator.py — EventTranslator 翻译逻辑单测 (0.16.1 新事件模型)

0.16.1 起事件统一走 session/event 通知。线缆形态 (reviewer-1 真机抓帧,
对规格书 §4 信封描述的勘误): 判别字段 type 在 params 顶层 (payload 内不含),
且带 deliveryKind 字段; 仅 session.updated 的 payload 内保留 type 作子类型
判别。信封为 {type, deliveryKind, seq, eventId, timestamp, traceId,
sessionId, payload}。payload 类型实测到:
  turn.started (turnNumber/input/messageId)
  model.streaming (kind:"text_delta"/"reasoning_delta", delta, assistantMessageId)
    → ACP agent_message_chunk / 推理增量
  tool.updated (kind:scheduled/started/progress/result/error/batch) → 工具状态
  session.updated (model/modelRef/iteration)
  turn.completed (response 全文 + usage 完整 token 明细) → turn 结束
  turn.failed (error{type,code,message} + turnPhase; 无 resultType → 无条件判失败)
  session.titleUpdated
另有 rewind.triggered 与平行的 state.updated / v4/telemetry/event 通知 (桥不消费)。
事实来源: reviewer-1 0.16.1 真机抓帧 + 实现 EventTranslator (translate /
_translate_streaming / _translate_tool / _translate_turn_done)。

  N0  信封元数据不影响翻译 (seq/eventId/timestamp/traceId 变化 → 同输出)
  N1  turn.started (turnNumber/input/messageId) → 无 ACP 事件, 置 turn_started
  N2  model.streaming text_delta → TextDelta (下游映射 ACP agent_message_chunk)
  N2b model.streaming reasoning_delta → ReasoningDelta
  N3  空 delta 过滤 (无 delta → 无事件)
  N4  model.streaming 未知 kind → 无事件
  N5  多段流式逐段输出, 不合并不乱序
  N6  session.updated (payload.type 只是子类型判别) → 无事件
  N7  session.titleUpdated → 无事件
  N8  turn.completed 含 usage 完整明细 → turn_done + UsageDelta (used 取 totalTokens)
  N9  turn.completed 缺 usage → turn_done, 无 UsageDelta, 不炸
  N10 完整 turn 序列 (started → 2×text_delta → completed)
  N11 未消费 payload 类型 (rewind.triggered / 未知类型) → 无事件不炸
  N12 顶层无 type 时 payload.type 兜底判别 (旧形态兼容分支)

  T1  tool.updated scheduled → ToolCallNew (tool 名 → ACP ToolKind 映射)
  T2  scheduled 去重 (同 toolCallId 不重复发 ToolCallNew)
  T3  tool.updated started → ToolCallUpdate in_progress
  T4  tool.updated progress → ToolCallUpdate 带 stdoutTail/stderrTail 实时输出
  T5  tool.updated result dict 形态 (0.16.1 新: {success,content,perf}) → completed
  T6  tool.updated result dict success:false → failed (output 取 content/error)
  T7  tool.updated result str 形态 (0.15 旧) → completed
  T8  tool.updated error → failed (output 取 payload.error)
  T9  tool.updated batch → 已 scheduled 的 id 补发 completed, 未知 id 不补
  T9b tool.updated batch errorCount>0 → 补发 failed

  F1  turn.failed (reviewer-1 实测载荷) → 无条件置失败标记, 不产 ACP 事件
  F2  turn.failed 端到端: _run_event_turn → -32603, 错误文案取 error.message

translate() 入参为 session/event 通知的 params 整体 (与旧版 seam 一致);
判别顺序为顶层 type 优先、payload.type 兜底 (旧形态兼容)。

运行: python3 tests/test_event_translator.py
依赖: 仅 Python 标准库 + 本项目的 acp-bridge 模块
"""

import os
import threading
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


def _session_event(etype, payload=None, seq=1, event_id=None,
                   timestamp=1754460000000, trace_id="trace_test"):
    """构造一条 0.16.1 session/event 通知的 params (reviewer-1 实测线缆形态)。

    判别字段 type 在 params 顶层 (payload 内不含), 带 deliveryKind 字段;
    信封元数据 (seq/eventId/timestamp/traceId/sessionId) 不影响翻译。
    """
    return {
        "type": etype,
        "deliveryKind": "desktop-continuous",
        "seq": seq,
        "eventId": event_id or f"evt_{seq}",
        "timestamp": timestamp,
        "traceId": trace_id,
        "sessionId": "sess_test",
        "payload": payload or {},
    }


def _turn_started(input_text="你好", turn_number=1):
    """turn.started payload (turnNumber/input/messageId; 事件名在顶层 type)"""
    return {"turnNumber": turn_number, "input": input_text, "messageId": "msg_1"}


def _text_delta(delta, assistant_message_id="am_1"):
    """model.streaming text_delta payload (kind/delta/assistantMessageId)"""
    return {"kind": "text_delta", "delta": delta,
            "assistantMessageId": assistant_message_id}


def _turn_completed(response="完整回复", usage=None):
    """turn.completed payload (response 全文 + usage 完整 token 明细)"""
    if usage is None:
        usage = {"inputTokens": 120, "outputTokens": 80,
                 "totalTokens": 200, "contextWindow": 200000}
    return {"response": response, "usage": usage}


def _tool_updated(kind, call_id="call_1", **extra):
    """tool.updated payload (kind/toolCallId + 各 kind 的附加字段)"""
    return {"kind": kind, "toolCallId": call_id, **extra}


# reviewer-1 0.16.1 真机抓到的 turn.failed 载荷 (provider 未配置触发的真实失败)。
# 注意载荷里没有 resultType 字段 — 失败判定不得依赖 resultType 存在。
TURN_FAILED_PAYLOAD = {
    "error": {"type": "unknown_error", "code": "provider_not_configured",
              "message": "Provider authentication failed."},
    "turnPhase": "processing_input",
}


def _run_with_guard(fn, timeout=15):
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
        e1 = t.translate(_session_event("model.streaming", _text_delta("OK"),
                                        seq=1, event_id="a", timestamp=1, trace_id="t1"))
        e2 = t.translate(_session_event("model.streaming", _text_delta("OK"),
                                        seq=99, event_id="b", timestamp=2, trace_id="t2"))
        self.assertEqual(e1, e2, "信封元数据不应影响 payload 翻译")
        self.assertEqual(len(e1), 1)

    # ---------- N1: turn.started ----------
    def test_n1_turn_started(self):
        """N1: turn.started → 不产出 ACP 事件, 仅置 turn_started 标记"""
        t = self._new_translator()
        self.assertFalse(t.turn_started)
        events = t.translate(_session_event("turn.started", _turn_started()))
        self.assertEqual(len(events), 0, "turn.started 不直接产出 ACP 事件")
        self.assertTrue(t.turn_started)
        self.assertFalse(t.turn_done)

    # ---------- N2: 流式文本 ----------
    def test_n2_text_delta(self):
        """N2: model.streaming text_delta → TextDelta (下游映射 agent_message_chunk)"""
        t = self._new_translator()
        events = t.translate(_session_event("model.streaming", _text_delta("OK")))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["kind"], "TextDelta")
        self.assertEqual(events[0]["text"], "OK")

    # ---------- N2b: 流式推理 ----------
    def test_n2b_reasoning_delta(self):
        """N2b: model.streaming reasoning_delta → ReasoningDelta (0.16.1 实测恢复覆盖)"""
        t = self._new_translator()
        events = t.translate(_session_event("model.streaming", {
            "kind": "reasoning_delta", "delta": "思考一下",
            "assistantMessageId": "am_1"}))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["kind"], "ReasoningDelta")
        self.assertEqual(events[0]["text"], "思考一下")

    # ---------- N3: 空 delta 过滤 ----------
    def test_n3_empty_delta(self):
        """N3: text_delta 但 delta 为空串 → 不产出事件"""
        t = self._new_translator()
        events = t.translate(_session_event("model.streaming", _text_delta("")))
        self.assertEqual(len(events), 0)

    # ---------- N4: 未知 streaming kind ----------
    def test_n4_unknown_streaming_kind(self):
        """N4: model.streaming 的未知 kind (tool_input_delta 等工具输入流) → 无事件"""
        t = self._new_translator()
        events = t.translate(_session_event("model.streaming", {
            "kind": "some_future_kind", "delta": "x", "assistantMessageId": "am_1"}))
        self.assertEqual(len(events), 0, "未知 streaming kind 应被忽略而非误映")

    # ---------- N5: 多段流式 ----------
    def test_n5_multi_segment_streaming(self):
        """N5: 多条 text_delta → 多个 TextDelta (逐段, 不合并不乱序)"""
        t = self._new_translator()
        segments = ["第一段", "第二段", "第三段"]
        all_events = []
        for i, seg in enumerate(segments, start=1):
            all_events.extend(t.translate(
                _session_event("model.streaming", _text_delta(seg), seq=i)))
        self.assertEqual(len(all_events), 3)
        combined = "".join(e["text"] for e in all_events)
        self.assertEqual(combined, "第一段第二段第三段")

    # ---------- N6: session.updated ----------
    def test_n6_session_updated_ignored(self):
        """N6: session.updated → 不产出事件 (其 payload.type 只是子类型判别)"""
        t = self._new_translator()
        events = t.translate(_session_event("session.updated", {
            # 0.16.1 实测: 仅 session.updated 的 payload 内保留 type 作子类型
            # (其余事件类型的 payload 均不含 type), 桥不消费该通知
            "type": "model",
            "model": "anthropic/GLM-5.2",
            "modelRef": {"providerId": "anthropic", "modelId": "GLM-5.2"},
            "iteration": 3}))
        self.assertEqual(len(events), 0)

    # ---------- N7: session.titleUpdated ----------
    def test_n7_title_updated_ignored(self):
        """N7: session.titleUpdated → 不产出事件"""
        t = self._new_translator()
        events = t.translate(_session_event("session.titleUpdated",
                                            {"title": "新标题"}))
        self.assertEqual(len(events), 0)

    # ---------- N8: turn.completed 含 usage ----------
    def test_n8_turn_completed_with_usage(self):
        """N8: turn.completed (response+usage) → turn_done + UsageDelta(used=totalTokens)"""
        t = self._new_translator()
        t.translate(_session_event("turn.started", _turn_started(), seq=1))
        events = t.translate(_session_event("turn.completed", _turn_completed(), seq=2))
        self.assertTrue(t.turn_done)
        usage_events = [e for e in events if e["kind"] == "UsageDelta"]
        self.assertEqual(len(usage_events), 1, "turn.completed 应产出一条 UsageDelta")
        self.assertEqual(usage_events[0]["used"], 200)  # usage.totalTokens

    # ---------- N9: turn.completed 缺 usage ----------
    def test_n9_turn_completed_without_usage(self):
        """N9: turn.completed 缺 usage → 仍置 turn_done, 无 UsageDelta, 不炸"""
        t = self._new_translator()
        events = t.translate(_session_event("turn.completed",
                                            {"response": "半截回复"}))
        self.assertTrue(t.turn_done)
        usage_events = [e for e in events if e["kind"] == "UsageDelta"]
        self.assertEqual(len(usage_events), 0)

    # ---------- N10: 完整 turn 序列 ----------
    def test_n10_full_turn_sequence(self):
        """N10: started → 2×text_delta → completed(usage) 的事件序列与 ACP 映射"""
        t = self._new_translator()
        all_events = []
        all_events.extend(t.translate(_session_event("turn.started", _turn_started(), seq=1)))
        all_events.extend(t.translate(_session_event("model.streaming", _text_delta("我来"), seq=2)))
        all_events.extend(t.translate(_session_event("model.streaming", _text_delta("回答"), seq=3)))
        all_events.extend(t.translate(_session_event("turn.completed", _turn_completed(), seq=4)))

        kinds = [e["kind"] for e in all_events]
        self.assertEqual(kinds, [
            "TextDelta", "TextDelta",   # 2 段流式文本 → agent_message_chunk ×2
            "UsageDelta",               # turn.completed usage
        ])
        self.assertTrue(t.turn_started)
        self.assertTrue(t.turn_done)

    # ---------- N11: 未消费 payload 类型 ----------
    def test_n11_unconsumed_types_ignored(self):
        """N11: rewind.triggered / steer 相关 / 未知类型 → 无事件不炸"""
        t = self._new_translator()
        for etype, payload in [
            ("rewind.triggered", {"checkpointId": "cp1"}),
            ("turn.steerQueued", {"content": "插队"}),
            ("unknown.futureType", {"foo": 1}),
        ]:
            events = t.translate(_session_event(etype, payload))
            self.assertEqual(len(events), 0,
                             f"{etype} 不应产出 ACP 事件")

    # ---------- N12: payload.type 兜底判别 ----------
    def test_n12_payload_type_fallback(self):
        """N12: 顶层无 type 的旧形态事件 → payload.type 兜底判别 (兼容分支)"""
        t = self._new_translator()
        evt = {"seq": 1, "sessionId": "sess_test",
               "payload": {"type": "model.streaming", "kind": "text_delta",
                           "delta": "兜底形态", "assistantMessageId": "am_1"}}
        events = t.translate(evt)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["kind"], "TextDelta")
        self.assertEqual(events[0]["text"], "兜底形态")

    # ---------- T: tool.updated (0.16.1 实测恢复覆盖) ----------
    def test_t1_tool_scheduled_new(self):
        """T1: tool.updated scheduled → ToolCallNew (含 tool → ACP ToolKind 映射)"""
        t = self._new_translator()
        events = t.translate(_session_event("tool.updated", _tool_updated(
            "scheduled", toolName="Bash")))
        self.assertEqual(len(events), 1)
        ev = events[0]
        self.assertEqual(ev["kind"], "ToolCallNew")
        self.assertEqual(ev["call_id"], "call_1")
        self.assertEqual(ev["tool"], "Bash")
        self.assertEqual(ev["acp_kind"], "execute")
        self.assertEqual(ev["status"], "pending")
        # 映射抽验: Read→read / Edit→edit / Grep→search / WebSearch→fetch
        for zname, expected in [("Read", "read"), ("Edit", "edit"),
                                 ("Grep", "search"), ("WebSearch", "fetch")]:
            evs = t.translate(_session_event("tool.updated", _tool_updated(
                "scheduled", call_id=f"call_{zname}", toolName=zname)))
            self.assertEqual(evs[0]["acp_kind"], expected, f"映射错误: {zname}")

    def test_t2_tool_scheduled_dedup(self):
        """T2: 同一 toolCallId 重复 scheduled → 不重复发 ToolCallNew"""
        t = self._new_translator()
        payload = _tool_updated("scheduled", toolName="Bash")
        first = t.translate(_session_event("tool.updated", payload, seq=1))
        second = t.translate(_session_event("tool.updated", payload, seq=2))
        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 0, "重复 scheduled 不应再发 ToolCallNew")

    def test_t3_tool_started(self):
        """T3: tool.updated started → ToolCallUpdate in_progress"""
        t = self._new_translator()
        t.translate(_session_event("tool.updated", _tool_updated(
            "scheduled", toolName="Bash"), seq=1))
        events = t.translate(_session_event("tool.updated", _tool_updated(
            "started"), seq=2))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["kind"], "ToolCallUpdate")
        self.assertEqual(events[0]["call_id"], "call_1")
        self.assertEqual(events[0]["status"], "in_progress")

    def test_t4_tool_progress_output(self):
        """T4: tool.updated progress → ToolCallUpdate 带 stdoutTail/stderrTail 输出"""
        t = self._new_translator()
        events = t.translate(_session_event("tool.updated", _tool_updated(
            "progress", stdoutTail="编译中..."), seq=1))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["status"], "in_progress")
        self.assertEqual(events[0]["output"], "编译中...")
        # stdoutTail 为空时取 stderrTail
        events = t.translate(_session_event("tool.updated", _tool_updated(
            "progress", stdoutTail="", stderrTail="warning x"), seq=2))
        self.assertEqual(events[0]["output"], "warning x")

    def test_t5_tool_result_dict_success(self):
        """T5: result dict 形态 (0.16.1 新 {success,content,perf}) → completed, output=content"""
        t = self._new_translator()
        t.translate(_session_event("tool.updated", _tool_updated(
            "scheduled", toolName="Read"), seq=1))
        events = t.translate(_session_event("tool.updated", _tool_updated(
            "result", result={"success": True, "content": "文件内容...",
                              "perf": {"durationMs": 12}}), seq=2))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["kind"], "ToolCallUpdate")
        self.assertEqual(events[0]["status"], "completed")
        self.assertEqual(events[0]["output"], "文件内容...")

    def test_t6_tool_result_dict_failure(self):
        """T6: result dict success:false → failed, output 取 content/error"""
        t = self._new_translator()
        events = t.translate(_session_event("tool.updated", _tool_updated(
            "result", result={"success": False, "content": "",
                              "error": "exit code 1",
                              "perf": {"durationMs": 3}})))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["status"], "failed")
        self.assertEqual(events[0]["output"], "exit code 1")

    def test_t7_tool_result_str_legacy(self):
        """T7: result str 形态 (0.15 旧) → completed, output 原样"""
        t = self._new_translator()
        events = t.translate(_session_event("tool.updated", _tool_updated(
            "result", result="字符串输出")))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["status"], "completed")
        self.assertEqual(events[0]["output"], "字符串输出")

    def test_t8_tool_error(self):
        """T8: tool.updated error → ToolCallUpdate failed (output 取 payload.error)"""
        t = self._new_translator()
        events = t.translate(_session_event("tool.updated", _tool_updated(
            "error", error="tool crashed")))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["status"], "failed")
        self.assertEqual(events[0]["output"], "tool crashed")

    def test_t9_tool_batch(self):
        """T9: batch → 已 scheduled 的 id 补发 completed; 未 scheduled 的 id 不补 (防幽灵事件)"""
        t = self._new_translator()
        for cid in ("call_a", "call_b"):
            t.translate(_session_event("tool.updated", _tool_updated(
                "scheduled", call_id=cid, toolName="Bash")))
        events = t.translate(_session_event("tool.updated", {
            "kind": "batch", "toolCallIds": ["call_a", "call_b", "call_ghost"],
            "successCount": 2, "errorCount": 0}))
        by_id = {e["call_id"]: e for e in events}
        self.assertEqual(set(by_id), {"call_a", "call_b"},
                         "batch 只补发 scheduled 过的 id")
        self.assertTrue(all(e["status"] == "completed" for e in events))

    def test_t9b_tool_batch_with_errors(self):
        """T9b: batch errorCount>0 → 补发 failed"""
        t = self._new_translator()
        t.translate(_session_event("tool.updated", _tool_updated(
            "scheduled", call_id="call_a", toolName="Bash"), seq=1))
        events = t.translate(_session_event("tool.updated", {
            "kind": "batch", "toolCallIds": ["call_a"],
            "successCount": 0, "errorCount": 1}, seq=2))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["status"], "failed")

    # ---------- F: turn.failed (reviewer-1 实测载荷恢复覆盖) ----------
    def test_f1_turn_failed_marks_failure(self):
        """F1: turn.failed → 无条件置失败标记 (不依赖 resultType), 文案取 error.message

        reviewer-1 真机载荷无 resultType 字段; 实现对 turn.failed 无条件
        turn_done + turn_failed 标记, 错误文案从 payload.error 提取
        (message 优先), 不做 resultType 模式匹配。
        """
        t = self._new_translator()
        self.assertFalse(t.turn_done)
        events = t.translate(_session_event("turn.failed", dict(TURN_FAILED_PAYLOAD)))
        self.assertEqual(events, [], "turn.failed 不直接产出 ACP 事件")
        self.assertTrue(t.turn_done, "turn.failed 必须结束 turn")
        self.assertTrue(t.turn_failed, "turn.failed 无条件判失败 (无 resultType 也成立)")
        self.assertIsNone(t.turn_result_type,
                          "resultType 仅 turn.completed 携带, turn.failed 不得编造")
        self.assertEqual(t.turn_error, "Provider authentication failed.",
                         "失败文案应取 payload 的 error.message")

    def test_f2_turn_failed_end_to_end_error(self):
        """F2: turn.failed 端到端 → prompt 返 -32603, 错误文案取 payload error.message"""
        mod = self.mod

        class _FakeBackend:
            """最小桩: turn.failed 在 turn_done 判定即返回, 触不到 request"""

            def request(self, msg_id, method, params=None, timeout=30):
                return {"result": {}}, []

            def send(self, msg):
                pass

        bridge = mod.ACPBridge()
        bridge.backend = _FakeBackend()
        acp_sid = zcode_sid = "sess_f2"
        bridge.session_map[acp_sid] = zcode_sid
        msg_id = 1
        turn = {"zcode_sid": zcode_sid, "cancelled": False, "perms_responses": {}}
        bridge.pending_turns[msg_id] = turn
        listener = mod.EventStreamListener(bridge.backend, zcode_sid)
        listener.handle_event(_session_event("turn.failed", dict(TURN_FAILED_PAYLOAD)))
        differ = bridge._get_or_create_differ(zcode_sid)
        resp = _run_with_guard(lambda: bridge._run_event_turn(
            listener, acp_sid, zcode_sid, msg_id, turn,
            chunk_msg_id="chunk_f2", differ=differ))
        self.assertIn("error", resp, "turn.failed 必须判失败, 不得静默 end_turn")
        self.assertEqual(resp["error"]["code"], -32603)
        self.assertIn("Provider authentication failed.", resp["error"]["message"],
                      "失败文案应取 payload 的 error.message (实测载荷无 resultType)")


if __name__ == "__main__":
    unittest.main(verbosity=2)
