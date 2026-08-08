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

import contextlib
import io
import json
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared"))
from credentials import (  # noqa: E402
    _safe_host,
    is_stale_env_base_url,
    load_zcode_credentials,
    merge_env_with_creds,
)


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

    # ---------- C9: merge_env_with_creds 空串不覆盖 (Codex P0) ----------
    def test_c9_empty_env_not_override(self):
        """C9: 空串 env 视为未设置, 保留 config 值 (Codex P0 修复)"""
        c = self._creds(_config_with_provider(models={"GLM-5.2": {}}))
        merged = merge_env_with_creds(c, {"ZCODE_MODEL": "", "OTHER": "x"})
        self.assertEqual(merged["ZCODE_MODEL"], "GLM-5.2",
                         "空串 env 不应覆盖 config 的有效值")
        self.assertEqual(merged["OTHER"], "x", "非凭证 env 应保留")

    def test_c9b_nonempty_env_overrides(self):
        """C9b: 非空 env 覆盖 config (merge_env_with_creds)"""
        c = self._creds(_config_with_provider(models={"GLM-5.2": {}}))
        merged = merge_env_with_creds(c, {"ZCODE_MODEL": "GLM-5-Turbo"})
        self.assertEqual(merged["ZCODE_MODEL"], "GLM-5-Turbo")

    def test_c9c_empty_apikey_not_clear_config(self):
        """C9c: ANTHROPIC_API_KEY="" 不应清空 config 的 key"""
        c = self._creds(_config_with_provider(api_key="sk-real-key-123456"))
        merged = merge_env_with_creds(c, {"ANTHROPIC_API_KEY": ""})
        self.assertEqual(merged["ANTHROPIC_API_KEY"], "sk-real-key-123456")

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

    # ---------- C10: ZCODE_BASE_URL 残留检测 (App 切换 plan 后的残留自愈) ----------
    def _config_multi_provider(self, enabled_url, stale_url):
        """构造两个 provider 的 config: enabled 用 enabled_url, 另一个用 stale_url。"""
        return {
            "provider": {
                "builtin:zai-coding-plan": {
                    "enabled": True,
                    "options": {"baseURL": enabled_url, "apiKey": "key-good"},
                    "models": {"GLM-5.2": {}},
                },
                "builtin:zai-start-plan": {
                    "enabled": False,
                    "options": {"baseURL": stale_url, "apiKey": "jwt-stale"},
                    "models": {"GLM-5.2": {}},
                },
            }
        }

    def _multi_path(self, enabled_url, stale_url):
        """写多 provider config 到临时文件, 返回路径 (残留检测需 config_path)。"""
        return _write_config(self._config_multi_provider(enabled_url, stale_url))

    def test_c10_stale_env_self_heals(self):
        """C10: env baseURL 是 config 另一 provider 的残留 → 自愈用 config 值"""
        path = self._multi_path("https://api.z.ai/api/anthropic",
                                "https://zcode.z.ai/api/v1/zcode-plan/anthropic")
        self._tmp_paths.append(path)
        c = load_zcode_credentials(config_path=path)
        # env 设为 stale provider 的根域名 (App 注入的真实形态); 传 config_path 让检测读到临时 config
        merged = merge_env_with_creds(c, {"ZCODE_BASE_URL": "https://zcode.z.ai"},
                                      config_path=path)
        self.assertEqual(merged["ZCODE_BASE_URL"], "https://api.z.ai/api/anthropic",
                         "残留 env 应被自愈为 config enabled provider 的值")

    def test_c10b_stale_warning_fired(self):
        """C10b: 残留时 warn 回调被触发"""
        path = self._multi_path("https://api.z.ai/api/anthropic",
                                "https://zcode.z.ai/api/v1/zcode-plan/anthropic")
        self._tmp_paths.append(path)
        c = load_zcode_credentials(config_path=path)
        warnings = []
        merge_env_with_creds(c, {"ZCODE_BASE_URL": "https://zcode.z.ai"},
                             config_path=path, warn=warnings.append)
        self.assertTrue(any("残留" in w for w in warnings), "残留时应触发告警")

    def test_c10c_custom_endpoint_respected(self):
        """C10c: env baseURL 是用户自建 (不在 config 任何 provider) → 尊重 env"""
        path = self._multi_path("https://api.z.ai/api/anthropic",
                                "https://zcode.z.ai/api/v1/zcode-plan/anthropic")
        self._tmp_paths.append(path)
        c = load_zcode_credentials(config_path=path)
        merged = merge_env_with_creds(c, {"ZCODE_BASE_URL": "https://my-proxy.example.com"},
                                      config_path=path)
        self.assertEqual(merged["ZCODE_BASE_URL"], "https://my-proxy.example.com",
                         "自定义 endpoint 应被尊重 (issue #3 调试场景)")

    def test_c10d_consistent_no_warning(self):
        """C10d: env 与 config 一致 → 不告警, 用一致值"""
        path = self._multi_path("https://api.z.ai/api/anthropic",
                                "https://zcode.z.ai/api/v1/zcode-plan/anthropic")
        self._tmp_paths.append(path)
        c = load_zcode_credentials(config_path=path)
        warnings = []
        merged = merge_env_with_creds(c, {"ZCODE_BASE_URL": "https://api.z.ai/api/anthropic"},
                                      config_path=path, warn=warnings.append)
        self.assertEqual(merged["ZCODE_BASE_URL"], "https://api.z.ai/api/anthropic")
        self.assertEqual(warnings, [], "一致时不应告警")

    def test_c10e_stale_exact_url_also_heals(self):
        """C10e: env baseURL 是 stale provider 的完整 URL (非根域名) → 同样自愈"""
        stale = "https://zcode.z.ai/api/v1/zcode-plan/anthropic"
        path = self._multi_path("https://api.z.ai/api/anthropic", stale)
        self._tmp_paths.append(path)
        c = load_zcode_credentials(config_path=path)
        merged = merge_env_with_creds(c, {"ZCODE_BASE_URL": stale}, config_path=path)
        self.assertEqual(merged["ZCODE_BASE_URL"], "https://api.z.ai/api/anthropic")

    def test_c10f_is_stale_helper_host_matching(self):
        """C10f: is_stale_env_base_url 用 host 匹配 (根域名 vs 完整路径)"""
        all_urls = {"https://api.z.ai/api/anthropic",
                    "https://zcode.z.ai/api/v1/zcode-plan/anthropic"}
        # env 根域名 zcode.z.ai, config 是 api.z.ai → 残留 (host 不同, env host 在 all_urls 里)
        self.assertTrue(is_stale_env_base_url(
            "https://zcode.z.ai", "https://api.z.ai/api/anthropic", all_urls))
        # env 是自建 host → 非残留
        self.assertFalse(is_stale_env_base_url(
            "https://my-proxy.com", "https://api.z.ai/api/anthropic", all_urls))
        # env 与 config 一致 → 非残留
        self.assertFalse(is_stale_env_base_url(
            "https://api.z.ai/api/anthropic", "https://api.z.ai/api/anthropic", all_urls))

    def test_c10g_no_config_path_still_safe(self):
        """C10g: config_path 默认 (None) 也能安全检测, 不崩 (CI 无 ~/.zcode 时)"""
        # 不传 config_path; CI 环境 ~ 不存在 config, _all_provider_base_urls 返回空 set,
        # 残留检测应安全跳过 (自建 endpoint 尊重 env, 不崩)
        c = {"ZCODE_BASE_URL": "https://api.z.ai/api/anthropic"}
        merged = merge_env_with_creds(c, {"ZCODE_BASE_URL": "https://self-hosted.test"})
        self.assertEqual(merged["ZCODE_BASE_URL"], "https://self-hosted.test")


