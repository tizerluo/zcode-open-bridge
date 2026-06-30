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
  from shared.credentials import load_zcode_credentials
  # 显式环境变量优先 (调试/覆盖用): 已设置的 ZCODE_MODEL 等会覆盖 config 读出的值。
  # 合并顺序: config 凭证作基底, os.environ 覆盖之。
  env = {**load_zcode_credentials(), **os.environ}
"""

import json
from pathlib import Path

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
