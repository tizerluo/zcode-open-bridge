"""
test_provider_error.py — parse_provider_error 错误解析单测

验证 shared/provider_error.py 能正确分类 zcode headless 调用返回的 provider 错误,
并提取 retry-after, 用于 MCP server 的有限重试决策 (issue #3 子项3a)。

  PE0 限流 429 (HTTP 通用)
  PE1 限流 1302 (z.ai 特定业务码)
  PE2 限流中文文案 (请求过于频繁 / 频繁访问)
  PE3 限流 + retry-after 秒数提取 (header / retryAfter / 中文 N秒后 / try again in)
  PE4 纯 retry-after 也判定为限流 (很多 API 错误体只给 retry-after)
  PE5 配额耗尽 (quota / 余额不足, 不可短期重试)
  PE6 其他 provider 错误 (Unauthorized, 不重试)
  PE7 空文本 / 无错误文本 → unknown
  PE8 retry-after 边界 (超大值截断到 300, 含堆栈的长文本只看头部)

运行: python3 tests/test_provider_error.py
依赖: 仅 Python 标准库 + shared/provider_error.py
"""

import os
import sys
import unittest

# shared/provider_error.py 是普通 .py, 直接 import
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared"))
from provider_error import parse_provider_error as pe  # noqa: E402