# ============================================================
# C11/C12: 内嵌副本与 shared 权威版的同步测试 (整体 review P2-1)
#   mcp-server / agent-help 为"单文件可独立运行"各内嵌了一份凭证逻辑副本,
#   这里断言它们与 shared/credentials.py 权威实现在同一 fixture 上行为一致, 防漂移。
# ============================================================
MCP_SERVER_PATH = os.path.join(
    os.path.dirname(__file__), "..", "packages", "mcp-server", "zcode-mcp-server"
)
AGENT_HELP_PATH = os.path.join(
    os.path.dirname(__file__), "..", "packages", "agent-help", "zcode-agent-help"
)

_CRED_KEYS = ("ZCODE_MODEL", "ZCODE_BASE_URL", "ANTHROPIC_API_KEY")


def _load_single_file_module(path, name):
    """exec 加载无后缀单文件组件 (去掉 __main__ 块), 与 test_mcp_protocol 同模式。"""
    mod = types.ModuleType(name)
    mod.__file__ = path
    with open(path) as f:
        code = f.read()
    code_no_main = code.split('if __name__ == "__main__":')[0]
    exec(code_no_main, mod.__dict__)
    return mod


def _isolated_env(test_case, home, **env):
    """patch os.environ: 移除三个凭证 key + HOME 指向隔离目录, 叠加 env 指定值。"""
    new = {k: v for k, v in os.environ.items() if k not in _CRED_KEYS}
    new["HOME"] = home
    new.update(env)
    p = mock.patch.dict(os.environ, new, clear=True)
    p.start()
    test_case.addCleanup(p.stop)


