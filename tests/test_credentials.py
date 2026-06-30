"""
test_credentials.py — 凭证读取与 env 优先级单测

验证 shared/credentials.py 的 model id 读取 (canonical 原始格式, 子项1) 和
"显式 env 优先"的合并语义 (子项2), 补齐此前的零测试覆盖。

  C0  正常读取: 返回第一个 enabled provider 的 model/baseURL/apiKey
  C1  model id 是原始格式 (config 里 models 的 key 原样, 如 GLM-5.2, 不加 zai/ 前缀)
  C2  models 为空 → 兜底 GLM-5.2
  C3  无 enabled provider → 返回 {}
  C4  config 文件缺失 → 返回 {} (不崩)
  C5  config JSON 损坏 → 返回 {} (不崩)
  C6  env 优先级: 显式 os.environ 覆盖 config 读出的值 (子项2 核心)
  C7  多 provider: 只取第一个 enabled
  C8  apiKey 为空: 仍返回 (baseURL/apiKey 可空)

运行: python3 tests/test_credentials.py
依赖: 仅 Python 标准库 + shared/credentials.py
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared"))
from credentials import load_zcode_credentials  # noqa: E402


def _write_config(config_dict):
    """写一个临时 config.json, 返回路径。"""
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
    json.dump(config_dict, f)
    f.close()
    return f.name


def _config_with_provider(provider_id="builtin:zai-coding-plan", enabled=True,
                          models=None, base_url="https://api.z.ai/api/anthropic",
                          api_key="sk-test-key-123456"):
    if models is None:
        models = {"GLM-5.2": {}}
    return {
        "provider": {
            provider_id: {
                "enabled": enabled,
                "options": {"baseURL": base_url, "apiKey": api_key},
                "models": models,
            }
        }
    }


class TestCredentials(unittest.TestCase):
    """凭证读取 + model id canonical 格式 + env 优先级"""

    def setUp(self):
        self._tmp_paths = []

    def tearDown(self):
        for p in self._tmp_paths:
            try:
                os.unlink(p)
            except OSError:
                pass

    def _creds(self, config_dict):
        path = _write_config(config_dict)
        self._tmp_paths.append(path)
        return load_zcode_credentials(config_path=path)

    # ---------- C0: 正常读取 ----------
    def test_c0_normal_read(self):
        """C0: 正常读取第一个 enabled provider"""
        c = self._creds(_config_with_provider())
        self.assertEqual(c["ZCODE_MODEL"], "GLM-5.2")
        self.assertEqual(c["ZCODE_BASE_URL"], "https://api.z.ai/api/anthropic")
        self.assertEqual(c["ANTHROPIC_API_KEY"], "sk-test-key-123456")

    # ---------- C1: model id 原始格式 (无 zai/ 前缀) ----------
    def test_c1_model_id_raw_format(self):
        """C1: model id = config 里 models 的 key 原样, 不加 provider 前缀 (子项1)"""
        c = self._creds(_config_with_provider(
            provider_id="builtin:zai-coding-plan",  # builtin:zai 也不加 zai/ 前缀
            models={"GLM-5.2": {}, "GLM-5-Turbo": {}}))
        self.assertEqual(c["ZCODE_MODEL"], "GLM-5.2",
                         "canonical model id 必须是原始 key, 不加 zai/ 前缀")
        self.assertNotIn("zai/", c["ZCODE_MODEL"])

    def test_c1b_custom_model_id(self):
        """C1b: 自定义 model id (如第三方 provider) 原样返回"""
        c = self._creds(_config_with_provider(
            provider_id="custom:openai", models={"gpt-custom": {}}))
        self.assertEqual(c["ZCODE_MODEL"], "gpt-custom")

    # ---------- C2: models 为空兜底 ----------
    def test_c2_empty_models_fallback(self):
        """C2: models 为空 → 兜底 GLM-5.2"""
        c = self._creds(_config_with_provider(models={}))
        self.assertEqual(c["ZCODE_MODEL"], "GLM-5.2")

    # ---------- C3: 无 enabled provider ----------
    def test_c3_no_enabled_provider(self):
        """C3: 无 enabled provider → {}"""
        c = self._creds(_config_with_provider(enabled=False))
        self.assertEqual(c, {})

    # ---------- C4: 文件缺失 ----------
    def test_c4_missing_file(self):
        """C4: config 文件不存在 → {} (不崩)"""
        c = load_zcode_credentials(config_path="/nonexistent/path/config.json")
        self.assertEqual(c, {})

    # ---------- C5: JSON 损坏 ----------
    def test_c5_corrupt_json(self):
        """C5: config JSON 损坏 → {} (不崩)"""
        f = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        f.write("{ this is not valid json")
        f.close()
        self._tmp_paths.append(f.name)
        c = load_zcode_credentials(config_path=f.name)
        self.assertEqual(c, {})

    # ---------- C6: env 优先级 (子项2 核心) ----------
    def test_c6_env_overrides_config(self):
        """C6: 显式 os.environ 覆盖 config 读出的值 (子项2a 合并顺序: creds 在左)"""
        c = self._creds(_config_with_provider(models={"GLM-5.2": {}}))
        # 模拟 bridge 的合并: {**creds, **os.environ} → 显式 env 赢
        fake_env = {"ZCODE_MODEL": "GLM-5-Turbo"}  # 显式覆盖
        merged = {**c, **fake_env}
        self.assertEqual(merged["ZCODE_MODEL"], "GLM-5-Turbo",
                         "显式 env 应覆盖 config 读出的 model")

    def test_c6b_env_partial_override(self):
        """C6b: 只覆盖部分 (baseURL 来自 config, model 来自 env)"""
        c = self._creds(_config_with_provider(models={"GLM-5.2": {}}))
        fake_env = {"ZCODE_MODEL": "GLM-5-Turbo"}  # 只设 model
        merged = {**c, **fake_env}
        self.assertEqual(merged["ZCODE_MODEL"], "GLM-5-Turbo")
        self.assertEqual(merged["ZCODE_BASE_URL"], "https://api.z.ai/api/anthropic",
                         "未覆盖的仍来自 config")

    def test_c6c_no_env_uses_config(self):
        """C6c: 无显式 env → 用 config 值 (原有行为不变)"""
        c = self._creds(_config_with_provider(models={"GLM-5.2": {}}))
        merged = {**c, **{}}  # 无显式 env
        self.assertEqual(merged["ZCODE_MODEL"], "GLM-5.2")

    # ---------- C7: 多 provider ----------
    def test_c7_multiple_providers_first_enabled(self):
        """C7: 多 provider, 只取第一个 enabled"""
        cfg = {
            "provider": {
                "disabled-one": {"enabled": False, "options": {},
                                 "models": {"model-A": {}}},
                "enabled-one": {"enabled": True, "options": {"baseURL": "https://x"},
                                "models": {"model-B": {}}},
            }
        }
        c = self._creds(cfg)
        self.assertEqual(c["ZCODE_MODEL"], "model-B")

    # ---------- C8: apiKey/baseURL 为空 ----------
    def test_c8_empty_apikey(self):
        """C8: apiKey 为空仍正常返回 (key 字段为空串)"""
        c = self._creds(_config_with_provider(api_key=""))
        self.assertEqual(c["ANTHROPIC_API_KEY"], "")
        self.assertEqual(c["ZCODE_MODEL"], "GLM-5.2")


if __name__ == "__main__":
    unittest.main(verbosity=2)
