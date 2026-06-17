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
        """P2: assistant text 增量 → TextDelta (只发新增部分)"""
        differ = self._new_differ()
        snap1 = {"projection": {},
                 "messages": [{"info": {"role": "assistant"},
                               "parts": [{"type": "text", "text": "你好"}]}],
                 "todos": []}
        differ.diff(None, snap1)  # 发 "你好"
        snap2 = {"projection": {},
                 "messages": [{"info": {"role": "assistant"},
                               "parts": [{"type": "text", "text": "你好世界"}]}],
                 "todos": []}
        events = differ.diff(snap1, snap2)
        deltas = [e for e in events if e["kind"] == "TextDelta"]
        self.assertEqual(len(deltas), 1)
        self.assertEqual(deltas[0]["text"], "世界")  # 只发增量

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