def _write_isolated_config(home, enabled_url="https://api.z.ai/api/anthropic",
                           stale_url="https://zcode.z.ai/api/v1/zcode-plan/anthropic"):
    """在隔离 HOME 里写两 provider 的 config (enabled + 一个 disabled 的 stale), 返回路径。"""
    cfg_dir = os.path.join(home, ".zcode", "v2")
    os.makedirs(cfg_dir, exist_ok=True)
    cfg_path = os.path.join(cfg_dir, "config.json")
    with open(cfg_path, "w") as f:
        json.dump({
            "provider": {
                "builtin:zai-coding-plan": {
                    "enabled": True,
                    "options": {"baseURL": enabled_url, "apiKey": "sk-good-key-123456"},
                    "models": {"GLM-5.2": {}},
                },
                "builtin:zai-start-plan": {
                    "enabled": False,
                    "options": {"baseURL": stale_url, "apiKey": "jwt-stale"},
                    "models": {"GLM-5.2": {}},
                },
            }
        }, f)
    return cfg_path


class TestMcpServerSync(unittest.TestCase):
    """C11: mcp-server 内嵌副本 (load_zcode_credentials / _merge_env_with_creds)
    与 shared 权威版行为一致 (整体 review P2-1)。

    mcp-server 副本用 Path.home() 定位 config、直读 os.environ (均不接受参数),
    故用 HOME 环境变量隔离 + patch os.environ 注入场景。
    """

    @classmethod
    def setUpClass(cls):
        cls.mcp = _load_single_file_module(MCP_SERVER_PATH, "zcode_mcp_server")

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cfg_path = _write_isolated_config(self._tmp.name)
        _isolated_env(self, self._tmp.name)  # 基线: 无凭证 env

    def _assert_merge_parity(self, scenario):
        """对拍: mcp 副本与权威版 merge 后, 三个凭证 key 完全一致。"""
        mcp_merged = self.mcp._merge_env_with_creds(self.mcp.load_zcode_credentials())
        ref_merged = merge_env_with_creds(
            load_zcode_credentials(config_path=self.cfg_path),
            dict(os.environ), config_path=self.cfg_path)
        for k in _CRED_KEYS:
            self.assertEqual(mcp_merged.get(k, ""), ref_merged.get(k, ""),
                             f"{scenario}: {k} 在 mcp-server 副本与权威版间不一致")
        return ref_merged

    def test_c11a_load_parity(self):
        """C11a: load_zcode_credentials 副本与权威版读同一 config 结果一致"""
        self.assertEqual(self.mcp.load_zcode_credentials(),
                         load_zcode_credentials(config_path=self.cfg_path))

    def test_c11b_merge_no_env(self):
        """C11b: 无凭证 env → 双份都用 config 值"""
        merged = self._assert_merge_parity("无 env")
        self.assertEqual(merged["ZCODE_MODEL"], "GLM-5.2")
        self.assertEqual(merged["ZCODE_BASE_URL"], "https://api.z.ai/api/anthropic")

    def test_c11c_empty_env_not_override(self):
        """C11c: 空串 env 不覆盖 (双份一致)"""
        _isolated_env(self, self._tmp.name, ZCODE_MODEL="")
        merged = self._assert_merge_parity("空串 env")
        self.assertEqual(merged["ZCODE_MODEL"], "GLM-5.2")

    def test_c11d_nonempty_env_overrides(self):
        """C11d: 非空 env 覆盖 (双份一致)"""
        _isolated_env(self, self._tmp.name, ZCODE_MODEL="GLM-5-Turbo")
        merged = self._assert_merge_parity("非空 env")
        self.assertEqual(merged["ZCODE_MODEL"], "GLM-5-Turbo")

    def test_c11e_stale_env_self_heals(self):
        """C11e: 残留 env (config 另一 provider 的 endpoint) → 双份都自愈为 enabled 值"""
        _isolated_env(self, self._tmp.name, ZCODE_BASE_URL="https://zcode.z.ai")
        merged = self._assert_merge_parity("残留 env")
        self.assertEqual(merged["ZCODE_BASE_URL"], "https://api.z.ai/api/anthropic")

    def test_c11f_custom_endpoint_respected(self):
        """C11f: 自建 endpoint (不在 config 任何 provider) → 双份都尊重 env"""
        _isolated_env(self, self._tmp.name, ZCODE_BASE_URL="https://my-proxy.example.com")
        merged = self._assert_merge_parity("自建 endpoint")
        self.assertEqual(merged["ZCODE_BASE_URL"], "https://my-proxy.example.com")


