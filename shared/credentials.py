"""
shared/credentials.py — ZCode 凭证动态读取 (单一真相源)

从 ~/.zcode/v2/config.json 动态读取 zcode 的 model/baseURL/apiKey,
不明文存储, 不依赖 shell 环境变量继承。

为什么需要这个:
  zcode-mcp-server 和 zcode-acp-bridge 都会被 zcode 以子进程方式 spawn,
  不会继承用户 shell 里 export 的 ZCODE_MODEL 等环境变量。
  所以它们必须自己从配置文件读取凭证, 注入给 zcode 子进程。

设计说明:
  为了保持"单文件可独立运行"的特性 (用户复制一个文件就能用),
  mcp-server 和 acp-bridge 各自内嵌了一份本逻辑的副本 (标注"源自此处")。
  本文件是权威实现; 修改凭证逻辑时, 请同步更新两处副本。

用法:
  from shared.credentials import load_zcode_credentials, merge_env_with_creds
  # 显式非空环境变量优先 (调试/覆盖用), 空串视为未设置, 且自动检测 baseURL 残留。
  # 不要手写 {**creds, **os.environ} (会绕过空串保护和残留自愈), 用 merge_env_with_creds。
  env = merge_env_with_creds(load_zcode_credentials())
"""

import json
from pathlib import Path
from urllib.parse import urlparse

# zcode 桌面 App 的配置文件 (含 provider 凭证)
ZCODE_CREDS_PATH = Path.home() / ".zcode" / "v2" / "config.json"


def load_zcode_credentials(config_path=None):
    """从 ~/.zcode/v2/config.json 动态读取凭证。

    读取第一个 enabled 的 provider, 返回环境变量 dict:
      ZCODE_MODEL:       模型 ID (如 GLM-5.2)
      ZCODE_BASE_URL:    API 端点
      ANTHROPIC_API_KEY: API 密钥 (provider kind 为 anthropic)

    Args:
        config_path: 可选, 自定义配置文件路径 (测试用)。默认 ZCODE_CREDS_PATH。

    Returns:
        dict: 环境变量; 读取失败返回 {}。
    """
    path = config_path or ZCODE_CREDS_PATH
    try:
        with open(path) as f:
            cfg = json.load(f)
        # 找第一个 enabled 的 provider
        for _pid, p in cfg.get("provider", {}).items():
            if p.get("enabled"):
                opts = p.get("options", {})
                models = p.get("models", {})
                model_id = next(iter(models)) if models else "GLM-5.2"
                return {
                    "ZCODE_MODEL": model_id,
                    "ZCODE_BASE_URL": opts.get("baseURL", ""),
                    "ANTHROPIC_API_KEY": opts.get("apiKey", ""),
                }
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        # 生产环境: 调用方负责日志; 这里静默返回 {} 由上层兜底
        return {}
    except Exception:
        return {}
    return {}


def _all_provider_base_urls(config_path=None):
    """读取 config 里所有 provider 的 baseURL 集合 (用于残留检测)。

    用于判断 env 里的 ZCODE_BASE_URL 是否指向"config 里某个其他 provider"
    (即 App 注入的残留), 而非用户自建 endpoint (合法自定义)。
    """
    path = config_path or ZCODE_CREDS_PATH
    urls = set()
    try:
        with open(path) as f:
            cfg = json.load(f)
        for p in cfg.get("provider", {}).values():
            bu = p.get("options", {}).get("baseURL", "")
            if bu:
                urls.add(bu)
    except Exception:
        pass
    return urls


def is_stale_env_base_url(env_base_url, config_base_url, all_config_urls):
    """判断 env 的 baseURL 是否是 App 注入的"残留官方 endpoint"。

    ZCode App 切换过 plan 的用户: 旧 plan 的 baseURL 会残留在子进程 env 里
    (即使该 plan 已 not_entitled), 而 config 的 enabled provider 是正确的。
    本函数精确区分两种"env 与 config 不同":
      - 残留: env 值 ∈ all_config_urls (是 config 里另一个 provider 的官方 endpoint)
              → 应自愈用 config 值
      - 自定义: env 值 ∉ all_config_urls (用户故意指向自建/代理)
              → 尊重 env (issue #3 调试场景)

    Args:
        env_base_url: env 里的 ZCODE_BASE_URL
        config_base_url: config enabled provider 的 baseURL
        all_config_urls: _all_provider_base_urls() 的返回

    Returns:
        True 若判定为残留 (建议用 config 值); False 若尊重 env。
    """
    if not env_base_url or not config_base_url:
        return False
    if env_base_url == config_base_url:
        return False  # 一致, 无问题
    # env 值是 config 里某个 provider 的官方 endpoint (但不是 enabled 那个) → 残留。
    # 注意: App 注入的可能是根域名 (如 https://zcode.z.ai), config 存的是完整路径
    # (如 https://zcode.z.ai/api/v1/zcode-plan/anthropic), 故用 host 匹配而非精确匹配。
    env_host = _safe_host(env_base_url)
    if not env_host:
        return False
    config_hosts = {_safe_host(u) for u in all_config_urls}
    config_host = _safe_host(config_base_url)
    # env 的 host 等于某个 config provider 的 host, 但不等于 enabled 那个 → 残留
    return env_host in config_hosts and env_host != config_host


