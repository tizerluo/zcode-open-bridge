"""
shared/provider_error.py — 解析 zcode headless 调用的 provider 限流/配额错误

zcode --prompt 失败时, provider 端错误 (限流/配额) 会出现在:
  - stderr 文本里 (APICallError / AI_APICallError 堆栈, Vercel AI SDK)
  - 或 --json 输出的 error 字段 (若有)

这些错误的文本形态 (从 zcode.cjs 与实测确认):
  - "429" / "Too Many Requests" / "RateLimit" / "rate_limit"  (通用 HTTP 限流)
  - "1302" (z.ai 特定业务码)
  - "retry-after" / "retryAfter" header 值
  - 中文: "请求过于频繁" / "频繁" / "配额"

本模块是纯函数, 无副作用, 便于单测。MCP/ACP bridge 各内嵌一份副本
(保持单文件可独立运行特性), 修改时请同步。

用法:
  from shared.provider_error import parse_provider_error
  info = parse_provider_error(stderr_text)
  if info["is_rate_limit"] and info["retry_after_sec"]:
      time.sleep(info["retry_after_sec"])
"""

import re


def parse_provider_error(text):
    """分析 zcode 调用输出文本, 判断是否 provider 限流/配额错误。

    Args:
        text: zcode 的 stderr / stdout / error 文本 (任意混合, 可含堆栈)。

    Returns:
        dict:
          is_rate_limit: bool   是否限流类错误 (可安全重试)
          is_quota: bool        是否配额/额度耗尽 (通常不可短期重试)
          retry_after_sec: int|None  建议等待秒数 (从 retry-after / "请 X 秒后" 提取)
          error_kind: str       "rate_limit"|"quota"|"provider_error"|"unknown"
          raw_hint: str         简短错误上下文 (脱敏, 仅用于日志/返回调用方)
    """
    if not text:
        return {"is_rate_limit": False, "is_quota": False, "retry_after_sec": None,
                "error_kind": "unknown", "raw_hint": ""}

    sample = text if len(text) <= 2000 else text[:2000]  # 只看头部, 避免大堆栈

    # 简短上下文: 取第一个非堆栈的有意义行
    raw_hint = _extract_hint(sample)

    lower = sample.lower()

    # 先尝试提取 retry-after (多处会用到)
    retry_after = _extract_retry_after(sample)

    # 限流 (可安全重试) — 明确限流词 (不含模糊的 exceeded)
    rate_limit_patterns = [
        r"\b429\b", r"too many requests", r"ratelimit", r"rate_limit",
        r"rate limit", r"\brate.?limit", r"\b1302\b", r"请求过于频繁",
        r"频繁访问", r"频繁请求", r"slow\s*down", r"throttl",
    ]
    if any(re.search(p, lower) for p in rate_limit_patterns):
        return {"is_rate_limit": True, "is_quota": False, "retry_after_sec": retry_after,
                "error_kind": "rate_limit", "raw_hint": raw_hint}

    # 配额/额度耗尽 (通常不可短期重试) — 用精确的 quota/credit/配额 词, 不用模糊的
    # "exceeded.*limit" (会误吃 "rate limit exceeded")。Codex review P1#2。
    quota_patterns = [
        r"\bquota\b", r"配额(不足|耗尽|用完|超限)", r"额度(不足|耗尽|用完)",
        r"余额不足", r"insufficient.*(quota|credit|balance)", r"\bcredit\b.*exhaust",
    ]
    if any(re.search(p, lower) for p in quota_patterns):
        return {"is_rate_limit": False, "is_quota": True, "retry_after_sec": retry_after,
                "error_kind": "quota", "raw_hint": raw_hint}

    # 明确带 retry-after 但无 quota/限流标志 → 视为限流兜底 (很多 API 只给 retry-after)
    if retry_after is not None:
        return {"is_rate_limit": True, "is_quota": False, "retry_after_sec": retry_after,
                "error_kind": "rate_limit", "raw_hint": raw_hint}

    # 其他 provider 错误 (有 APICallError 标志, 但不是限流/配额) → 不重试
    provider_patterns = [
        r"apicallerror", r"ai_apicallerror", r"providerbusinesserror",
        r"unauthorized", r"forbidden", r"invalid.*api.*key",
        r"internal\s*server", r"service\s*unavailable", r"bad\s*gateway",
    ]
    if any(re.search(p, lower) for p in provider_patterns):
        return {"is_rate_limit": False, "is_quota": False, "retry_after_sec": None,
                "error_kind": "provider_error", "raw_hint": raw_hint}

    return {"is_rate_limit": False, "is_quota": False, "retry_after_sec": None,
            "error_kind": "unknown", "raw_hint": raw_hint}


def _extract_hint(sample):
    """从错误文本提取简短上下文 (跳过堆栈行, 脱敏)。"""
    for line in sample.splitlines():
        line = line.strip()
        if not line:
            continue
        # 跳过 JS 堆栈行
        if line.startswith("at ") or line.startswith("    at "):
            continue
        if line.startswith("{") or line.startswith("[Object]") or "[Object]" in line:
            continue
        # 截断到合理长度
        if len(line) > 120:
            line = line[:120] + "..."
        return line
    return ""


def _extract_retry_after(sample):
    """从错误文本提取 retry-after 秒数 (支持英文/中文/header 形式)。

    匹配:
      "retry-after: 30" / "Retry-After: 30"
      "retryAfter: 30"
      "请在 30 秒后" / "请 30 秒后重试"
      "try again in 30 seconds"
    """
    # retry-after header (秒), 形如 retry-after: 30 或 retry-after: "30"
    m = re.search(r'retry-?after\s*[:=]\s*"?(\d+)', sample, re.IGNORECASE)
    if m:
        return _bounded(int(m.group(1)))
    # retryAfter JSON 字段
    m = re.search(r'retryAfter"\s*:\s*"?(\d+)', sample, re.IGNORECASE)
    if m:
        return _bounded(int(m.group(1)))
    # 中文 "请在 N 秒后" / "N 秒后重试"
    m = re.search(r"(\d+)\s*秒[后以]", sample)
    if m:
        return _bounded(int(m.group(1)))
    # "try again in N seconds"
    m = re.search(r"try\s+again\s+in\s+(\d+)", sample, re.IGNORECASE)
    if m:
        return _bounded(int(m.group(1)))
    return None


def _bounded(seconds, lo=1, hi=300):
    """限制 retry_after 到合理范围 [1, 300] 秒, 防止 provider 返回异常大值导致长眠。"""
    try:
        s = int(seconds)
    except (TypeError, ValueError):
        return None
    return max(lo, min(hi, s))


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        text = " ".join(sys.argv[1:])
    else:
        text = sys.stdin.read()
    import json
    print(json.dumps(parse_provider_error(text), indent=2, ensure_ascii=False))
