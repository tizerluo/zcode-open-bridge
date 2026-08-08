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

import json
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
    """进程级文件锁 (ReviewFileLock) 测试。

    用 ZCODE_BRIDGE_LOCK_DIR 隔离到临时目录, 避免写真实 ~/.zcode (Codex P1-2:
    沙箱/CI 里真实 HOME 可能不可写, 导致 PermissionError)。
    """

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_mcp_module()
        cls.Lock = cls.mod.ReviewFileLock

    def setUp(self):
        import tempfile
        self._tmpdir = tempfile.mkdtemp(prefix="zcode-lock-test-")
        self._saved_dir = os.environ.get("ZCODE_BRIDGE_LOCK_DIR")
        os.environ["ZCODE_BRIDGE_LOCK_DIR"] = self._tmpdir

    def tearDown(self):
        import shutil
        if self._saved_dir is None:
            os.environ.pop("ZCODE_BRIDGE_LOCK_DIR", None)
        else:
            os.environ["ZCODE_BRIDGE_LOCK_DIR"] = self._saved_dir
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_rl0_acquire_release(self):
        """RL0: 锁可获取, with 块结束自动释放"""
        with self.Lock(timeout=5) as lock:
            self.assertTrue(lock.acquired, "应成功获取锁")
            self.assertIsNotNone(lock.fd)

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

    def test_rl3_lock_file_in_temp_dir(self):
        """RL3: 锁文件创建在隔离的临时目录, 不写真实 ~/.zcode"""
        with self.Lock(timeout=5):
            lock_files = os.listdir(self._tmpdir)
        self.assertIn("zcode-bridge-review.lock", lock_files,
                       "锁文件应在 ZCODE_BRIDGE_LOCK_DIR 指定的临时目录")

    def test_rl4_degrade_when_dir_unwritable(self):
        """RL4: 锁目录不可写时降级为无锁 (不崩, Codex P1-2)"""
        # 指向一个不存在的、且无法 mkdir 的路径 (父父目录无权限通常难造,
        # 改用: 指向一个已存在的普通文件作为 dir, mkdir 会失败)
        import tempfile
        f = tempfile.NamedTemporaryFile(delete=False)
        f.close()
        self.addCleanup(os.unlink, f.name)
        os.environ["ZCODE_BRIDGE_LOCK_DIR"] = f.name  # 这是一个文件, 不是目录
        with self.Lock(timeout=5) as lock:
            # 应降级为无锁 (fd=None), 而非抛异常
            self.assertIsNone(lock.fd, "目录无效时应降级无锁, fd=None")


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

    # ---------- RT5: exit=0 + stderr=Unauthorized 不当成功 (Codex P1-1) ----------
    def test_rt5_exit0_with_stderr_error_not_success(self):
        """RT5: zcode exit=0 但 stderr 含 APICallError: Unauthorized → 当错误返回 (Codex P1-1)

        zcode 错误常 exit=0 + stdout 空 + stderr 含堆栈。修复前 parser 副本缺
        provider_error 分支, 会误判为成功空输出。补分支后应识别为错误。
        """
        mod = self.mod
        procs = [_FakeCompletedProcess(
            returncode=0, stdout="",
            stderr="APICallError [AI_APICallError]: Unauthorized\n    at ...")]
        old = os.environ.pop("ZCODE_BRIDGE_REVIEW_LOCK", None)
        os.environ["ZCODE_BRIDGE_REVIEW_LOCK"] = "0"
        try:
            calls, saved_run, real_sleep = self._patch(mod, procs)
            try:
                result = self._review(mod)
            finally:
                self._restore(mod, saved_run, real_sleep)
            self.assertTrue(result.get("isError"),
                            "exit=0+stderr=Unauthorized 应识别为错误, 不当成功空输出")
            self.assertEqual(calls["n"], 1, "provider_error 不重试")
            self.assertIn("Unauthorized", result["content"][0]["text"])
        finally:
            if old is None:
                os.environ.pop("ZCODE_BRIDGE_REVIEW_LOCK", None)
            else:
                os.environ["ZCODE_BRIDGE_REVIEW_LOCK"] = old

    # ---------- RT6: stdout 含限流词 + stderr 非空 → 不误判限流 (Claude P1-1) ----------
    def test_rt6_review_text_with_rate_keywords_not_misjudged(self):
        """RT6: 审查"限流逻辑"代码的结论含 429/频繁, 但 stderr 非空诊断 → 不当限流重试

        Claude review P1-1: 若把 stdout 审查正文喂进 parse_provider_error, 审查限流
        相关代码会被误判限流而白重试。修复: 只解析 stderr。
        """
        mod = self.mod
        # stdout 是审查结论 (含 429/频繁), stderr 有诊断 (非限流)
        procs = [_FakeCompletedProcess(
            returncode=0,
            stdout="这段代码处理 429 限流和频繁请求的逻辑建议加退避。额度计算也有问题。",
            stderr="some diagnostic noise\nfinished")]
        old = os.environ.pop("ZCODE_BRIDGE_REVIEW_LOCK", None)
        os.environ["ZCODE_BRIDGE_REVIEW_LOCK"] = "0"
        try:
            calls, saved_run, real_sleep = self._patch(mod, procs)
            try:
                result = self._review(mod)
            finally:
                self._restore(mod, saved_run, real_sleep)
            # 关键: 不应重试 (calls=1), 不应 isError, 应返回审查结论
            self.assertEqual(calls["n"], 1, "审查正文含限流词不应触发重试")
            self.assertNotIn("isError", result, "成功审查不应被误判为错误")
            self.assertIn("429", result["content"][0]["text"], "审查结论应原样返回")
        finally:
            if old is None:
                os.environ.pop("ZCODE_BRIDGE_REVIEW_LOCK", None)
            else:
                os.environ["ZCODE_BRIDGE_REVIEW_LOCK"] = old
    # ---------- RT7: stderr 警告词 + stdout 有结论 → 不误杀 (整体 review P1-1) ----------
    def test_rt7_stderr_warning_with_stdout_not_error(self):
        """RT7: exit=0 + stdout 是合法 --json 结论 + stderr 含错误关键词 → 算成功

        整体 review P1-1 + 复审 P1-A: 判据是"stdout 可解析出 response 结果"
        (非"非空") — zcode 常往 stderr 打 Node 警告/诊断, 恰好含
        "Unauthorized" 等词时, 旧规则会把成功审查误判为错误丢弃结论。
        """
        mod = self.mod
        procs = [_FakeCompletedProcess(
            returncode=0,
            stdout=json.dumps({"response": "审查结论: 代码无问题"}, ensure_ascii=False),
            stderr="(node:123) Warning: Unauthorized token refresh attempt, retried OK")]
        old = os.environ.pop("ZCODE_BRIDGE_REVIEW_LOCK", None)
        os.environ["ZCODE_BRIDGE_REVIEW_LOCK"] = "0"
        try:
            calls, saved_run, real_sleep = self._patch(mod, procs)
            try:
                result = self._review(mod)
            finally:
                self._restore(mod, saved_run, real_sleep)
            self.assertNotIn("isError", result,
                             "stderr 警告词 + stdout 合法结论不应误判为错误")
            self.assertEqual(calls["n"], 1, "不应触发重试")
            self.assertIn("审查结论", result["content"][0]["text"])
        finally:
            if old is None:
                os.environ.pop("ZCODE_BRIDGE_REVIEW_LOCK", None)
            else:
                os.environ["ZCODE_BRIDGE_REVIEW_LOCK"] = old

    def test_rt7b_nonjson_stdout_with_stderr_keyword_is_error(self):
        """RT7b: stdout 非空但不是合法 JSON 结果 (错误回显文本) + stderr 命中 → 判错误

        复审 P1-A 反例: 收窄不能漏掉"stdout 有内容但实为错误回显"。
        """
        mod = self.mod
        procs = [_FakeCompletedProcess(
            returncode=0,
            stdout="Error: provider returned an error page",  # 非 JSON 的错误文本
            stderr="APICallError: Unauthorized")]
        old = os.environ.pop("ZCODE_BRIDGE_REVIEW_LOCK", None)
        os.environ["ZCODE_BRIDGE_REVIEW_LOCK"] = "0"
        try:
            calls, saved_run, real_sleep = self._patch(mod, procs)
            try:
                result = self._review(mod)
            finally:
                self._restore(mod, saved_run, real_sleep)
            self.assertTrue(result.get("isError"),
                            "stdout 非 JSON 结果 + stderr 命中应判错误")
        finally:
            if old is None:
                os.environ.pop("ZCODE_BRIDGE_REVIEW_LOCK", None)
            else:
                os.environ["ZCODE_BRIDGE_REVIEW_LOCK"] = old

    def test_rt7c_leading_warning_line_tolerated(self):
        """RT7c: stdout 先打 AI SDK 警告行再输出 JSON → 仍能提取 response (实测形态)"""
        mod = self.mod
        payload = ("AI SDK Warning System: To turn off warning logging...\n"
                   + json.dumps({"response": "正文结论"}, ensure_ascii=False))
        procs = [_FakeCompletedProcess(returncode=0, stdout=payload, stderr="")]
        old = os.environ.pop("ZCODE_BRIDGE_REVIEW_LOCK", None)
        os.environ["ZCODE_BRIDGE_REVIEW_LOCK"] = "0"
        try:
            calls, saved_run, real_sleep = self._patch(mod, procs)
            try:
                result = self._review(mod)
            finally:
                self._restore(mod, saved_run, real_sleep)
            self.assertNotIn("isError", result)
            self.assertEqual(result["content"][0]["text"], "正文结论")
        finally:
            if old is None:
                os.environ.pop("ZCODE_BRIDGE_REVIEW_LOCK", None)
            else:
                os.environ["ZCODE_BRIDGE_REVIEW_LOCK"] = old

    # ---------- RT8: stdout 体积上限截断 (整体 review P2-1) ----------
    def test_rt8_stdout_size_cap(self):
        """RT8: 成功但 stdout 超 ZCODE_BRIDGE_MAX_OUTPUT → 截断 + 标注"""
        mod = self.mod
        procs = [_FakeCompletedProcess(0, "y" * 20000, "")]
        old = os.environ.pop("ZCODE_BRIDGE_REVIEW_LOCK", None)
        old_mo = os.environ.pop("ZCODE_BRIDGE_MAX_OUTPUT", None)
        os.environ["ZCODE_BRIDGE_REVIEW_LOCK"] = "0"
        os.environ["ZCODE_BRIDGE_MAX_OUTPUT"] = "10000"
        try:
            calls, saved_run, real_sleep = self._patch(mod, procs)
            try:
                result = self._review(mod)
            finally:
                self._restore(mod, saved_run, real_sleep)
            self.assertNotIn("isError", result)
            text = result["content"][0]["text"]
            self.assertIn("已截断", text)
            self.assertLess(len(text), 11000, "截断后长度应在上限附近")
        finally:
            if old is None:
                os.environ.pop("ZCODE_BRIDGE_REVIEW_LOCK", None)
            else:
                os.environ["ZCODE_BRIDGE_REVIEW_LOCK"] = old
            if old_mo is None:
                os.environ.pop("ZCODE_BRIDGE_MAX_OUTPUT", None)
            else:
                os.environ["ZCODE_BRIDGE_MAX_OUTPUT"] = old_mo