def _safe_host(url):
    """提取 URL 的 host (scheme://host[:port]), 解析失败返回 None。"""
    try:
        p = urlparse(url)
        if not p.hostname:
            return None
        return f"{p.scheme}://{p.netloc}" if p.scheme else f"https://{p.netloc}"
    except Exception:
        return None


# 受凭证注入影响的 env key (显式设置时优先于 config, 但空串视为未设置)
_CRED_ENV_KEYS = ("ZCODE_MODEL", "ZCODE_BASE_URL", "ANTHROPIC_API_KEY")


def merge_env_with_creds(creds=None, environ=None, config_path=None, warn=None):
    """合并 config 凭证与环境变量, 实现"显式 env 优先, 空串视为未设置,
    且自动检测/自愈 App 注入的残留 baseURL"。

    issue #3 子项2: 让 ZCODE_MODEL=xxx 临时覆盖生效, 但避免空串 env
    (如 ZCODE_MODEL="") 把 config 的有效值覆盖成空。

    残留检测 (产品级): ZCode App 切换过 plan 的用户, 旧 plan 的 baseURL 会
    残留在子进程 env (即使已 not_entitled)。若 env 的 ZCODE_BASE_URL 是 config
    里"另一个 provider"的官方 endpoint (非 enabled 那个), 判定为残留 → 用 config
    值 + 告警。用户自建 endpoint (不在 config 任何 provider 里) 则正常尊重 env。

    Args:
        creds: load_zcode_credentials() 的返回 (config 凭证), 默认 {}
        environ: 环境变量 dict, 默认 os.environ
        config_path: 可选, 用于读 all_provider_urls 做残留判定
        warn: 可选回调 fn(msg), 用于告警 (通常传 log)

    Returns:
        合并后的 env dict: config 凭证作基底, 仅非空显式 env 覆盖之 (残留除外)。
    """
    import os
    if creds is None:
        creds = {}
    if environ is None:
        environ = os.environ
    # 基底: 完整 os.environ + config 凭证 (config 覆盖 os.environ 里的同名空串/旧值)
    merged = {**environ, **creds}
    # 显式非空 env 再覆盖回去 (解决"显式 env 优先")
    for k in _CRED_ENV_KEYS:
        v = environ.get(k)
        if v:  # 仅非空才覆盖 (空串视为未设置, 保留 config 值)
            merged[k] = v

    # 残留检测: env 的 baseURL 是否是 App 注入的"另一个 plan 的官方 endpoint"
    env_bu = environ.get("ZCODE_BASE_URL", "")
    config_bu = creds.get("ZCODE_BASE_URL", "")
    if env_bu and config_bu and env_bu != config_bu:
        all_urls = _all_provider_base_urls(config_path)
        if is_stale_env_base_url(env_bu, config_bu, all_urls):
            # 残留: env 值是 config 里另一个 provider 的官方 endpoint → 自愈用 config 值
            merged["ZCODE_BASE_URL"] = config_bu
            if warn:
                warn(f"⚠ ZCODE_BASE_URL 残留检测: env='{env_bu}' 是 config 里另一个 "
                     f"provider 的 endpoint, 与 enabled provider (config='{config_bu}') "
                     f"不一致。已自动用 config 值。若需用该 endpoint, 请在 config 切换 "
                     f"provider 或用自建 endpoint (非 config 内置)。")
    return merged


if __name__ == "__main__":
    # 自测: 打印读取到的凭证 (脱敏)
    creds = load_zcode_credentials()
    if creds:
        print("✅ 读取成功:")
        print(f"  ZCODE_MODEL:    {creds.get('ZCODE_MODEL')}")
        print(f"  ZCODE_BASE_URL: {creds.get('ZCODE_BASE_URL')}")
        key = creds.get("ANTHROPIC_API_KEY", "")
        print(f"  ANTHROPIC_API_KEY: {key[:8]}...{key[-4:]}" if len(key) > 12 else "  (短或空)")
    else:
        print(f"❌ 未从 {ZCODE_CREDS_PATH} 读取到凭证")
