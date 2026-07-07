"""
test_app_server_methods.py — app-server 新协议方法 (0.15.0+) 单测

用 FakeBackend 替换真实 zcode 子进程, 验证 ACPBridge.handle_acp() 能把
新协议方法 (session 级 0.15.0+ 与 workspace/* 0.15.0+) 正确路由、转换参数、
透传复杂对象、回显错误, 并覆盖旧 handler (fork/rewind/goal 等) 此前的测试盲区。

  M0  FakeBackend 基建 (路由 + 默认响应)
  M1  session/setThoughtLevel (参数透传 + 缺参错误 + 错误回显)
  M2  session/updateRuntimeModelConfig (复杂嵌套对象原样透传)
  M3  session/cancelBackgroundTask (taskId 透传)
  M4  session/rewindCascade (target/scope 构造, 与 rewind 对比)
  M5  session/setModel + session/setMode (0.14.8 旧方法补齐)
  M6  workspace/_resolve_workspace 选择器 (workspace dict / workspacePath / cwd / 默认)
  M7  workspace/readState + workspace/generateText (含 timeout 60s)
  M8  workspace/setDefault* 三件套 (乐观锁透传)
  M9  workspace Provider 管理 (apiKey 嵌套对象透传保真 + 日志/错误回显双脱敏)
  M10 旧 handler 回归 (fork/rewind/goal/compact/steer 路由, 补盲区)
  PE  prompt/enhance 同步 + cancel (3.3.0+, 透传 + 缺参 + 错误回显)

运行: python3 tests/test_app_server_methods.py
依赖: 仅 Python 标准库 + 本项目的 acp-bridge 模块
"""

import contextlib
import io
import os
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
    exec(code_no_main, mod.__dict__)
    return mod


class FakeBackend:
    """替代真实 ZCodeBackend, 记录所有 request() 调用, 按预设返回响应。

    - self.calls: 每次调用的 {"id", "method", "params", "timeout"} 列表
    - self.next_response: 下一次 request() 返回的响应 dict (默认成功空结果)
    """

    def __init__(self):
        self.calls = []
        self.next_response = {"result": {"ok": True}}

    def request(self, msg_id, method, params=None, timeout=30):
        self.calls.append({
            "id": msg_id, "method": method,
            "params": params or {}, "timeout": timeout,
        })
        return self.next_response, []

    def send(self, msg):
        # 仅用于兼容 (本次测试不覆盖 fire-and-forget 路径)
        pass

    # prompt/enhance/start 的 listener 桩 (M10 注册完整性测试会调到 start handler;
    # 真正的异步等待逻辑测试在 test_prompt_enhance.py)。这里立即塞一条 cancelled
    # 结果, 让 start handler 快速返回, 避免 M10 阻塞在 120s 等待上。
    # 返回元组 (q, error) 与真实 ZCodeBackend.register_enhance_listener 签名一致。
    def register_enhance_listener(self, request_id):
        import queue as _q
        q = _q.Queue()
        q.put({"requestId": request_id, "status": "cancelled"})
        return q, None

    def unregister_enhance_listener(self, request_id):
        pass