class TestEmbeddedProviderError(unittest.TestCase):
    """内嵌 _parse_provider_error 与 shared/provider_error.py 权威版对齐
    (整体 review P1-4: insufficient 拆成 credit/balance 两条, 漏 quota 形态)"""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_mcp_module()

    def test_ep0_insufficient_quota_recognized(self):
        """EP0: 'insufficientQuota' (无词边界, \bquota\b 吃不到) → quota"""
        r = self.mod._parse_provider_error("Your plan has insufficientQuota")
        self.assertTrue(r["is_quota"], "insufficient.*quota 形态应判为配额错误")
        self.assertEqual(r["error_kind"], "quota")

    def test_ep1_insufficient_credit_balance(self):
        """EP1: insufficient credit / balance → quota (对齐后不回归)"""
        for text in ("insufficient credit", "insufficient balance"):
            r = self.mod._parse_provider_error(text)
            self.assertTrue(r["is_quota"], f"{text!r} 应判为配额错误")

    def test_ep2_rate_limit_exceeded_not_quota(self):
        """EP2: 'rate limit exceeded' 仍判限流, 不被 quota 误吃 (对齐后语义不变)"""
        r = self.mod._parse_provider_error("Rate limit exceeded, retry-after: 30")
        self.assertTrue(r["is_rate_limit"])
        self.assertFalse(r["is_quota"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
