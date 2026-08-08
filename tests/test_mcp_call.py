"""
test_mcp_call.py — mcp-server --call 一次性调用模式单测

对真实脚本文件起子进程验证 (跨进程, 无法 monkeypatch handler):
  - 未知 tool → exit 1
  - args 非 JSON → exit 1
  - args 非 JSON 对象 → exit 1
  - handler 返回 isError (目录不存在的 zcode_pr_review) → exit 2,
    stdout 是合法 JSON 且 ok=false
  - get_zcode_capabilities → exit 0, ok=true
    (该 handler 只调 packages/agent-help/zcode-agent-help, 零外部依赖;
     显式设 ZCODE_AGENT_HELP_BIN 指向仓库内副本, 防环境里已有同名变量
     指向安装态旧版/缺失路径)

运行: python3 tests/test_mcp_call.py
依赖: 仅 Python 标准库 + 仓库内两个组件
"""

import json
import os
import subprocess
import sys
import unittest

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..")
MCP_PATH = os.path.join(REPO_ROOT, "packages", "mcp-server", "zcode-mcp-server")
AGENT_HELP_PATH = os.path.abspath(
    os.path.join(REPO_ROOT, "packages", "agent-help", "zcode-agent-help")
)


def _run_call(*args):
    env = dict(os.environ)
    env["ZCODE_AGENT_HELP_BIN"] = AGENT_HELP_PATH
    return subprocess.run(
        [sys.executable, MCP_PATH, "--call", *args],
        capture_output=True, text=True, timeout=60, env=env,
    )


class TestMcpCall(unittest.TestCase):
    def test_unknown_tool_exit_1(self):
        p = _run_call("no_such_tool", "{}")
        self.assertEqual(p.returncode, 1)
        self.assertIn("未知 tool", p.stderr)

    def test_bad_json_exit_1(self):
        p = _run_call("zcode_pr_review", "not-json{")
        self.assertEqual(p.returncode, 1)

    def test_non_dict_args_exit_1(self):
        p = _run_call("zcode_pr_review", "[1, 2]")
        self.assertEqual(p.returncode, 1)

    def test_tool_error_exit_2(self):
        """handler 返回 isError → exit 2, stdout 合法 JSON 且 ok=false"""
        p = _run_call("zcode_pr_review",
                      json.dumps({"path": "/nonexistent-path-zcode-gate-test"}))
        self.assertEqual(p.returncode, 2, msg=f"stderr: {p.stderr[:300]}")
        data = json.loads(p.stdout)  # stdout 必须整体是合法 JSON
        self.assertFalse(data["ok"])
        self.assertTrue(data["result"]["isError"])

    def test_ok_exit_0(self):
        """get_zcode_capabilities 只调仓库内 agent-help, 无网络/zcode 依赖"""
        p = _run_call("get_zcode_capabilities", "{}")
        self.assertEqual(p.returncode, 0, msg=f"stderr: {p.stderr[:500]}")
        data = json.loads(p.stdout)
        self.assertTrue(data["ok"])
        text = data["result"]["content"][0]["text"]
        self.assertIn("zcode", text)

    def test_stdio_mode_untouched(self):
        """无 --call 参数时仍是 stdio server: initialize 走 JSON-RPC 握手"""
        req = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
               "params": {"protocolVersion": "2025-11-25"}}
        p = subprocess.run(
            [sys.executable, MCP_PATH],
            input=json.dumps(req) + "\n",
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(p.returncode, 0)
        resp = json.loads(p.stdout.strip().splitlines()[0])
        self.assertEqual(resp["id"], 1)
        self.assertIn("protocolVersion", resp["result"])


if __name__ == "__main__":
    unittest.main()
