"""
test_event_translator.py — EventTranslator 翻译逻辑单测

验证事件驱动模式下 EventTranslator 能正确把 zcode session/event 事件
(model.streaming, tool.updated, turn.started/completed) 翻译成 ACP 事件 dict:
  E0  流式文本 (model.streaming text_delta → TextDelta)
  E1  流式推理 (model.streaming reasoning_delta → ReasoningDelta)
  E2  工具新增 (tool.updated scheduled → ToolCallNew)
  E3  工具开始 (tool.updated started → ToolCallUpdate in_progress)
  E4  工具完成 (tool.updated result → ToolCallUpdate completed)
  E5  工具进度 (tool.updated progress → ToolCallUpdate with output)
  E6  turn 生命周期 (turn.started + turn.completed → 状态标记 + UsageDelta)
  E7  turn 失败 (turn.failed → turn_done 标记)
  E8  空 delta 过滤 (model.streaming 无 delta → 无事件)
  E9  batch/忽略类型 (session.updated → 无事件)
  E10 多段流式合并 (多条 text_delta → 多个 TextDelta)

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


def _event(etype, payload=None):
    """构造一个 session/event params dict"""
    return {"type": etype, "payload": payload or {}, "seq": 0, "sessionId": "sess_test"}


class TestEventTranslator(unittest.TestCase):
    """EventTranslator 的事件翻译单测"""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_bridge_module()
        cls.Translator = cls.mod.EventTranslator

    def _new_translator(self):
        return self.Translator()

    # ---------- E0: 流式文本 ----------
    def test_e0_text_delta(self):
        """E0: model.streaming text_delta → TextDelta"""
        t = self._new_translator()
        events = t.translate(_event("model.streaming", {"kind": "text_delta", "delta": "你好"}))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["kind"], "TextDelta")
        self.assertEqual(events[0]["text"], "你好")

    # ---------- E1: 流式推理 ----------
    def test_e1_reasoning_delta(self):
        """E1: model.streaming reasoning_delta → ReasoningDelta"""
        t = self._new_translator()
        events = t.translate(_event("model.streaming", {"kind": "reasoning_delta", "delta": "思考中"}))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["kind"], "ReasoningDelta")
        self.assertEqual(events[0]["text"], "思考中")

    # ---------- E2: 工具新增 ----------
    def test_e2_tool_new(self):
        """E2: tool.updated scheduled → ToolCallNew"""
        t = self._new_translator()
        events = t.translate(_event("tool.updated", {
            "kind": "scheduled", "toolCallId": "call_1", "toolName": "Bash",
        }))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["kind"], "ToolCallNew")
        self.assertEqual(events[0]["call_id"], "call_1")
        self.assertEqual(events[0]["tool"], "Bash")
        self.assertEqual(events[0]["acp_kind"], "execute")
        self.assertEqual(events[0]["status"], "pending")

    def test_e2b_tool_kind_mapping(self):
        """E2b: tool 名 → ACP ToolKind 映射 (Read→read, Edit→edit, Grep→search)"""
        t = self._new_translator()
        for zname, expected in [("Read", "read"), ("Edit", "edit"), ("Grep", "search"),
                                 ("WebSearch", "fetch"), ("Glob", "search")]:
            events = t.translate(_event("tool.updated", {
                "kind": "scheduled", "toolCallId": f"call_{zname}", "toolName": zname,
            }))
            self.assertEqual(events[0]["acp_kind"], expected, f"映射错误: {zname}")

    def test_e2c_tool_dedup(self):
        """E2c: 同一 toolCallId 的 scheduled 不重复发 ToolCallNew"""
        t = self._new_translator()
        payload = {"kind": "scheduled", "toolCallId": "call_dup", "toolName": "Bash"}
        t.translate(_event("tool.updated", payload))
        events2 = t.translate(_event("tool.updated", payload))
        self.assertEqual(len(events2), 0, "重复 scheduled 不应再发 ToolCallNew")

    # ---------- E3: 工具开始 ----------
    def test_e3_tool_started(self):
        """E3: tool.updated started → ToolCallUpdate in_progress"""
        t = self._new_translator()
        t.translate(_event("tool.updated", {
            "kind": "scheduled", "toolCallId": "call_2", "toolName": "Bash",
        }))
        events = t.translate(_event("tool.updated", {
            "kind": "started", "toolCallId": "call_2", "toolName": "Bash",
        }))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["kind"], "ToolCallUpdate")
        self.assertEqual(events[0]["status"], "in_progress")

    # ---------- E4: 工具完成 ----------
    def test_e4_tool_result(self):
        """E4: tool.updated result → ToolCallUpdate completed"""
        t = self._new_translator()
        t.translate(_event("tool.updated", {
            "kind": "scheduled", "toolCallId": "call_3", "toolName": "Read",
        }))
        events = t.translate(_event("tool.updated", {
            "kind": "result", "toolCallId": "call_3", "result": "文件内容...",
        }))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["status"], "completed")
        self.assertEqual(events[0]["output"], "文件内容...")

    # ---------- E5: 工具进度 ----------
    def test_e5_tool_progress(self):
        """E5: tool.updated progress → ToolCallUpdate (含实时输出)"""
        t = self._new_translator()
        t.translate(_event("tool.updated", {
            "kind": "scheduled", "toolCallId": "call_4", "toolName": "Bash",
        }))
        events = t.translate(_event("tool.updated", {
            "kind": "progress", "toolCallId": "call_4", "stdoutTail": "正在运行...",
        }))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["status"], "in_progress")
        self.assertEqual(events[0]["output"], "正在运行...")

    # ---------- E6: turn 生命周期 ----------
    def test_e6_turn_lifecycle(self):
        """E6: turn.started + turn.completed → 状态标记 + UsageDelta"""
        t = self._new_translator()
        self.assertFalse(t.turn_started)

        t.translate(_event("turn.started"))
        self.assertTrue(t.turn_started)
        self.assertFalse(t.turn_done)

        events = t.translate(_event("turn.completed", {
            "resultType": "success", "tokenCount": 100, "usage": {"contextWindow": 200000},
        }))
        self.assertTrue(t.turn_done)
        self.assertEqual(t.turn_result_type, "success")
        # turn.completed 应产出 UsageDelta
        usage_events = [e for e in events if e["kind"] == "UsageDelta"]
        self.assertEqual(len(usage_events), 1)
        self.assertEqual(usage_events[0]["used"], 100)

    # ---------- E7: turn 失败 ----------
    def test_e7_turn_failed(self):
        """E7: turn.failed → turn_done 标记 + result_type"""
        t = self._new_translator()
        t.translate(_event("turn.failed", {"resultType": "error_max_turns"}))
        self.assertTrue(t.turn_done)
        self.assertEqual(t.turn_result_type, "error_max_turns")

    # ---------- E8: 空 delta 过滤 ----------
    def test_e8_empty_delta(self):
        """E8: model.streaming 无 delta → 不产出事件"""
        t = self._new_translator()
        events = t.translate(_event("model.streaming", {"kind": "text_delta", "delta": ""}))
        self.assertEqual(len(events), 0)

    # ---------- E9: 忽略类型 ----------
    def test_e9_ignored_types(self):
        """E9: session.updated/titleUpdated → 不产出事件"""
        t = self._new_translator()
        for etype in ["session.updated", "session.titleUpdated", "streamRecovery.updated"]:
            events = t.translate(_event(etype, {}))
            self.assertEqual(len(events), 0, f"{etype} 不应产出事件")

    # ---------- E10: 多段流式 ----------
    def test_e10_multi_segment_streaming(self):
        """E10: 多条 text_delta → 多个 TextDelta (逐段, 不合并)"""
        t = self._new_translator()
        segments = ["第一段", "第二段", "第三段"]
        all_events = []
        for seg in segments:
            all_events.extend(t.translate(_event("model.streaming", {
                "kind": "text_delta", "delta": seg,
            })))
        self.assertEqual(len(all_events), 3)
        # 验证顺序和内容
        combined = "".join(e["text"] for e in all_events)
        self.assertEqual(combined, "第一段第二段第三段")

    # ---------- E11: 完整 turn 模拟 ----------
    def test_e11_full_turn_sequence(self):
        """E11: 模拟一个完整 turn 的事件序列 (started → text → tool → completed)"""
        t = self._new_translator()
        all_events = []

        # turn 开始
        all_events.extend(t.translate(_event("turn.started")))

        # 流式文本 2 段
        all_events.extend(t.translate(_event("model.streaming", {"kind": "text_delta", "delta": "我来"})))
        all_events.extend(t.translate(_event("model.streaming", {"kind": "text_delta", "delta": "读取文件"})))

        # 工具调用完整序列
        all_events.extend(t.translate(_event("tool.updated", {
            "kind": "scheduled", "toolCallId": "call_full", "toolName": "Read"})))
        all_events.extend(t.translate(_event("tool.updated", {
            "kind": "started", "toolCallId": "call_full", "toolName": "Read"})))
        all_events.extend(t.translate(_event("tool.updated", {
            "kind": "result", "toolCallId": "call_full", "result": "内容"})))

        # turn 完成
        all_events.extend(t.translate(_event("turn.completed", {
            "resultType": "success", "tokenCount": 50, "usage": {"contextWindow": 100000}})))

        # 验证事件序列
        kinds = [e["kind"] for e in all_events]
        self.assertEqual(kinds, [
            "TextDelta", "TextDelta",                      # 2 段流式文本
            "ToolCallNew",                                  # scheduled
            "ToolCallUpdate",                               # started
            "ToolCallUpdate",                               # result
            "UsageDelta",                                   # turn completed usage
        ])
        self.assertTrue(t.turn_done)
        self.assertEqual(t.turn_result_type, "success")

    # ---------- E12: tool error ----------
    def test_e12_tool_error(self):
        """E12: tool.updated error → ToolCallUpdate failed"""
        t = self._new_translator()
        t.translate(_event("tool.updated", {
            "kind": "scheduled", "toolCallId": "call_err", "toolName": "Bash"}))
        events = t.translate(_event("tool.updated", {
            "kind": "error", "toolCallId": "call_err", "error": "命令失败"}))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["status"], "failed")
        self.assertEqual(events[0]["output"], "命令失败")

    # ---------- E13: batch tool completion ----------
    def test_e13_tool_batch(self):
        """E13: tool.updated batch → 为 scheduled 过的 call_id 补发 completed"""
        t = self._new_translator()
        # 两个工具 scheduled
        t.translate(_event("tool.updated", {"kind": "scheduled", "toolCallId": "b1", "toolName": "Read"}))
        t.translate(_event("tool.updated", {"kind": "scheduled", "toolCallId": "b2", "toolName": "Bash"}))
        # batch 事件 (含 toolCallIds)
        events = t.translate(_event("tool.updated", {
            "kind": "batch", "toolCallIds": ["b1", "b2"], "successCount": 2, "errorCount": 0}))
        # 应为两个 scheduled 过的 id 各补发 completed
        self.assertEqual(len(events), 2)
        statuses = {e["call_id"]: e["status"] for e in events}
        self.assertEqual(statuses["b1"], "completed")
        self.assertEqual(statuses["b2"], "completed")

    def test_e13b_batch_with_errors(self):
        """E13b: batch 有 error → 对应 call_id 标记 failed"""
        t = self._new_translator()
        t.translate(_event("tool.updated", {"kind": "scheduled", "toolCallId": "be1", "toolName": "Bash"}))
        events = t.translate(_event("tool.updated", {
            "kind": "batch", "toolCallIds": ["be1"], "successCount": 0, "errorCount": 1}))
        self.assertEqual(events[0]["status"], "failed")

    def test_e13c_batch_ignores_unknown_ids(self):
        """E13c: batch 里的未知 toolCallId (未 scheduled) 不补发"""
        t = self._new_translator()
        t.translate(_event("tool.updated", {"kind": "scheduled", "toolCallId": "known", "toolName": "Read"}))
        events = t.translate(_event("tool.updated", {
            "kind": "batch", "toolCallIds": ["known", "unknown"], "successCount": 2, "errorCount": 0}))
        # 只有 "known" 补发, "unknown" 忽略
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["call_id"], "known")

    # ---------- E14: reader dead fail-fast ----------
    def test_e14_turn_usage_total_tokens(self):
        """E14: turn.completed 的 usage.totalTokens 优先于 tokenCount"""
        t = self._new_translator()
        events = t.translate(_event("turn.completed", {
            "resultType": "success",
            "tokenCount": 50,
            "usage": {"totalTokens": 200, "contextWindow": 100000},
        }))
        usage_events = [e for e in events if e["kind"] == "UsageDelta"]
        self.assertEqual(usage_events[0]["used"], 200)  # totalTokens 优先


if __name__ == "__main__":
    unittest.main(verbosity=2)