class TestAppServerMethods(unittest.TestCase):
    """app-server 新协议方法 (0.15.0+) 的路由/参数转换/透传/错误 单测"""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_bridge_module()
        cls.Bridge = cls.mod.ACPBridge

    def _new_bridge(self):
        """构造一个注入了 FakeBackend 的 bridge (跳过真实子进程)。"""
        b = self.Bridge()
        fake = FakeBackend()
        b.backend = fake
        return b, fake

    def _call(self, bridge, method, params=None, msg_id=1):
        """封装一次 handle_acp 调用。"""
        req = {"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params or {}}
        return bridge.handle_acp(req)

    def _assert_ok(self, resp, msg=None):
        """断言响应是成功 (含 result, 无 error)。"""
        self.assertNotIn("error", resp, f"期望成功, 实际报错: {resp.get('error')} ({msg})")
        self.assertIn("result", resp)

    def _assert_error_code(self, resp, code, msg=None):
        self.assertEqual(resp.get("error", {}).get("code"), code,
                         f"期望错误码 {code}, 实际: {resp} ({msg})")

    # ---------- M0: FakeBackend 基建 ----------
    def test_m0_basic_routing(self):
        """M0: 已有方法 (initialize) 正常路由, 证明 FakeBackend 注入生效"""
        bridge, fake = self._new_bridge()
        resp = self._call(bridge, "initialize")
        self._assert_ok(resp)
        self.assertEqual(fake.calls, [], "initialize 不应调 backend")

    # ---------- M1: session/setThoughtLevel ----------
    def test_m1_set_thought_level_passthrough(self):
        """M1: setThoughtLevel 透传 thoughtLevel (动态值, 不做 enum 硬校验)"""
        bridge, fake = self._new_bridge()
        resp = self._call(bridge, "session/setThoughtLevel",
                          {"sessionId": "sess_x", "thoughtLevel": "high"})
        self._assert_ok(resp)
        self.assertEqual(fake.calls[0]["method"], "session/setThoughtLevel")
        self.assertEqual(fake.calls[0]["params"]["thoughtLevel"], "high")
        self.assertEqual(fake.calls[0]["params"]["sessionId"], "sess_x")

    def test_m1_set_thought_level_missing_param(self):
        """M1a: 缺 thoughtLevel → -32602"""
        bridge, _ = self._new_bridge()
        resp = self._call(bridge, "session/setThoughtLevel", {"sessionId": "sess_x"})
        self._assert_error_code(resp, -32602)

    def test_m1_set_thought_level_backend_error(self):
        """M1b: backend 返回 error → -32603"""
        bridge, fake = self._new_bridge()
        fake.next_response = {"error": {"message": "model has no reasoning levels"}}
        resp = self._call(bridge, "session/setThoughtLevel",
                          {"sessionId": "sess_x", "thoughtLevel": "high"})
        self._assert_error_code(resp, -32603)

    # ---------- M2: session/updateRuntimeModelConfig ----------
    def test_m2_update_runtime_model_config_passthrough(self):
        """M2: 复杂嵌套 runtimeModel 原样透传不被篡改"""
        bridge, fake = self._new_bridge()
        runtime_model = {
            "revision": "r1", "generatedAt": 1700000000000,
            "model": {"providerId": "zai", "modelId": "glm-5.2", "variant": "v1"},
            "provider": {"providerId": "zai", "kind": "openai-compatible",
                         "providerOptions": {"baseURL": "https://x", "temperature": 0.7}},
            "thoughtLevel": "standard",
        }
        resp = self._call(bridge, "session/updateRuntimeModelConfig",
                          {"sessionId": "sess_x", "runtimeModel": runtime_model,
                           "applyModelSelection": False})
        self._assert_ok(resp)
        self.assertEqual(fake.calls[0]["params"]["runtimeModel"], runtime_model,
                         "runtimeModel 必须原样透传")
        self.assertEqual(fake.calls[0]["params"]["applyModelSelection"], False)

    def test_m2_update_runtime_model_config_missing(self):
        """M2a: 缺 runtimeModel → -32602"""
        bridge, _ = self._new_bridge()
        resp = self._call(bridge, "session/updateRuntimeModelConfig", {"sessionId": "sess_x"})
        self._assert_error_code(resp, -32602)

    # ---------- M3: session/cancelBackgroundTask ----------
    def test_m3_cancel_background_task_passthrough(self):
        """M3: cancelBackgroundTask 透传 taskId"""
        bridge, fake = self._new_bridge()
        resp = self._call(bridge, "session/cancelBackgroundTask",
                          {"sessionId": "sess_x", "taskId": "task_42"})
        self._assert_ok(resp)
        self.assertEqual(fake.calls[0]["method"], "session/cancelBackgroundTask")
        self.assertEqual(fake.calls[0]["params"]["taskId"], "task_42")

    def test_m3_cancel_background_task_missing(self):
        """M3a: 缺 taskId → -32602"""
        bridge, _ = self._new_bridge()
        resp = self._call(bridge, "session/cancelBackgroundTask", {"sessionId": "sess_x"})
        self._assert_error_code(resp, -32602)

    # ---------- M4: session/rewindCascade ----------
    def test_m4_rewind_cascade_default_target(self):
        """M4: rewindCascade 默认 target = {kind: latestCheckpoint}"""
        bridge, fake = self._new_bridge()
        resp = self._call(bridge, "session/rewindCascade", {"sessionId": "sess_x"})
        self._assert_ok(resp)
        self.assertEqual(fake.calls[0]["method"], "session/rewindCascade")
        self.assertEqual(fake.calls[0]["params"]["target"], {"kind": "latestCheckpoint"})

    def test_m4_rewind_cascade_checkpoint_and_scope(self):
        """M4a: rewindCascade 显式 checkpointId → {kind: checkpoint}, 且 scope 透传"""
        bridge, fake = self._new_bridge()
        resp = self._call(bridge, "session/rewindCascade",
                          {"sessionId": "sess_x", "checkpointId": "cp1", "scope": "both"})
        self._assert_ok(resp)
        self.assertEqual(fake.calls[0]["params"]["target"],
                         {"kind": "checkpoint", "checkpointId": "cp1"})
        self.assertEqual(fake.calls[0]["params"]["scope"], "both")

    def test_m4_rewind_cascade_differs_from_rewind(self):
        """M4b: rewindCascade 与 rewind 调用不同 method 名 (确认 dispatch 不混淆)"""
        bridge1, fake1 = self._new_bridge()
        bridge2, fake2 = self._new_bridge()
        self._call(bridge1, "session/rewindCascade", {"sessionId": "sess_x"})
        self._call(bridge2, "session/rewind", {"sessionId": "sess_x"})
        self.assertEqual(fake1.calls[0]["method"], "session/rewindCascade")
        self.assertEqual(fake2.calls[0]["method"], "session/rewind")

    # ---------- M5: session/setModel + session/setMode (补齐) ----------
    def test_m5_set_model_passthrough(self):
        """M5: setModel 透传 modelId"""
        bridge, fake = self._new_bridge()
        resp = self._call(bridge, "session/setModel",
                          {"sessionId": "sess_x", "modelId": "glm-5.2"})
        self._assert_ok(resp)
        self.assertEqual(fake.calls[0]["method"], "session/setModel")
        self.assertEqual(fake.calls[0]["params"]["modelId"], "glm-5.2")

    def test_m5_set_model_missing(self):
        """M5a: 缺 modelId → -32602"""
        bridge, _ = self._new_bridge()
        resp = self._call(bridge, "session/setModel", {"sessionId": "sess_x"})
        self._assert_error_code(resp, -32602)

    def test_m5_set_mode_passthrough(self):
        """M5b: setMode 透传 mode"""
        bridge, fake = self._new_bridge()
        resp = self._call(bridge, "session/setMode",
                          {"sessionId": "sess_x", "mode": "plan"})
        self._assert_ok(resp)
        self.assertEqual(fake.calls[0]["method"], "session/setMode")
        self.assertEqual(fake.calls[0]["params"]["mode"], "plan")

    def test_m5_set_mode_missing(self):
        """M5c: 缺 mode → -32602"""
        bridge, _ = self._new_bridge()
        resp = self._call(bridge, "session/setMode", {"sessionId": "sess_x"})
        self._assert_error_code(resp, -32602)

    # ---------- M6: _resolve_workspace 选择器 ----------
    def test_m6_workspace_from_dict(self):
        """M6: params["workspace"] 是合法 dict → 直接透传"""
        bridge, fake = self._new_bridge()
        ws = {"workspacePath": "/p/a", "workspaceKey": "/p/a", "workspaceIdentity": "/p/a"}
        self._call(bridge, "workspace/readState", {"workspace": ws})
        self.assertEqual(fake.calls[0]["params"]["workspace"], ws)

    def test_m6_workspace_from_workspace_path(self):
        """M6a: params["workspacePath"] → 构造 {workspacePath, workspaceKey}"""
        bridge, fake = self._new_bridge()
        self._call(bridge, "workspace/readState", {"workspacePath": "/p/b"})
        self.assertEqual(fake.calls[0]["params"]["workspace"],
                         {"workspacePath": "/p/b", "workspaceKey": "/p/b"})

    def test_m6_workspace_from_cwd(self):
        """M6b: params["cwd"] → 构造选择器 (与 session/new 一致的 key)"""
        bridge, fake = self._new_bridge()
        self._call(bridge, "workspace/readState", {"cwd": "/p/c"})
        self.assertEqual(fake.calls[0]["params"]["workspace"],
                         {"workspacePath": "/p/c", "workspaceKey": "/p/c"})

    def test_m6_workspace_default_getcwd(self):
        """M6c: 无 workspace 相关参数 → 用 os.getcwd() 兜底"""
        bridge, fake = self._new_bridge()
        self._call(bridge, "workspace/readState", {})
        ws = fake.calls[0]["params"]["workspace"]
        self.assertEqual(ws["workspacePath"], os.getcwd())
        self.assertEqual(ws["workspaceKey"], os.getcwd())

    def test_m6_workspace_dict_without_path_falls_back(self):
        """M6d: workspace dict 但缺 workspacePath → 回退到 cwd 解析"""
        bridge, fake = self._new_bridge()
        self._call(bridge, "workspace/readState", {"workspace": {"foo": "bar"}})
        ws = fake.calls[0]["params"]["workspace"]
        self.assertIn("workspacePath", ws, "非法 workspace dict 应回退到路径构造")

    # ---------- M7: workspace/readState + workspace/generateText ----------
    def test_m7_read_state_passthrough(self):
        """M7: readState 透传 workspace + 可选 runtimeModel"""
        bridge, fake = self._new_bridge()
        rm = {"revision": "r1", "model": {"providerId": "zai", "modelId": "glm-5.2"}}
        resp = self._call(bridge, "workspace/readState",
                          {"workspacePath": "/p", "runtimeModel": rm})
        self._assert_ok(resp)
        self.assertEqual(fake.calls[0]["method"], "workspace/readState")
        self.assertEqual(fake.calls[0]["params"]["runtimeModel"], rm)

    def test_m7_generate_text_passthrough(self):
        """M7a: generateText 透传 modelRef/prompt/querySource/maxOutputTokens/temperature"""
        bridge, fake = self._new_bridge()
        resp = self._call(bridge, "workspace/generateText", {
            "workspacePath": "/p",
            "modelRef": {"providerId": "zai", "modelId": "glm-5.2"},
            "prompt": "say hi", "querySource": "editor",
            "maxOutputTokens": 512, "temperature": 0.5,
        })
        self._assert_ok(resp)
        p = fake.calls[0]["params"]
        self.assertEqual(fake.calls[0]["method"], "workspace/generateText")
        self.assertEqual(p["modelRef"], {"providerId": "zai", "modelId": "glm-5.2"})
        self.assertEqual(p["prompt"], "say hi")
        self.assertEqual(p["querySource"], "editor")
        self.assertEqual(p["maxOutputTokens"], 512)
        self.assertEqual(p["temperature"], 0.5)

    def test_m7_generate_text_timeout_60(self):
        """M7b: generateText timeout=60 (涉及模型调用)"""
        bridge, fake = self._new_bridge()
        self._call(bridge, "workspace/generateText", {
            "workspacePath": "/p",
            "modelRef": {"providerId": "zai", "modelId": "glm-5.2"}, "prompt": "x",
        })
        self.assertEqual(fake.calls[0]["timeout"], 60)

    def test_m7_generate_text_default_query_source(self):
        """M7c: generateText 缺 querySource → 默认 "bridge" """
        bridge, fake = self._new_bridge()
        self._call(bridge, "workspace/generateText", {
            "workspacePath": "/p",
            "modelRef": {"providerId": "zai", "modelId": "glm-5.2"}, "prompt": "x",
        })
        self.assertEqual(fake.calls[0]["params"]["querySource"], "bridge")

    def test_m7_generate_text_missing_model_ref(self):
        """M7d: 缺 modelRef → -32602"""
        bridge, _ = self._new_bridge()
        resp = self._call(bridge, "workspace/generateText",
                          {"workspacePath": "/p", "prompt": "x"})
        self._assert_error_code(resp, -32602)

    def test_m7_generate_text_missing_prompt(self):
        """M7e: 缺 prompt → -32602"""
        bridge, _ = self._new_bridge()
        resp = self._call(bridge, "workspace/generateText",
                          {"workspacePath": "/p",
                           "modelRef": {"providerId": "zai", "modelId": "glm-5.2"}})
        self._assert_error_code(resp, -32602)

    # ---------- M8: workspace/setDefault* 三件套 ----------
    def test_m8_set_default_model(self):
        """M8: setDefaultModel 透传 model + 乐观锁 expectedWorkspaceRevision"""
        bridge, fake = self._new_bridge()
        resp = self._call(bridge, "workspace/setDefaultModel", {
            "workspacePath": "/p",
            "model": {"providerId": "zai", "modelId": "glm-5.2"},
            "expectedWorkspaceRevision": 7,
        })
        self._assert_ok(resp)
        p = fake.calls[0]["params"]
        self.assertEqual(fake.calls[0]["method"], "workspace/setDefaultModel")
        self.assertEqual(p["model"], {"providerId": "zai", "modelId": "glm-5.2"})
        self.assertEqual(p["expectedWorkspaceRevision"], 7)

    def test_m8_set_default_model_missing(self):
        """M8a: 缺 model → -32602"""
        bridge, _ = self._new_bridge()
        resp = self._call(bridge, "workspace/setDefaultModel", {"workspacePath": "/p"})
        self._assert_error_code(resp, -32602)

    def test_m8_set_default_mode(self):
        """M8b: setDefaultMode 透传 mode + 乐观锁"""
        bridge, fake = self._new_bridge()
        resp = self._call(bridge, "workspace/setDefaultMode", {
            "workspacePath": "/p", "mode": "build", "expectedWorkspaceRevision": 3,
        })
        self._assert_ok(resp)
        self.assertEqual(fake.calls[0]["method"], "workspace/setDefaultMode")
        self.assertEqual(fake.calls[0]["params"]["mode"], "build")
        self.assertEqual(fake.calls[0]["params"]["expectedWorkspaceRevision"], 3)

    def test_m8_set_default_mode_missing(self):
        """M8c: 缺 mode → -32602"""
        bridge, _ = self._new_bridge()
        resp = self._call(bridge, "workspace/setDefaultMode", {"workspacePath": "/p"})
        self._assert_error_code(resp, -32602)

    def test_m8_set_default_thought_level(self):
        """M8d: setDefaultThoughtLevel 透传 thoughtLevel"""
        bridge, fake = self._new_bridge()
        resp = self._call(bridge, "workspace/setDefaultThoughtLevel", {
            "workspacePath": "/p", "thoughtLevel": "concise",
        })
        self._assert_ok(resp)
        self.assertEqual(fake.calls[0]["method"], "workspace/setDefaultThoughtLevel")
        self.assertEqual(fake.calls[0]["params"]["thoughtLevel"], "concise")

    def test_m8_set_default_thought_level_missing(self):
        """M8e: 缺 thoughtLevel → -32602"""
        bridge, _ = self._new_bridge()
        resp = self._call(bridge, "workspace/setDefaultThoughtLevel",
                          {"workspacePath": "/p"})
        self._assert_error_code(resp, -32602)

    # ---------- M9: workspace Provider 管理 (apiKey 透传 + 脱敏) ----------
    def test_m9_upsert_provider_passthrough_with_apikey(self):
        """M9: upsertModelProvider 透传含 apiKey 的 provider 对象 (保真)"""
        bridge, fake = self._new_bridge()
        provider = {
            "providerId": "custom", "kind": "openai-compatible", "baseURL": "https://x",
            "apiKey": {"source": "inline", "value": "sk-secret-do-not-leak-123"},
            "models": [{"modelId": "m1"}],
        }
        resp = self._call(bridge, "workspace/upsertModelProvider",
                          {"workspacePath": "/p", "provider": provider})
        self._assert_ok(resp)
        # 透传必须保真: provider 整体原样到达 backend
        self.assertEqual(fake.calls[0]["params"]["provider"], provider)
        self.assertEqual(fake.calls[0]["method"], "workspace/upsertModelProvider")

    def test_m9_upsert_provider_missing(self):
        """M9a: 缺 provider → -32602"""
        bridge, _ = self._new_bridge()
        resp = self._call(bridge, "workspace/upsertModelProvider",
                          {"workspacePath": "/p"})
        self._assert_error_code(resp, -32602)

    def test_m9_upsert_provider_log_no_apikey(self):
        """M9b: upsertModelProvider 的 log 不得包含 apiKey 明文 (脱敏验证)"""
        bridge, fake = self._new_bridge()
        # 捕获 stderr 日志 (模块级 log() 写 sys.stderr)
        captured = io.StringIO()
        provider = {
            "providerId": "custom",
            "apiKey": {"source": "inline", "value": "sk-LEAK-MARKER-xyz"},
            "models": [{"modelId": "m1"}],
        }
        with contextlib.redirect_stderr(captured):
            bridge2 = self.Bridge()
            bridge2.backend = FakeBackend()
            bridge2.handle_acp({"jsonrpc": "2.0", "id": 1,
                                "method": "workspace/upsertModelProvider",
                                "params": {"workspacePath": "/p", "provider": provider}})
        log_out = captured.getvalue()
        self.assertNotIn("sk-LEAK-MARKER-xyz", log_out,
                         "log 不得泄露 apiKey 明文")
        self.assertIn("custom", log_out, "log 应打 providerId 便于排查")

    def test_m9_remove_provider(self):
        """M9c: removeModelProvider 透传 providerId"""
        bridge, fake = self._new_bridge()
        resp = self._call(bridge, "workspace/removeModelProvider",
                          {"workspacePath": "/p", "providerId": "old"})
        self._assert_ok(resp)
        self.assertEqual(fake.calls[0]["method"], "workspace/removeModelProvider")
        self.assertEqual(fake.calls[0]["params"]["providerId"], "old")

    def test_m9_remove_provider_missing(self):
        """M9d: 缺 providerId → -32602"""
        bridge, _ = self._new_bridge()
        resp = self._call(bridge, "workspace/removeModelProvider",
                          {"workspacePath": "/p"})
        self._assert_error_code(resp, -32602)

    def test_m9_update_registry_passthrough_with_apikey(self):
        """M9e: updateProviderRegistry 透传含 apiKey 的 providers (保真), 且 log 只打数量"""
        bridge, fake = self._new_bridge()
        registry = {
            "revision": "reg1", "generatedAt": 1700000000000,
            "providers": [
                {"providerId": "p1", "models": [{"modelId": "m1"}]},
                {"providerId": "p2", "apiKey": {"source": "inline", "value": "sk-reg-LEAK"},
                 "models": [{"modelId": "m2"}]},
            ],
        }
        captured = io.StringIO()
        with contextlib.redirect_stderr(captured):
            resp = self._call(bridge, "workspace/updateProviderRegistry",
                              {"workspacePath": "/p", "registry": registry})
        self._assert_ok(resp)
        self.assertEqual(fake.calls[0]["params"]["registry"], registry, "registry 须原样透传")
        log_out = captured.getvalue()
        self.assertNotIn("sk-reg-LEAK", log_out, "registry log 不得泄露 apiKey")

    def test_m9_update_registry_missing(self):
        """M9f: 缺 registry → -32602"""
        bridge, _ = self._new_bridge()
        resp = self._call(bridge, "workspace/updateProviderRegistry",
                          {"workspacePath": "/p"})
        self._assert_error_code(resp, -32602)

    def test_m9_upsert_provider_error_no_apikey(self):
        """M9g: upsertModelProvider 后端 error 回显脱敏 (Codex P1: error 路径不能泄露 apiKey)"""
        bridge, fake = self._new_bridge()
        # 后端错误消息里夹带了入参的 inline apiKey 明文
        fake.next_response = {"error": {"message":
            "validation failed: apiKey invalid sk-LEAK-IN-ERR-9876 in provider custom"}}
        resp = self._call(bridge, "workspace/upsertModelProvider",
                          {"workspacePath": "/p",
                           "provider": {"providerId": "custom",
                                        "apiKey": {"source": "inline",
                                                   "value": "sk-LEAK-IN-ERR-9876"},
                                        "models": [{"modelId": "m1"}]}})
        self._assert_error_code(resp, -32603)
        err_msg = resp["error"]["message"]
        self.assertNotIn("sk-LEAK-IN-ERR-9876", err_msg,
                         "error 回显不得泄露 apiKey 明文")
        self.assertIn("sk-***", err_msg, "sk- 前缀应被遮蔽为 sk-***")

    def test_m9_update_registry_error_no_apikey(self):
        """M9h: updateProviderRegistry 后端 error 回显脱敏 (registry 含多个 provider apiKey)"""
        bridge, fake = self._new_bridge()
        fake.next_response = {"error": {"message":
            'provider[1] invalid: {"value":"sk-REG-ERR-5555"} rejected'}}
        resp = self._call(bridge, "workspace/updateProviderRegistry",
                          {"workspacePath": "/p",
                           "registry": {"providers": [
                               {"providerId": "p1", "models": [{"modelId": "m1"}]},
                               {"providerId": "p2",
                                "apiKey": {"source": "inline", "value": "sk-REG-ERR-5555"},
                                "models": [{"modelId": "m2"}]}]}})
        self._assert_error_code(resp, -32603)
        err_msg = resp["error"]["message"]
        self.assertNotIn("sk-REG-ERR-5555", err_msg,
                         "registry error 回显不得泄露 apiKey 明文")

    def test_m9_remove_provider_error_not_redacted(self):
        """M9i: removeModelProvider error 不需脱敏 (入参无 apiKey, 保留原文利于排查)"""
        bridge, fake = self._new_bridge()
        fake.next_response = {"error": {"message": "provider old not found"}}
        resp = self._call(bridge, "workspace/removeModelProvider",
                          {"workspacePath": "/p", "providerId": "old"})
        self._assert_error_code(resp, -32603)
        # remove 入参不含 apiKey, 后端错误原样回显 (便于排查)
        self.assertIn("not found", resp["error"]["message"])

    # ---------- M10: 旧 handler 回归 (补盲区) ----------
    def test_m10_fork_routes(self):
        """M10: session/fork 仍正确路由到 session/fork"""
        bridge, fake = self._new_bridge()
        resp = self._call(bridge, "session/fork", {"sessionId": "sess_x"})
        self._assert_ok(resp)
        self.assertEqual(fake.calls[0]["method"], "session/fork")

    def test_m10_rewind_routes(self):
        """M10a: session/rewind 路由"""
        bridge, fake = self._new_bridge()
        self._call(bridge, "session/rewind", {"sessionId": "sess_x"})
        self.assertEqual(fake.calls[0]["method"], "session/rewind")

    def test_m10_goal_show_routes(self):
        """M10b: session/goal show 路由 (不触发 turn 等待)"""
        bridge, fake = self._new_bridge()
        self._call(bridge, "session/goal", {"sessionId": "sess_x", "action": "show"})
        self.assertEqual(fake.calls[0]["method"], "session/goal")
        self.assertEqual(fake.calls[0]["params"]["action"], "show")

    def test_m10_compact_routes(self):
        """M10c: session/compact 路由"""
        bridge, fake = self._new_bridge()
        self._call(bridge, "session/compact", {"sessionId": "sess_x"})
        self.assertEqual(fake.calls[0]["method"], "session/compact")

    def test_m10_steer_routes(self):
        """M10d: session/steer 路由 + content 透传"""
        bridge, fake = self._new_bridge()
        self._call(bridge, "session/steer", {"sessionId": "sess_x", "content": "hi"})
        self.assertEqual(fake.calls[0]["method"], "session/steer")
        self.assertEqual(fake.calls[0]["params"]["content"], "hi")

    def test_m10_unknown_method_32601(self):
        """M10e: 未知方法 → -32601 (dispatch 兜底未受新方法影响)"""
        bridge, _ = self._new_bridge()
        resp = self._call(bridge, "session/nonexistent", {"sessionId": "sess_x"})
        self._assert_error_code(resp, -32601)

    def test_m10_dispatch_registry_complete(self):
        """M10f: 所有 14 个新方法名都已在 dispatch 注册 (无遗漏)"""
        bridge, _ = self._new_bridge()
        new_methods = [
            "session/setThoughtLevel", "session/updateRuntimeModelConfig",
            "session/cancelBackgroundTask", "session/rewindCascade",
            "session/setModel", "session/setMode",
            "workspace/readState", "workspace/generateText",
            "workspace/setDefaultModel", "workspace/setDefaultMode",
            "workspace/setDefaultThoughtLevel",
            "workspace/upsertModelProvider", "workspace/removeModelProvider",
            "workspace/updateProviderRegistry",
            # App 3.3.0+ prompt/* (client 可调; result 是 server 推送, 不在此列)
            "prompt/enhance", "prompt/enhance/start", "prompt/enhance/cancel",
        ]
        for m in new_methods:
            resp = self._call(bridge, m, {"sessionId": "sess_x", "workspacePath": "/p",
                                          "thoughtLevel": "x", "modelId": "m",
                                          "model": {"modelId": "m"}, "mode": "yolo",
                                          "taskId": "t", "provider": {"models": [{"modelId": "m"}]},
                                          "providerId": "p", "registry": {"providers": []},
                                          "prompt": "x", "modelRef": {"modelId": "m"},
                                          "requestId": "r1"})
            # 关键: 不能是 -32601 (未注册)。各方法要么成功, 要么因缺参报 -32602,
            # 但绝不应该是 "Method not supported"
            if "error" in resp:
                self.assertNotEqual(resp["error"]["code"], -32601,
                                    f"{m} 未注册到 dispatch (返回 -32601)")

    # ---------- PE: prompt/enhance 同步 + cancel (App 3.3.0+) ----------
    def test_pe_sync_passthrough(self):
        """PE1: prompt/enhance 透传 workspace + prompt + 可选 sessionId/context"""
        bridge, fake = self._new_bridge()
        fake.next_response = {"result": {"enhanced": "更好的提示词"}}
        resp = self._call(bridge, "prompt/enhance", {
            "workspacePath": "/p", "prompt": "写个函数",
            "sessionId": "sess_x", "context": [{"role": "user", "content": "hi"}],
        })
        self._assert_ok(resp)
        self.assertEqual(fake.calls[0]["method"], "prompt/enhance")
        p = fake.calls[0]["params"]
        self.assertEqual(p["prompt"], "写个函数")
        self.assertEqual(p["sessionId"], "sess_x")
        self.assertEqual(p["context"], [{"role": "user", "content": "hi"}])
        self.assertEqual(p["workspace"], {"workspacePath": "/p", "workspaceKey": "/p"})
        self.assertEqual(resp["result"], {"enhanced": "更好的提示词"})

    def test_pe_sync_missing_prompt(self):
        """PE2: 缺 prompt → -32602"""
        bridge, _ = self._new_bridge()
        resp = self._call(bridge, "prompt/enhance", {"workspacePath": "/p"})
        self._assert_error_code(resp, -32602)

    def test_pe_sync_backend_error(self):
        """PE3: 后端错误 → -32603"""
        bridge, fake = self._new_bridge()
        fake.next_response = {"error": {"message": "model unavailable"}}
        resp = self._call(bridge, "prompt/enhance",
                          {"workspacePath": "/p", "prompt": "x"})
        self._assert_error_code(resp, -32603)

    def test_pe_sync_timeout_90(self):
        """PE4: prompt/enhance timeout=90 (模型调用, 比 generateText 的 60 宽裕)"""
        bridge, fake = self._new_bridge()
        self._call(bridge, "prompt/enhance",
                   {"workspacePath": "/p", "prompt": "x"})
        self.assertEqual(fake.calls[0]["timeout"], 90)

    def test_pe_sync_context_omitted(self):
        """PE5: 不传 sessionId/context → zc_params 不含这俩键"""
        bridge, fake = self._new_bridge()
        self._call(bridge, "prompt/enhance",
                   {"workspacePath": "/p", "prompt": "x"})
        p = fake.calls[0]["params"]
        self.assertNotIn("sessionId", p)
        self.assertNotIn("context", p)

    def test_pe_cancel_passthrough(self):
        """PE6: prompt/enhance/cancel 透传 requestId, 返回 cancelled"""
        bridge, fake = self._new_bridge()
        fake.next_response = {"result": {"requestId": "r1", "cancelled": True}}
        resp = self._call(bridge, "prompt/enhance/cancel", {"requestId": "r1"})
        self._assert_ok(resp)
        self.assertEqual(fake.calls[0]["method"], "prompt/enhance/cancel")
        self.assertEqual(fake.calls[0]["params"], {"requestId": "r1"})
        self.assertEqual(resp["result"], {"requestId": "r1", "cancelled": True})

    def test_pe_cancel_missing_requestid(self):
        """PE7: cancel 缺 requestId → -32602"""
        bridge, _ = self._new_bridge()
        resp = self._call(bridge, "prompt/enhance/cancel", {})
        self._assert_error_code(resp, -32602)


if __name__ == "__main__":
    unittest.main(verbosity=2)
