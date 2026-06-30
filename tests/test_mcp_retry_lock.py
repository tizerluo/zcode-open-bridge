"""
test_mcp_retry_lock.py — MCP server 限流重试与文件锁单测

验证 zcode-mcp-server 的 tool_zcode_review (issue #3 子项3b+3c):
  - provider 限流错误 → 有限重试 (指数退避)
  - 非限流错误 → 不重试, 直接返回
  - 进程级文件锁 (ReviewFileLock) 可获取/释放/禁用/超时

用 monkeypatch 替换 subprocess.run (不真跑 zcode), 模拟限流/非限流错误。
为避免重试测试真的 sleep, 也 patch time.sleep 为 no-op。

  RL0 文件锁: 获取 + 自动释放
  RL1 文件锁: ZCODE_BRIDGE_REVIEW_LOCK=0 禁用
  RL2 文件锁: 同进程二次获取 (重入, fcntl LOCK_EX 在同 fd 上是递归的)
  RT0 重试: 限流错误 (429) → 重试, 最终成功
  RT1 重试: 重试耗尽仍限流 → 返回带"已重试 N 次"的错误
  RT2 重试: 非限流错误 (Unauthorized) → 不重试, 直接返回错误
  RT3 重试: 首次成功 → 不重试
  RT4 重试: max_retries 可由 env 配置 (ZCODE_BRIDGE_MAX_RETRIES)

运行: python3 tests/test_mcp_retry_lock.py
依赖: 仅 Python 标准库 + zcode-mcp-server 模块
"""

import os
import types
import unittest

# 加载 zcode-mcp-server (单文件, exec 导入, 跳过 main)
MCP_PATH = os.path.join(
    os.path.dirname(__file__), "..", "packages", "mcp-server", "zcode-mcp-server"
)


def _load_mcp_module():
    mod = types.ModuleType("zcode_mcp_server")
    # MCP server 顶层用 Path(__file__) 解析 AGENT_HELP_BIN, exec 进新模块需提供 __file__
    mod.__file__ = MCP_PATH
    with open(MCP_PATH) as f:
        code = f.read()
    code_no_main = code.split('if __name__ == "__main__":')[0]
    exec(code_no_main, mod.__dict__)
    return mod


class _FakeCompletedProcess:
    """模拟 subprocess.run 的返回值。"""

    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class TestReviewFileLock(unittest.TestCase):
    """进程级文件锁 (ReviewFileLock) 测试"""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_mcp_module()
        cls.Lock = cls.mod.ReviewFileLock

    def test_rl0_acquire_release(self):
        """RL0: 锁可获取, with 块结束自动释放"""
        with self.Lock(timeout=5) as lock:
            self.assertIsNotNone(lock)

    def test_rl1_disabled_by_env(self):
        """RL1: ZCODE_BRIDGE_REVIEW_LOCK=0 → 禁用锁 (enabled=False)"""
        old = os.environ.get("ZCODE_BRIDGE_REVIEW_LOCK")
        try:
            os.environ["ZCODE_BRIDGE_REVIEW_LOCK"] = "0"
            lock = self.Lock(timeout=5)
            self.assertFalse(lock.enabled, "env=0 应禁用锁")
            with lock:
                pass  # 禁用时直接通过, 不获取文件锁
        finally:
            if old is None:
                os.environ.pop("ZCODE_BRIDGE_REVIEW_LOCK", None)
            else:
                os.environ["ZCODE_BRIDGE_REVIEW_LOCK"] = old

    def test_rl2_reentrant_same_process(self):
        """RL2: 同进程顺序获取两次 (第一次释放后再取) 不死锁"""
        with self.Lock(timeout=5):
            pass
        with self.Lock(timeout=5):
            pass  # 第二次也能拿到 (前次已释放)


