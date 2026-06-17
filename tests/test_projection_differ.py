"""
test_projection_differ.py — ProjectionDiffer 转换逻辑单测

验证 ACP bridge 的快照对比器 (ProjectionDiffer) 能正确把 zcode 的
messages/projection/todos 数据转换成 ACP 事件:
  P0  工具调用 (ToolCallNew)
  P1a usage (UsageDelta)
  P2  流式文本 (TextDelta)
  P3a 思考 (ReasoningDelta)
  P3b plan (PlanUpdate)
  P4a diff (FilesChanged)

运行: python3 tests/test_projection_differ.py
依赖: 仅 Python 标准库 + 本项目的 acp-bridge 模块
"""

import os
import sys
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


class TestProjectionDiffer(unittest.TestCase):
    """ProjectionDiffer 的 6 项能力转换单测"""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_bridge_module()
        cls.Differ = cls.mod.ProjectionDiffer

    def _new_differ(self):
        return self.Differ()

    def test_p0_tool_call_new(self):
        """P0: 新增 tool part → ToolCallNew 事件"""
        differ = self._new_differ()
        snap1 = {"projection": {}, "messages": [], "todos": []}
        snap2 = {
            "projection": {},
            "messages": [{
                "info": {"role": "assistant"},
                "parts": [{
                    "type": "tool", "callID": "call_001", "tool": "Bash",
                    "state": {"status": "completed",
                              "input": {"command": "echo hi"},
                              "output": "hi"},
                }],
            }],
            "todos": [],
        }
        events = differ.diff(snap1, snap2)
        tool_events = [e for e in events if e["kind"] == "ToolCallNew"]
        self.assertEqual(len(tool_events), 1)
        e = tool_events[0]
        self.assertEqual(e["call_id"], "call_001")
        self.assertEqual(e["tool"], "Bash")
        self.assertEqual(e["status"], "completed")

    def test_p0_tool_call_update(self):
        """P0: 同一 toolCallId 状态变化 → ToolCallUpdate"""
        differ = self._new_differ()
        snap1 = {
            "projection": {},
            "messages": [{
                "info": {"role": "assistant"},
                "parts": [{
                    "type": "tool", "callID": "call_002", "tool": "Bash",
                    "state": {"status": "running", "input": {"command": "ls"}},
                }],
            }],
            "todos": [],
        }
        differ.diff(None, snap1)  # 首次, 触发 ToolCallNew
        snap2 = {
            "projection": {},
            "messages": [{
                "info": {"role": "assistant"},
                "parts": [{
                    "type": "tool", "callID": "call_002", "tool": "Bash",
                    "state": {"status": "completed", "output": "file1\nfile2"},
                }],
            }],
            "todos": [],
        }
        events = differ.diff(snap1, snap2)
        updates = [e for e in events if e["kind"] == "ToolCallUpdate"]
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0]["status"], "completed")
        self.assertEqual(updates[0]["output"], "file1\nfile2")

    def test_p1a_usage_delta(self):
        """P1a: totalTokenCount 变化 → UsageDelta"""
        differ = self._new_differ()
        snap1 = {"projection": {"totalTokenCount": 100, "contextWindow": 1000000},
                 "messages": [], "todos": []}
        snap2 = {"projection": {"totalTokenCount": 500, "contextWindow": 1000000},
                 "messages": [], "todos": []}
        events = differ.diff(snap1, snap2)
        usage = [e for e in events if e["kind"] == "UsageDelta"]
        self.assertEqual(len(usage), 1)
        self.assertEqual(usage[0]["used"], 500)
        self.assertEqual(usage[0]["size"], 1000000)

    def test_p2_text_delta(self):
        """P2: 新增 assistant text → 整条 TextDelta (按 message id 去重)"""
        differ = self._new_differ()
        # 第一条 assistant 消息
        snap1 = {"projection": {},
                 "messages": [{"info": {"role": "assistant", "id": "m1"},
                               "parts": [{"type": "text", "text": "你好"}]}],
                 "todos": []}
        events1 = differ.diff(None, snap1)
        deltas1 = [e for e in events1 if e["kind"] == "TextDelta"]
        self.assertEqual(len(deltas1), 1)
        self.assertEqual(deltas1[0]["text"], "你好")
        # 同一条消息再 diff 不应重发 (id 去重)
        events2 = differ.diff(snap1, snap1)
        deltas2 = [e for e in events2 if e["kind"] == "TextDelta"]
        self.assertEqual(len(deltas2), 0)

    def test_p1_1_no_history_replay(self):
        """P1.1: 多轮/resume 时历史 assistant 消息不重发"""
        differ = self._new_differ()
        # 第一轮: 两条历史消息 (user + assistant)
        snap_baseline = {"projection": {},
                 "messages": [
                     {"info": {"role": "user", "id": "u1"}, "parts": [{"type": "text", "text": "问题"}]},
                     {"info": {"role": "assistant", "id": "a1"}, "parts": [{"type": "text", "text": "旧回复"}]},
                 ],
                 "todos": []}
        differ.mark_seen(snap_baseline["messages"])  # 建 baseline
        # 第二轮: 历史仍在 + 新增一条 assistant
        snap_turn2 = {"projection": {},
                 "messages": [
                     {"info": {"role": "user", "id": "u1"}, "parts": [{"type": "text", "text": "问题"}]},
                     {"info": {"role": "assistant", "id": "a1"}, "parts": [{"type": "text", "text": "旧回复"}]},
                     {"info": {"role": "user", "id": "u2"}, "parts": [{"type": "text", "text": "追问"}]},
                     {"info": {"role": "assistant", "id": "a2"}, "parts": [{"type": "text", "text": "新回复"}]},
                 ],
                 "todos": []}
        events = differ.diff(None, snap_turn2)
        deltas = [e for e in events if e["kind"] == "TextDelta"]
        # 只应发"新回复", 不重发"旧回复"
        self.assertEqual(len(deltas), 1)
        self.assertEqual(deltas[0]["text"], "新回复")

    def test_p2_3_plan_clear_emits_empty(self):
        """P2.3: todos 从有内容变空, 应发空 plan (不残留旧 plan)"""
        differ = self._new_differ()
        # 先有 plan
        snap1 = {"projection": {}, "messages": [],
                 "todos": [{"content": "任务1", "status": "in_progress", "priority": "high"}]}
        differ.diff(None, snap1)
        # plan 清空
        snap2 = {"projection": {}, "messages": [], "todos": []}
        events = differ.diff(snap1, snap2)
        plans = [e for e in events if e["kind"] == "PlanUpdate"]
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0]["entries"], [])  # 空列表也要发

    def test_p3_no_id_message_not_repeated(self):
        """P3: 无 info.id 的消息连续 diff 不重复发 (用 parts hash fallback 去重)"""
        differ = self._new_differ()
        # 模拟无 id 的 assistant message (边界情况, zcode 实测都有 id, 但防御性)
        snap = {"projection": {},
                "messages": [{"info": {"role": "assistant"},  # 注意: 无 id
                              "parts": [{"type": "text", "text": "回复"}]}],
                "todos": []}
        events1 = differ.diff(None, snap)
        deltas1 = [e for e in events1 if e["kind"] == "TextDelta"]
        self.assertEqual(len(deltas1), 1)  # 首次发
        # 同一无 id 消息再 diff: 不应重发
        events2 = differ.diff(snap, snap)
        deltas2 = [e for e in events2 if e["kind"] == "TextDelta"]
        self.assertEqual(len(deltas2), 0)  # 修复后: fallback 去重生效

    def test_p1_fetch_last_reply_skips_seen(self):
        """P1: 兜底回复跳过 differ 已见的消息 (避免把旧回复当兜底发)"""
        differ = self._new_differ()
        # 两条 assistant 消息: 旧的(a1) 和 新的(a2)
        msgs = [
            {"info": {"role": "assistant", "id": "a1"},
             "parts": [{"type": "text", "text": "旧回复"}]},
            {"info": {"role": "assistant", "id": "a2"},
             "parts": [{"type": "text", "text": "新回复"}]},
        ]
        # 把 a1 (旧) 标记为已见 (模拟上一轮已发)
        differ.mark_seen([msgs[0]])
        # 模拟 _fetch_last_reply 的过滤逻辑: reversed 找第一条未见的 assistant
        found = None
        for m in reversed(msgs):
            if m["info"]["role"] != "assistant":
                continue
            key = differ._message_dedup_key(m)
            if key and key in differ._seen_message_ids:
                continue  # 已见, 跳过
            texts = [p["text"] for p in m["parts"] if p.get("type") == "text"]
            found = "\n".join(texts)
            break
        self.assertEqual(found, "新回复")  # 只返回未见的, 不返回旧的

    def test_p1_fetch_last_reply_all_seen_returns_none(self):
        """P1: 所有 assistant 都已见过时不兜底发旧内容 (tool-only turn)"""
        differ = self._new_differ()
        msgs = [{"info": {"role": "assistant", "id": "a1"},
                 "parts": [{"type": "text", "text": "旧回复"}]}]
        differ.mark_seen(msgs)  # 全部已见
        found = None
        for m in reversed(msgs):
            if m["info"]["role"] != "assistant":
                continue
            key = differ._message_dedup_key(m)
            if key and key in differ._seen_message_ids:
                continue
            found = m["parts"][0]["text"]
            break
        self.assertIsNone(found)  # 无未见文本, 不兜底

    def test_p3a_reasoning_delta(self):
        """P3a: reasoning part 增量 → ReasoningDelta"""
        differ = self._new_differ()
        snap1 = {"projection": {}, "messages": [], "todos": []}
        snap2 = {
            "projection": {},
            "messages": [{"info": {"role": "assistant"},
                          "parts": [{"type": "reasoning", "text": "首先分析问题"}]}],
            "todos": [],
        }
        events = differ.diff(snap1, snap2)
        reasoning = [e for e in events if e["kind"] == "ReasoningDelta"]
        self.assertEqual(len(reasoning), 1)
        self.assertEqual(reasoning[0]["text"], "首先分析问题")

    def test_p3b_plan_update(self):
        """P3b: todos 变化 → PlanUpdate (entries 全量)"""
        differ = self._new_differ()
        snap1 = {"projection": {}, "messages": [], "todos": []}
        snap2 = {
            "projection": {}, "messages": [], "todos": [
                {"content": "读取文件", "status": "in_progress", "priority": "high"},
                {"content": "分析内容", "status": "pending", "priority": "medium"},
                {"content": "输出报告", "status": "pending", "priority": "low"},
            ],
        }
        events = differ.diff(snap1, snap2)
        plans = [e for e in events if e["kind"] == "PlanUpdate"]
        self.assertEqual(len(plans), 1)
        self.assertEqual(len(plans[0]["entries"]), 3)
        # 验证枚举值透传 (zcode 的 status/priority 枚举与 ACP 一致)
        self.assertEqual(plans[0]["entries"][0]["status"], "in_progress")
        self.assertEqual(plans[0]["entries"][0]["priority"], "high")

    def test_p4a_files_changed(self):
        """P4a: patch part → FilesChanged (文件列表)"""
        differ = self._new_differ()
        snap1 = {"projection": {}, "messages": [], "todos": []}
        snap2 = {
            "projection": {},
            "messages": [{"info": {"role": "assistant"},
                          "parts": [{"type": "patch", "hash": "abc123",
                                     "files": ["/tmp/a.py", "/tmp/b.py"]}]}],
            "todos": [],
        }
        events = differ.diff(snap1, snap2)
        files = [e for e in events if e["kind"] == "FilesChanged"]
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0]["files"], ["/tmp/a.py", "/tmp/b.py"])

    def test_no_duplicate_tool_call(self):
        """同一 callID 不重复触发 ToolCallNew"""
        differ = self._new_differ()
        snap = {
            "projection": {},
            "messages": [{"info": {"role": "assistant"},
                          "parts": [{"type": "tool", "callID": "c1", "tool": "Read",
                                     "state": {"status": "completed", "output": "x"}}]}],
            "todos": [],
        }
        differ.diff(None, snap)
        events = differ.diff(snap, snap)  # 相同快照再 diff
        new_calls = [e for e in events if e["kind"] == "ToolCallNew"]
        self.assertEqual(len(new_calls), 0)  # 不应重复

    def test_tool_kind_mapping(self):
        """tool 名 → ACP ToolKind 映射正确"""
        differ = self._new_differ()
        cases = {
            "Bash": "execute", "Edit": "edit", "Write": "edit",
            "Read": "read", "Grep": "search", "WebFetch": "fetch",
        }
        for zcode_tool, expected_kind in cases.items():
            self.assertEqual(
                differ.TOOL_KIND_MAP.get(zcode_tool), expected_kind,
                f"{zcode_tool} 应映射为 {expected_kind}",
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