class TestParseProviderError(unittest.TestCase):
    """parse_provider_error 的分类与 retry-after 提取单测"""

    def _rl(self, text):
        """断言是限流, 返回完整结果供进一步检查。"""
        r = pe(text)
        self.assertTrue(r["is_rate_limit"], f"应判定为限流: {text!r} → {r}")
        return r

    def _not_rl(self, text, kind=None):
        r = pe(text)
        self.assertFalse(r["is_rate_limit"], f"不应判定为限流: {text!r} → {r}")
        if kind:
            self.assertEqual(r["error_kind"], kind)
        return r

    # ---------- PE0: 429 通用限流 ----------
    def test_pe0_429_rate_limit(self):
        """PE0: 含 429 → rate_limit"""
        r = self._rl("APICallError: Too Many Requests 429")
        self.assertEqual(r["error_kind"], "rate_limit")

    def test_pe0b_too_many_requests(self):
        """PE0b: "Too Many Requests" 文案 (无数字) → rate_limit"""
        self._rl("Error: Too Many Requests. Please slow down.")

    # ---------- PE1: 1302 z.ai 特定码 ----------
    def test_pe1_1302_zai_code(self):
        """PE1: z.ai 业务码 1302 → rate_limit"""
        r = self._rl("Error 1302: 请求过于频繁")
        self.assertEqual(r["error_kind"], "rate_limit")

    def test_pe1b_1302_alone(self):
        """PE1b: 单独 1302 → rate_limit"""
        self._rl("request failed with code 1302")

    # ---------- PE2: 中文限流文案 ----------
    def test_pe2_chinese_rate_limit(self):
        """PE2: 中文"请求过于频繁"/"频繁访问" → rate_limit"""
        self._rl("错误: 请求过于频繁,请稍后重试")
        self._rl("频繁访问, 触发限流")

    def test_pe2b_rate_limit_snake(self):
        """PE2b: rate_limit_error 文案 → rate_limit"""
        self._rl('{"error":{"type":"rate_limit_error"}}')

    # ---------- PE3: retry-after 提取 ----------
    def test_pe3_retry_after_header(self):
        """PE3: retry-after header 秒数提取"""
        r = self._rl("429 Too Many Requests\nretry-after: 30")
        self.assertEqual(r["retry_after_sec"], 30)

    def test_pe3b_retry_after_retryafter_json(self):
        """PE3b: retryAfter JSON 字段"""
        r = self._rl('429 {"error":{"retryAfter": "45"}}')
        self.assertEqual(r["retry_after_sec"], 45)

    def test_pe3c_retry_after_chinese_seconds(self):
        """PE3c: 中文 "请在 N 秒后" 提取"""
        r = self._rl("429 请在 60 秒后重试")
        self.assertEqual(r["retry_after_sec"], 60)

    def test_pe3d_retry_after_try_again(self):
        """PE3d: "try again in N seconds" 提取"""
        r = self._rl("429 Please try again in 20 seconds")
        self.assertEqual(r["retry_after_sec"], 20)

    # ---------- PE4: 纯 retry-after 判定限流 ----------
    def test_pe4_bare_retry_after_is_rate_limit(self):
        """PE4: 文本只有 retry-after (无 429) 也判为限流"""
        r = self._rl("retry-after: 30")
        self.assertTrue(r["is_rate_limit"])
        self.assertEqual(r["retry_after_sec"], 30)

    # ---------- PE5: 配额耗尽 ----------
    def test_pe5_quota_not_retryable(self):
        """PE5: quota/配额 → is_quota, 非限流 (不可短期重试)"""
        r = self._not_rl("quota exceeded for this month", kind="quota")
        self.assertTrue(r["is_quota"])

    def test_pe5b_balance_insufficient(self):
        """PE5b: 余额不足 → quota"""
        r = self._not_rl("余额不足, 请充值", kind="quota")
        self.assertTrue(r["is_quota"])

    def test_pe5c_quota_with_retry_after(self):
        """PE5c: quota 仍可携带 retry_after (供调用方决策; 超大值被截断到 300)"""
        r = pe("quota exceeded, retry-after: 3600")
        self.assertTrue(r["is_quota"])
        self.assertFalse(r["is_rate_limit"])
        self.assertEqual(r["retry_after_sec"], 300, "retry_after 被截断到 300")

    # ---------- PE6: 其他 provider 错误 ----------
    def test_pe6_unauthorized_not_rate_limit(self):
        """PE6: Unauthorized → provider_error, 非限流 (不重试)"""
        self._not_rl("APICallError: Unauthorized", kind="provider_error")

    def test_pe6b_invalid_key(self):
        """PE6b: invalid api key → provider_error"""
        self._not_rl("invalid x-api-key", kind="provider_error")

    # ---------- PE7: 空文本 / 无错误 ----------
    def test_pe7_empty_text(self):
        """PE7: 空文本 → unknown"""
        r = pe("")
        self.assertFalse(r["is_rate_limit"])
        self.assertEqual(r["error_kind"], "unknown")
        self.assertIsNone(r["retry_after_sec"])

    def test_pe7b_no_error_text(self):
        """PE7b: 正常无错文本 → unknown"""
        r = self._not_rl("这是一段正常的代码审查结论, 没有错误")
        self.assertEqual(r["error_kind"], "unknown")

    def test_pe7c_none_text(self):
        """PE7c: None 入参 → unknown (不崩)"""
        r = pe(None)
        self.assertEqual(r["error_kind"], "unknown")

    # ---------- PE8: 边界 ----------
    def test_pe8_retry_after_bounded(self):
        """PE8: retry-after 超大值截断到 300 (防 provider 异常导致长眠)"""
        r = self._rl("429 retry-after: 99999")
        self.assertEqual(r["retry_after_sec"], 300)

    def test_pe8b_long_stacktrace_head_recognized(self):
        """PE8b: 超长文本, 错误在头部 (<2000) → 能识别; 在尾部 (>2000) → 看不到"""
        # 错误在头部, 能识别
        head_text = "APICallError: 429 Too Many Requests\n" + "x" * 3000
        self.assertTrue(pe(head_text)["is_rate_limit"], "头部含 429 应被识别")
        # 错误在尾部 (>2000 字符后), 头部全是填充 → 看不到 (这是已知的截断行为)
        tail_text = "x" * 3000 + "\nAPICallError: 429 Too Many Requests"
        self.assertFalse(pe(tail_text)["is_rate_limit"],
                         "尾部 (>2000) 的错误不在取样头部, 识别不到 (已知截断行为)")

    def test_pe8c_raw_hint_skips_stacktrace(self):
        """PE8c: raw_hint 跳过 JS 堆栈行, 取有意义的首行"""
        text = "    at /Applications/ZCode.app/...\n    at process.x\nAPICallError: 429\n"
        r = pe(text)
        self.assertNotIn("at ", r["raw_hint"], "raw_hint 不应是堆栈行")
        self.assertIn("APICallError", r["raw_hint"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