class TestAgentHelp(unittest.TestCase):
    """C12: agent-help 内嵌副本同步 + 本轮 review 修复的回归 (P1-1/3/4/5, P2-2/4/6)。

    agent-help 是单文件可独立运行设计, 不 import shared, 靠这里的对拍防漂移。
    """

    @classmethod
    def setUpClass(cls):
        cls.ah = _load_single_file_module(AGENT_HELP_PATH, "zcode_agent_help")

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def _run_main(self, argv):
        """调 agent-help main(), 返回 (rc, stdout, stderr)。"""
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "argv", ["zcode-agent-help"] + argv), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = self.ah.main()
        return rc, out.getvalue(), err.getvalue()

    def test_c12a_safe_host_parity(self):
        """C12a: agent-help._safe_host 与权威 _safe_host 逐值一致 (整体 review P1-1)"""
        cases = [
            "https://api.z.ai/api/anthropic",   # 带路径
            "https://zcode.z.ai",               # 根域名 (App 注入形态)
            "//zcode.z.ai/api/v1/x",            # 无 scheme → 补 https://
            "http://localhost:8080/path",       # 带端口
            "ftp://example.com/x",              # 非 http scheme
            "zcode.z.ai/api/v1/x",              # 无 // → hostname 解析不出 → None
            "", "not a url",
        ]
        for u in cases:
            self.assertEqual(self.ah._safe_host(u), _safe_host(u), f"_safe_host 漂移: {u!r}")
        # 无 scheme 补 https:// 分支必须存在 (P1-1 核心)
        self.assertEqual(self.ah._safe_host("//zcode.z.ai/x"), "https://zcode.z.ai")

    def test_c12b_section_missing_value(self):
        """C12b: --section 末尾无值 → 用法错误 + return 1, 不静默打印全量 (P1-3)"""
        rc, out, err = self._run_main(["--section"])
        self.assertEqual(rc, 1)
        self.assertIn("用法错误", err)
        self.assertEqual(out, "", "不应打印全量 JSON")

    def test_c12c_section_followed_by_flag(self):
        """C12c: --section 后随另一个 flag → 同样按用法错误处理 (P1-3)"""
        rc, out, err = self._run_main(["--section", "--pretty"])
        self.assertEqual(rc, 1)
        self.assertIn("用法错误", err)

    def test_c12d_empty_creds_prints_env(self):
        """C12d: config 无 enabled provider 但 env 有值 → 脱敏打印并标注 (P1-5)"""
        _isolated_env(self, self._tmp.name,
                      ZCODE_BASE_URL="https://zcode.z.ai",
                      ANTHROPIC_API_KEY="sk-abcdef1234567890")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = self.ah.print_injected_env(config_path="/nonexistent/config.json")
        self.assertEqual(rc, 0)
        text = out.getvalue()
        self.assertIn("config 无 enabled provider, 将直接使用 env 值", text)
        self.assertIn("https://zcode.z.ai", text)
        self.assertIn("sk-a...7890", text, "apiKey 应按 4+4 脱敏")
        self.assertNotIn("sk-abcdef1234567890", text, "不得泄露明文 key")

    def test_c12e_empty_creds_empty_env(self):
        """C12e: config 空且 env 也空 → 维持原 ❌ 提示 (P1-5 不改变该路径)"""
        _isolated_env(self, self._tmp.name)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = self.ah.print_injected_env(config_path="/nonexistent/config.json")
        self.assertEqual(rc, 0)
        self.assertIn("❌", out.getvalue())

    def test_c12f_probe_exception_safety(self):
        """C12f: zcode 二进制不可执行 (PermissionError) 时探测兜底不崩 (P1-4)"""
        err = io.StringIO()
        saved_run = self.ah.subprocess.run

        def fake_run(*a, **kw):
            raise PermissionError("cannot execute binary")

        self.ah.subprocess.run = fake_run
        try:
            with contextlib.redirect_stderr(err):
                self.assertIsNone(self.ah._run_zcode_json(["skills", "list"]))
                self.assertEqual(self.ah._get_version(), "unknown")
        finally:
            self.ah.subprocess.run = saved_run
        self.assertIn("探测失败", err.getvalue(), "探测失败应有 stderr 提示")

    def test_c12g_commands_non_list_guard(self):
        """C12g: commands list 返回 dict 型 commands → custom_commands=[] 不出垃圾 (P2-2)"""
        saved_run = self.ah.subprocess.run

        class _R:
            returncode = 0
            stdout = json.dumps({"commands": {"a": 1}})  # dict 而非 list

        self.ah.subprocess.run = lambda *a, **kw: _R()
        try:
            env = self.ah.discover_environment()
        finally:
            self.ah.subprocess.run = saved_run
        self.assertEqual(env["custom_commands"], [])

    def test_c12h_pretty_incomplete_environment(self):
        """C12h: print_pretty 对缺 key 的 environment 不 KeyError (P2-4)"""
        saved_run = self.ah.subprocess.run

        def fake_run(*a, **kw):
            raise OSError("no zcode")

        self.ah.subprocess.run = fake_run
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                cap = self.ah.build_full()
        finally:
            self.ah.subprocess.run = saved_run
        cap["environment"] = {}  # 模拟不完整探测结果
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.ah.print_pretty(cap)  # 不应抛 KeyError
        self.assertIn("unknown", out.getvalue())

    def test_c12i_overview_tested_against(self):
        """C12i: OVERVIEW 带 tested_against 语义注记 (P2-6)"""
        self.assertIn("tested_against", self.ah.OVERVIEW)
        self.assertIn("0.16.1", self.ah.OVERVIEW["tested_against"])

    def test_c12j_stale_env_diagnosed(self):
        """C12j: 残留 env 在 --print-injected-env 里被标注 🚫 (P1-1 行为级)"""
        cfg_path = _write_isolated_config(self._tmp.name)
        _isolated_env(self, self._tmp.name, ZCODE_BASE_URL="https://zcode.z.ai")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = self.ah.print_injected_env(config_path=cfg_path)
        self.assertEqual(rc, 0)
        self.assertIn("🚫 残留", out.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