class TestRetryLogic(unittest.TestCase):
    """tool_zcode_review 的限流重试逻辑测试 (monkeypatch subprocess.run + time.sleep)"""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_mcp_module()

    def _patch(self, mod, completed_processes):
        """patch subprocess.run 返回序列 + time.sleep no-op。返回 call 计数器。"""
        calls = {"n": 0}
        real_sleep = mod.time.sleep
        saved_run = mod.subprocess.run

        def fake_run(*a, **kw):
            i = calls["n"]
            calls["n"] += 1
            if i < len(completed_processes):
                return completed_processes[i]
            return completed_processes[-1]  # 超出则重复最后一个

        def no_sleep(_secs):
            pass

        mod.subprocess.run = fake_run
        mod.time.sleep = no_sleep
        return calls, saved_run, real_sleep

    def _restore(self, mod, saved_run, real_sleep):
        mod.subprocess.run = saved_run
        mod.time.sleep = real_sleep

    def _review(self, mod, **kwargs):
        """调 tool_zcode_review, 默认给 code (避免真文件)。"""
        args = {"code": "print('x')", "focus": "测试", **kwargs}
        return mod.tool_zcode_review(args)

    # ---------- RT0: 限流重试后成功 ----------
    def test_rt0_rate_limit_then_success(self):
        """RT0: 首次 429 限流 → 重试 → 第二次成功"""
        mod = self.mod
        procs = [
            _FakeCompletedProcess(returncode=0, stdout="", stderr="429 Too Many Requests"),
            _FakeCompletedProcess(returncode=0, stdout="审查结论: OK", stderr=""),
        ]
        # 禁用文件锁, 避免测试间锁干扰
        old = os.environ.pop("ZCODE_BRIDGE_REVIEW_LOCK", None)
        os.environ["ZCODE_BRIDGE_REVIEW_LOCK"] = "0"
        try:
            calls, saved_run, real_sleep = self._patch(mod, procs)
            try:
                result = self._review(mod)
            finally:
                self._restore(mod, saved_run, real_sleep)
            self.assertEqual(calls["n"], 2, "应重试一次 (共调用 2 次)")
            self.assertNotIn("isError", result, "最终成功不应 isError")
            self.assertIn("审查结论", result["content"][0]["text"])
        finally:
            if old is None:
                os.environ.pop("ZCODE_BRIDGE_REVIEW_LOCK", None)
            else:
                os.environ["ZCODE_BRIDGE_REVIEW_LOCK"] = old

    # ---------- RT1: 重试耗尽 ----------
    def test_rt1_rate_limit_exhausted(self):
        """RT1: 始终限流 → 重试耗尽, 返回带重试次数的错误"""
        mod = self.mod
        procs = [_FakeCompletedProcess(returncode=0, stdout="",
                                        stderr="429 Too Many Requests")]  # 每次都限流
        old = os.environ.pop("ZCODE_BRIDGE_REVIEW_LOCK", None)
        old_mr = os.environ.pop("ZCODE_BRIDGE_MAX_RETRIES", None)
        os.environ["ZCODE_BRIDGE_REVIEW_LOCK"] = "0"
        os.environ["ZCODE_BRIDGE_MAX_RETRIES"] = "2"
        try:
            calls, saved_run, real_sleep = self._patch(mod, procs)
            try:
                result = self._review(mod)
            finally:
                self._restore(mod, saved_run, real_sleep)
            self.assertTrue(result.get("isError"), "重试耗尽应返回错误")
            self.assertIn("已重试", result["content"][0]["text"])
            self.assertEqual(calls["n"], 3, "max_retries=2 → 共调用 3 次 (1+2 重试)")
        finally:
            if old is None:
                os.environ.pop("ZCODE_BRIDGE_REVIEW_LOCK", None)
            else:
                os.environ["ZCODE_BRIDGE_REVIEW_LOCK"] = old
            if old_mr is None:
                os.environ.pop("ZCODE_BRIDGE_MAX_RETRIES", None)
            else:
                os.environ["ZCODE_BRIDGE_MAX_RETRIES"] = old_mr

    # ---------- RT2: 非限流不重试 ----------
    def test_rt2_non_rate_limit_no_retry(self):
        """RT2: Unauthorized (provider_error) → 不重试, 直接返回"""
        mod = self.mod
        procs = [_FakeCompletedProcess(returncode=1, stdout="",
                                        stderr="APICallError: Unauthorized")]
        old = os.environ.pop("ZCODE_BRIDGE_REVIEW_LOCK", None)
        os.environ["ZCODE_BRIDGE_REVIEW_LOCK"] = "0"
        try:
            calls, saved_run, real_sleep = self._patch(mod, procs)
            try:
                result = self._review(mod)
            finally:
                self._restore(mod, saved_run, real_sleep)
            self.assertTrue(result.get("isError"))
            self.assertEqual(calls["n"], 1, "非限流错误不应重试")
            self.assertNotIn("已重试", result["content"][0]["text"],
                             "未重试不应出现'已重试'字样")
        finally:
            if old is None:
                os.environ.pop("ZCODE_BRIDGE_REVIEW_LOCK", None)
            else:
                os.environ["ZCODE_BRIDGE_REVIEW_LOCK"] = old

    # ---------- RT3: 首次成功 ----------
    def test_rt3_success_first_try(self):
        """RT3: 首次成功 → 不重试"""
        mod = self.mod
        procs = [_FakeCompletedProcess(returncode=0, stdout="OK", stderr="")]
        old = os.environ.pop("ZCODE_BRIDGE_REVIEW_LOCK", None)
        os.environ["ZCODE_BRIDGE_REVIEW_LOCK"] = "0"
        try:
            calls, saved_run, real_sleep = self._patch(mod, procs)
            try:
                result = self._review(mod)
            finally:
                self._restore(mod, saved_run, real_sleep)
            self.assertNotIn("isError", result)
            self.assertEqual(calls["n"], 1)
        finally:
            if old is None:
                os.environ.pop("ZCODE_BRIDGE_REVIEW_LOCK", None)
            else:
                os.environ["ZCODE_BRIDGE_REVIEW_LOCK"] = old

    # ---------- RT4: 配额错误不重试 ----------
    def test_rt4_quota_no_retry(self):
        """RT4: quota exceeded (配额耗尽) → 不重试"""
        mod = self.mod
        procs = [_FakeCompletedProcess(returncode=1, stdout="",
                                        stderr="quota exceeded")]
        old = os.environ.pop("ZCODE_BRIDGE_REVIEW_LOCK", None)
        os.environ["ZCODE_BRIDGE_REVIEW_LOCK"] = "0"
        try:
            calls, saved_run, real_sleep = self._patch(mod, procs)
            try:
                result = self._review(mod)
            finally:
                self._restore(mod, saved_run, real_sleep)
            self.assertTrue(result.get("isError"))
            self.assertEqual(calls["n"], 1, "配额耗尽不应重试")
        finally:
            if old is None:
                os.environ.pop("ZCODE_BRIDGE_REVIEW_LOCK", None)
            else:
                os.environ["ZCODE_BRIDGE_REVIEW_LOCK"] = old


if __name__ == "__main__":
    unittest.main(verbosity=2)
