"""
test_app_server_methods.py — app-server 0.16.1 新协议方法单测

0.15.0 → 0.16.1 协议三层全变 (规格书 docs/upgrade-0.16.1-spec.md, 全部实测):
信封去 jsonrpc 键 (§1)、方法重命名/删除 (§2)、新增 server→client 反向调用
session/requestRuntimePreferences (§3)、subscribe 必传 deliveryKind + 新事件
模型 (§4)。用 FakeBackend 替换真实 zcode 子进程, 验证 ACPBridge.handle_acp()
的路由、参数转换与降级行为; prompt 全流程用线程限时兜底 (接口未对齐时快速
失败, 不让套件卡在 120s 等待上)。

  V   信封: ACP 侧保留 jsonrpc (ACP 协议不变), zcode 侧新信封无 jsonrpc 键
      (V2 params 净身 / V3 真实 request() 帧构造断言)
  C   session/new → session/create (cwd → workspace{workspacePath,workspaceKey})
  EV  事件/轮询模式选择 · 事件分支: subscribe 必传 deliveryKind → 事件模式
      (不轮询); 轮询降级分支见 test_polling_failure.py PF3
  PM  协议模式 (legacy/v16): _detect_protocol 三分支探测 + 实锤标志
      protocol_confirmed (P1-3: 超时兜底未确认, subscribe 失败可推翻回退
      轮询 — PM7) / v16 subscribe 门禁 / state.updated 投影合并 / prompt
      入口投影重置 (P1-1 跨 turn 陈旧防御 — PM8/PM8a) / reader 反向调用路由
  S   session/prompt → session/send ({sessionId,content} → {accepted,stateRevision})
  X   session/cancel → session/stop (X1 空闲直发一次; X2 活动 turn 只置标志,
      stop 由 turn 循环补发 — zcode review P1-2 单点化)
  R   server→client 反向调用 session/requestRuntimePreferences 应答
      (两个 scope: create=runtime-materialization, send=user-execution)
  M   存活方法回归 (规格书 §2 存活清单: setThoughtLevel/setModel/setMode/
      cancelBackgroundTask/fork/goal/compact + workspace/*)
  D   已删方法降级: steer/rewind/rewindCascade → -32601「该版本不支持」文案
      (prompt/enhance* 的降级见 test_prompt_enhance.py)
  Z   未知方法仍 -32601 (bridge 自身文案, 与降级文案区分)

事实注记 (reviewer-1 0.16.1 真机抓帧, 对规格书 §4 信封描述的勘误): 事件判别
字段 type 在 params 顶层 (payload 内不含), 通知带 deliveryKind; server 反向
调用的分发入口为 ZCodeBackend._handle_server_request (实现已冻结)。

运行: python3 tests/test_app_server_methods.py
依赖: 仅 Python 标准库 + 本项目的 acp-bridge 模块
"""

import contextlib
import io
import json
import os
import queue
import threading
import time
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


# 规格书 §3: server→client 反向调用 session/requestRuntimePreferences 的应答
# schema (bundle zod 实证)。create (runtime-materialization) 与 send
# (user-execution) 两个 scope 用同一份应答。
RUNTIME_PREFS_RESULT = {
    "nativeSearchEnhancementsEnabled": False,
    "memoryEnabled": False,
    "askUserQuestionAutoResolutionEnabled": False,
}

# 规格书 §4: session/subscribe 必填 deliveryKind 的取值枚举 (实测 desktop-continuous 可用)
DELIVERY_KINDS = ("desktop-continuous", "web-remote-replayable")

# 规格书 §2: 0.16.1 已从 bundle 删除的方法 (steer/rewind 系; prompt/enhance* 见另一文件)
DELETED_SESSION_METHODS = ("session/steer", "session/rewind", "session/rewindCascade")

# 规格书 §2 存活且实测仍在 bundle 的扩展方法 (回归锚; updateRuntimeModelConfig
# 经 commander 0.16.1 实测确认存活 — schema 新要求 runtimeModel.revision 必填,
# 已回归纳入, 透传用例见 M2)
SURVIVING_EXTENSION_METHODS = [
    "session/setThoughtLevel", "session/cancelBackgroundTask",
    "session/updateRuntimeModelConfig",
    "session/setModel", "session/setMode",
    "workspace/readState", "workspace/generateText",
    "workspace/setDefaultModel", "workspace/setDefaultMode",
    "workspace/setDefaultThoughtLevel",
    "workspace/upsertModelProvider", "workspace/removeModelProvider",
    "workspace/updateProviderRegistry",
]


def _contains_key(obj, key):
    """递归检查嵌套 dict/list 里是否出现某个键 (信封净身用)。"""
    if isinstance(obj, dict):
        if key in obj:
            return True
        return any(_contains_key(v, key) for v in obj.values())
    if isinstance(obj, list):
        return any(_contains_key(v, key) for v in obj)
    return False


def _session_event(etype, payload, seq=1):
    """构造一条 0.16.1 session/event 通知的 params (reviewer-1 实测线缆形态)。

    判别字段 type 在 params 顶层 (payload 内不含), 带 deliveryKind 字段
    (对规格书 §4 信封描述的勘误)。
    """
    return {"type": etype, "deliveryKind": "desktop-continuous",
            "seq": seq, "eventId": f"evt_{seq}", "timestamp": 1754460000000,
            "traceId": "trace_test", "sessionId": "sess_test", "payload": payload}


def _run_with_guard(fn, timeout=15):
    """在线程中执行 fn 并限时取结果; 超时/异常都显式失败。

    prompt 全流程用: bridge 与实现的接口未对齐时宁可快速失败, 不让套件
    卡在 turn 等待 (旧实现最长 120s) 上。
    """
    box = {}

    def _run():
        try:
            box["result"] = fn()
        except Exception as exc:
            box["exc"] = exc

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise AssertionError(f"调用 {timeout}s 内未完成 (实现未对齐或流程卡住)")
    if "exc" in box:
        raise box["exc"]
    return box.get("result")


class FakeBackend:
    """替代真实 ZCodeBackend: 按方法脚本化响应, 记录所有调用与 send 帧。

    - script: {method: {"response": resp 或 [resp 序列], "events": [params, ...]}}
      response 缺省 {"result": {"ok": True}}; events 是 request() 第二返回值
      (turn 等待期间从 reader 收到的事件, 与旧版 seam 一致)。
    - calls: 每次调用的 {"id", "method", "params", "timeout"} 列表
    - sent: 经 send() 发出的帧 (bridge 应答 server 反向调用预期走这条路)
    """

    def __init__(self, script=None):
        self.script = script or {}
        self.calls = []
        self.sent = []

    def request(self, msg_id, method, params=None, timeout=30):
        self.calls.append({
            "id": msg_id, "method": method,
            "params": params or {}, "timeout": timeout,
        })
        entry = self.script.get(method, {})
        resp = entry.get("response", {"result": {"ok": True}})
        if isinstance(resp, list):  # 序列: 同一方法第 N 次调用取第 N 个 (末尾驻留)
            seen = sum(1 for c in self.calls[:-1] if c["method"] == method)
            resp = resp[min(seen, len(resp) - 1)]
        return resp, entry.get("events", [])

    def send(self, msg):
        self.sent.append(msg)


class TestAppServerMethods(unittest.TestCase):
    """app-server 0.16.1 新协议的路由/参数转换/透传/降级 单测"""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_bridge_module()
        cls.Bridge = cls.mod.ACPBridge

    def _new_bridge(self, script=None):
        """构造一个注入了 FakeBackend 的 bridge (跳过真实子进程)。"""
        b = self.Bridge()
        fake = FakeBackend(script)
        b.backend = fake
        return b, fake

    def _call(self, bridge, method, params=None, msg_id=1):
        """封装一次 handle_acp 调用 (ACP 侧信封, 保留 jsonrpc 键)。"""
        req = {"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params or {}}
        return bridge.handle_acp(req)

    def _assert_ok(self, resp, msg=None):
        """断言响应是成功 (含 result, 无 error)。"""
        self.assertNotIn("error", resp, f"期望成功, 实际报错: {resp.get('error')} ({msg})")
        self.assertIn("result", resp)

    def _assert_error_code(self, resp, code, msg=None):
        self.assertEqual(resp.get("error", {}).get("code"), code,
                         f"期望错误码 {code}, 实际: {resp} ({msg})")

    # ---------- V: 信封 ----------
    def test_v0_acp_side_keeps_jsonrpc(self):
        """V0: ACP 侧信封不变 (ACP 协议自身是标准 JSON-RPC), 响应仍带 jsonrpc"""
        bridge, fake = self._new_bridge()
        resp = self._call(bridge, "initialize")
        self._assert_ok(resp)
        self.assertEqual(resp.get("jsonrpc"), "2.0",
                         "ACP 侧响应应保留 jsonrpc 键 (变的是 zcode 侧)")
        self.assertEqual(fake.calls, [], "initialize 由 bridge 本地应答, 不调 backend")

    def test_v1_new_envelope_fixtures(self):
        """V1: 规格书 §1 实证的新信封形态 (请求/通知/响应/反向请求均无 jsonrpc 键)"""
        frames = {
            "request": {"id": 1, "method": "session/list", "params": {}},
            "notification": {"method": "session/event",
                             "params": {"seq": 1, "payload": {}}},
            "response_ok": {"id": 1, "result": {}},
            "response_err": {"id": 1,
                             "error": {"code": -32601, "message": "Method not found"}},
            "server_request": {
                "id": "server-1", "method": "session/requestRuntimePreferences",
                "params": {"sessionId": "sess_1", "scope": "runtime-materialization"}},
        }
        for name, frame in frames.items():
            self.assertNotIn("jsonrpc", frame, f"{name} 不得带 jsonrpc 键")
        # 形态校验: 请求 {id,method,params}; 通知 {method,params} 无 id;
        # 响应 {id,result|error} 无 method
        self.assertEqual(set(frames["request"]), {"id", "method", "params"})
        self.assertNotIn("id", frames["notification"])
        self.assertNotIn("method", frames["response_ok"])
        self.assertNotIn("method", frames["response_err"])
        self.assertEqual(set(frames["server_request"]), {"id", "method", "params"})

    def test_v2_outbound_params_no_jsonrpc(self):
        """V2: bridge 发往 zcode 的参数里不得混入 jsonrpc 键 (净身检查)"""
        bridge, fake = self._new_bridge()
        self._call(bridge, "session/new", {"cwd": "/p"})
        self._call(bridge, "workspace/readState", {"workspacePath": "/p"})
        for c in fake.calls:
            self.assertFalse(_contains_key(c["params"], "jsonrpc"),
                             f"{c['method']} 的 params 混入 jsonrpc 键: {c['params']}")

    def test_v3_outbound_request_frame_no_jsonrpc(self):
        """V3: 真实 ZCodeBackend.request() 构造的帧顶层恰为 {id,method,params}

        V2 只查 params 净身 (FakeBackend 整体替换 request(), 帧构造触达不到);
        这里用裸 backend + send 捕获 (R 系列手法), 断言真实 request() 发出去
        的帧符合 0.16.1 新信封 (规格书 §1: 顶层无 jsonrpc 键)。
        """
        backend = self._bare_backend()

        def _send_reply(msg):
            backend.sent_frames.append(msg)
            # 模拟 reader: 回显响应, 让 request() 正常返回
            with backend._resp_lock:
                q = backend._response_queues.get(msg.get("id"))
            if q is not None:
                q.put({"id": msg["id"], "result": {"ok": True}})

        backend.send = _send_reply
        resp, _ = backend.request(424242, "session/list", {"workspacePath": "/p"})
        self.assertEqual(resp.get("result"), {"ok": True}, "回显响应应原样返回")
        self.assertEqual(len(backend.sent_frames), 1)
        frame = backend.sent_frames[0]
        self.assertEqual(set(frame), {"id", "method", "params"},
                         f"出站请求帧顶层应恰为 id/method/params: {frame}")
        self.assertNotIn("jsonrpc", frame, "0.16.1 新信封无 jsonrpc 键 (规格书 §1)")
        self.assertEqual(frame["id"], 424242)
        self.assertEqual(frame["method"], "session/list")
        self.assertEqual(frame["params"], {"workspacePath": "/p"})

    # ---------- C: session/new → session/create ----------
    def test_c1_create_maps_cwd_to_workspace(self):
        """C1: ACP session/new {cwd} → session/create {workspace:{path,key}}, key=path"""
        bridge, fake = self._new_bridge({
            "session/create": {"response": {"result": {
                "sessionId": "sess_new",
                "protocol": {"name": "ZCode Protocol", "version": 1},
            }}},
        })
        resp = self._call(bridge, "session/new", {"cwd": "/p/work"})
        self._assert_ok(resp)
        create = [c for c in fake.calls if c["method"] == "session/create"]
        self.assertEqual(len(create), 1, "应调一次 session/create (0.15.0 的 session/new 已删)")
        ws = create[0]["params"].get("workspace")
        self.assertEqual(ws, {"workspacePath": "/p/work", "workspaceKey": "/p/work"},
                         "本地场景 workspaceKey = workspacePath (规格书 §2)")
        self.assertNotIn("cwd", create[0]["params"], "0.15.0 的 cwd 参数不得再发")
        self.assertTrue(resp["result"].get("sessionId"), "ACP 响应应带回 sessionId")

    def test_c2_create_protocol_block_tolerated(self):
        """C2: create 响应含 protocol:{name,version} 版本探测块 (§2) → 正常完成"""
        bridge, _ = self._new_bridge({
            "session/create": {"response": {"result": {
                "sessionId": "sess_p",
                "protocol": {"name": "ZCode Protocol", "version": 1},
            }}},
        })
        resp = self._call(bridge, "session/new", {"cwd": "/p"})
        self._assert_ok(resp, "protocol 版本探测块不应让 create 失败")

    def test_c3_create_mode_passthrough(self):
        """C3: 显式 mode 透传到 session/create (实证记录: create 参数含 mode)"""
        bridge, fake = self._new_bridge()
        self._call(bridge, "session/new", {"cwd": "/p", "mode": "plan"})
        create = [c for c in fake.calls if c["method"] == "session/create"]
        self.assertEqual(len(create), 1)
        self.assertEqual(create[0]["params"].get("mode"), "plan")

    # ---------- EV: 事件/轮询模式选择 · 事件分支 ----------
    def test_ev1_event_branch_subscribe_deliverykind(self):
        """EV1: subscribe 带 deliveryKind 成功 → 事件模式 (不触发轮询); 轮询分支见 PF3"""
        bridge, fake = self._new_bridge({
            "session/subscribe": {"response": {"result": {"subscribed": True}}},
            "session/send": {
                "response": {"result": {"accepted": True, "stateRevision": 3}},
                "events": [
                    _session_event("turn.started", {"turnNumber": 1, "input": "hi",
                                                    "messageId": "msg_1"}, seq=1),
                    _session_event("model.streaming", {"kind": "text_delta",
                                                       "delta": "你好",
                                                       "assistantMessageId": "am_1"}, seq=2),
                    _session_event("turn.completed", {
                        "response": "你好",
                        "usage": {"inputTokens": 10, "outputTokens": 5,
                                  "totalTokens": 15, "contextWindow": 200000}}, seq=3),
                ],
            },
        })
        bridge.session_map["acp_ev1"] = "sess_ev1"
        resp = _run_with_guard(lambda: self._call(
            bridge, "session/prompt", {"sessionId": "acp_ev1", "prompt": "hi"}))
        self._assert_ok(resp)

        sub = [c for c in fake.calls if c["method"] == "session/subscribe"]
        self.assertEqual(len(sub), 1, "事件分支应先调 session/subscribe")
        self.assertEqual(sub[0]["params"].get("deliveryKind"), "desktop-continuous",
                         f"subscribe 必传 deliveryKind (枚举 {DELIVERY_KINDS}, 规格书 §4)")
        self.assertEqual(sub[0]["params"].get("sessionId"), "sess_ev1")

        send = [c for c in fake.calls if c["method"] == "session/send"]
        self.assertEqual(len(send), 1, "session/prompt 已重命名为 session/send (§2)")
        self.assertEqual(send[0]["params"].get("sessionId"), "sess_ev1")
        self.assertIn("content", send[0]["params"], "send 参数为 {sessionId, content}")

        self.assertFalse(any(c["method"] == "session/read" for c in fake.calls),
                         "事件分支不应出现 session/read 轮询调用")

    # ---------- S: session/send 边界 ----------
    def test_s1_send_not_accepted_is_error(self):
        """S1: send 返回 accepted:false (§2 应答含 accepted/stateRevision) → 判失败

        派生边界: 规格书只给出应答结构, accepted=false 的语义按"发送未被接受"
        处理, 不得静默空等 turn (实现已冻结此行为)。
        """
        bridge, fake = self._new_bridge({
            "session/subscribe": {"response": {"result": {"subscribed": True}}},
            "session/send": {"response": {"result": {"accepted": False,
                                                     "stateRevision": 0}}},
        })
        bridge.session_map["acp_s1"] = "sess_s1"
        resp = _run_with_guard(lambda: self._call(
            bridge, "session/prompt",
            {"sessionId": "acp_s1", "prompt": "hi", "content": "hi"}))
        self.assertIn("error", resp, "accepted:false 应返回错误而非静默等待")
        self.assertTrue(any(c["method"] == "session/send" for c in fake.calls))

    # ---------- X: session/cancel → session/stop ----------
    def test_x1_cancel_routes_to_stop(self):
        """X1: ACP session/cancel → session/stop (§2 rename; 另有 session/close)

        空闲 (无活动 turn) 路径: 直发一次 session/stop (stop 幂等, 后端报错忽略)。
        """
        bridge, fake = self._new_bridge()
        bridge.session_map["acp_x1"] = "sess_x1"
        self._call(bridge, "session/cancel", {"sessionId": "acp_x1"})
        stop = [c for c in fake.calls if c["method"] == "session/stop"]
        self.assertEqual(len(stop), 1, "session/cancel 已重命名为 session/stop (§2)")
        self.assertEqual(stop[0]["params"].get("sessionId"), "sess_x1")

    def test_x2_cancel_with_active_turn_marks_only(self):
        """X2: 有活动 turn 时 cancel 只置标志, 不直发 session/stop (P1-2 单点化)

        zcode review P1-2: session/stop 由 turn 循环经 _cancel_backend_turn
        补发 (fire-and-forget); 本方法若同时直发, 同一次 cancel 会双发 stop
        (stop 虽幂等, 双发无谓)。空闲直发路径见 X1。
        """
        bridge, fake = self._new_bridge()
        bridge.session_map["acp_x2"] = "sess_x2"
        bridge.pending_turns[10000001] = {"zcode_sid": "sess_x2", "cancelled": False}
        resp = self._call(bridge, "session/cancel", {"sessionId": "acp_x2"})
        self.assertIsNone(resp, "session/cancel 是 notification, 无响应")
        self.assertTrue(bridge.pending_turns[10000001]["cancelled"],
                        "活动 turn 应被标记取消 (turn 循环据此补发 stop)")
        stop = [c for c in fake.calls if c["method"] == "session/stop"]
        self.assertEqual(stop, [],
                         "有活动 turn 时本方法不直发 session/stop (防同次 cancel 双发)")
        stop_sent = [f for f in fake.sent
                     if isinstance(f, dict) and f.get("method") == "session/stop"]
        self.assertEqual(stop_sent, [], "也不经 send() 直发 (补发是 turn 循环的职责)")

    # ---------- R: server→client 反向调用应答 ----------
    def _bare_backend(self):
        """绕过 __init__ 构造裸 ZCodeBackend (不起子进程), 注入分发所需最小状态。

        属性集沿用旧测试 (PE12 时代) 已揭示的内部 seam; send 被实例级替换为
        帧记录器, 捕获对 server 反向调用的应答。协议探测/reader 循环/状态投影
        测试也复用本桩 (探测锁/投影锁/enhance 队列一并备齐)。
        """
        mod = self.mod
        backend = mod.ZCodeBackend.__new__(mod.ZCodeBackend)
        backend._response_queues = {}
        backend._resp_lock = threading.Lock()
        backend._notification_queue = queue.Queue()
        backend._event_listeners = {}
        backend._listeners_lock = threading.Lock()
        backend._reader_dead = False
        backend._reader_stop = False
        # 协议探测 / reader 退出清理 / 状态投影 所需的其余内部状态
        backend._enhance_result_queues = {}
        backend._enhance_lock = threading.Lock()
        backend.protocol_mode = None
        # 探测判定实锤标志 (P1-3): None=未探测; True=实锤; False=仅超时兜底
        backend.protocol_confirmed = None
        backend._probe_lock = threading.Lock()
        backend._protocol_error_queue = queue.Queue()
        backend._state_projections = {}
        backend._state_lock = threading.Lock()
        backend.sent_frames = []
        backend.send = backend.sent_frames.append
        return backend

    def _feed_server_request(self, backend, msg):
        """把一条 server→client 请求喂进 backend 的反向调用分发入口。

        实现已冻结: reader 对 id+method 双有帧调 _handle_server_request
        (reader 路由本身见 PM6), 这里直接调分发入口。
        """
        return backend._handle_server_request(msg)

    def _assert_runtime_prefs_answer(self, backend, server_id):
        """断言 backend.sent_frames 里有且仅有一帧合规的 runtime-preferences 应答。"""
        answers = [f for f in backend.sent_frames
                   if isinstance(f, dict) and f.get("id") == server_id]
        self.assertEqual(len(answers), 1,
                         f"应应答一次 {server_id}, 实际 sent={backend.sent_frames}")
        ans = answers[0]
        self.assertNotIn("jsonrpc", ans, "应答帧不得带 jsonrpc 键 (0.16.1 新信封)")
        self.assertNotIn("method", ans, "应答是 response 不是 request")
        self.assertEqual(ans.get("result"), RUNTIME_PREFS_RESULT,
                         "应答 schema 必须逐项匹配规格书 §3 (bundle zod 实证)")

    def test_r1_answer_runtime_materialization(self):
        """R1: create 触发的反向调用 (scope=runtime-materialization) → 按 schema 应答"""
        backend = self._bare_backend()
        self._feed_server_request(backend, {
            "id": "server-1", "method": "session/requestRuntimePreferences",
            "params": {"sessionId": "sess_1", "scope": "runtime-materialization"}})
        self._assert_runtime_prefs_answer(backend, "server-1")

    def test_r2_answer_user_execution(self):
        """R2: send 触发的反向调用 (scope=user-execution) → 同一应答, id 回显"""
        backend = self._bare_backend()
        self._feed_server_request(backend, {
            "id": "server-2", "method": "session/requestRuntimePreferences",
            "params": {"sessionId": "sess_2", "scope": "user-execution"}})
        self._assert_runtime_prefs_answer(backend, "server-2")

    def test_r3_unknown_server_request_32601(self):
        """R3: 不认识的 server 反向调用 → 显式 -32601 错误应答 (不沉默, 不用 result 误答)"""
        backend = self._bare_backend()
        self._feed_server_request(backend, {
            "id": "server-9", "method": "workspace/someFutureCall", "params": {}})
        answers = [f for f in backend.sent_frames
                   if isinstance(f, dict) and f.get("id") == "server-9"]
        self.assertEqual(len(answers), 1,
                         f"未知 server 请求也应显式应答一次 (防 server 空等挂起), "
                         f"实际 sent={backend.sent_frames}")
        ans = answers[0]
        self.assertNotIn("result", ans, "未知 server 请求不得用 result 应答 (防误答)")
        self.assertEqual(ans.get("error", {}).get("code"), -32601,
                         "未知 server 请求应收 -32601 错误应答")
        self.assertNotIn("jsonrpc", ans, "应答帧不得带 jsonrpc 键 (0.16.1 新信封)")

    # ---------- PM: 协议模式 (legacy/v16) 探测 / 门禁 / 投影 / reader 路由 ----------
    def _detect_backend(self, responder):
        """构造探测专用的裸 ZCodeBackend: send 捕获探测帧并由 responder 模拟应答。

        responder(frame, backend) 把模拟的 server 应答写进对应响应队列
        (真实实现里这是 reader 线程的活); None 则不应答 (模拟超时)。
        """
        backend = self._bare_backend()

        def _send(msg):
            backend.sent_frames.append(msg)
            if responder is not None:
                responder(msg, backend)

        backend.send = _send
        return backend

    @staticmethod
    def _feed_response(backend, frame_id, msg):
        """模拟 reader: 按 id 把帧写进已注册的响应队列。"""
        with backend._resp_lock:
            q = backend._response_queues.get(frame_id)
        if q is not None:
            q.put(msg)

    def test_pm1_detect_v16_envelope_reject(self):
        """PM1: 探测帧被 0.16+ zod 拒收 (-32600, id 固定占位 invalid-message) → v16"""
        backend = self._detect_backend(
            lambda frame, b: self._feed_response(b, "invalid-message", {
                "id": "invalid-message",
                "error": {"code": -32600, "message": "invalid request",
                          "data": {"issues": [{"code": "invalid_union",
                                               "path": ["jsonrpc"]}]}}}))
        backend._detect_protocol()
        self.assertEqual(backend.protocol_mode, "v16")
        self.assertIs(backend.protocol_confirmed, True,
                      "信封拒绝帧是实锤判定 → confirmed=True (P1-3)")
        probe = backend.sent_frames[0]
        self.assertEqual(probe["id"], "bridge-probe-1")
        self.assertEqual(probe["method"], "workspace/readState")
        self.assertIn("jsonrpc", probe, "探测故意用含 jsonrpc 的旧信封试探")

    def test_pm2_detect_legacy_probe_echo(self):
        """PM2: ≤0.15 理解旧信封 → 回显 probe id 的正常响应 (-32601/-32602) → legacy"""
        for code in (-32601, -32602):
            with self.subTest(code=code):
                backend = self._detect_backend(
                    lambda frame, b, c=code: self._feed_response(b, frame["id"], {
                        "id": frame["id"],
                        "error": {"code": c, "message": "Invalid params"}}))
                backend._detect_protocol()
                self.assertEqual(backend.protocol_mode, "legacy")
                self.assertIs(backend.protocol_confirmed, True,
                              "旧协议回显是实锤判定 → confirmed=True (P1-3)")

    def test_pm3_detect_timeout_unconfirmed_v16(self):
        """PM3: 探测超时 (无响应) → 暂按 v16 但未确认 (confirmed=False, P1-3)

        zcode review P1-3: 超时不是实锤 (可能只是 server 启动慢于 8s), 不得
        享受 v16 门禁 — subscribe 失败时应可推翻回退 legacy 轮询 (端到端
        回退见 PM7; 实锤 v16 门禁见 PM4)。
        """
        backend = self._detect_backend(None)  # send 只捕获, 不应答
        # time.time 快进, 让 8s 探测窗口立即耗尽 (防套件真等 8s)
        real_time = time.time
        clock = [real_time()]
        try:
            time.time = lambda: (clock.__setitem__(0, clock[0] + 10), clock[0])[1]
            backend._detect_protocol()
        finally:
            time.time = real_time
        self.assertEqual(backend.protocol_mode, "v16", "超时仍暂按当前发行版 v16 处理")
        self.assertIs(backend.protocol_confirmed, False,
                      "超时兜底无实锤 → confirmed=False (subscribe 失败可推翻)")
        self.assertEqual(len(backend.sent_frames), 1, "超时前仍发出了一次探测帧")
        # 判定已缓存: 二次探测直接复用, 不再发帧
        backend._detect_protocol()
        self.assertEqual(len(backend.sent_frames), 1, "探测结果全程只探一次 (缓存复用)")

    def test_pm4_v16_subscribe_failure_no_polling(self):
        """PM4: v16 模式 subscribe 失败 → 直接报错, 不降级轮询 (0.16+ 必须事件订阅)"""
        bridge, fake = self._new_bridge({
            "session/subscribe": {"response": {"error": {
                "code": -32602, "message": "deliveryKind required"}}},
        })
        # 真机启动时 _detect_protocol 缓存的判定; FakeBackend 无该属性按 legacy
        fake.protocol_mode = "v16"
        bridge.session_map["acp_pm4"] = "sess_pm4"
        resp = _run_with_guard(lambda: self._call(
            bridge, "session/prompt", {"sessionId": "acp_pm4", "prompt": "hi"}))
        self.assertIn("error", resp, "v16 模式 subscribe 失败必须报错 (不允许轮询降级)")
        self.assertIn("subscribe", resp["error"]["message"])
        self.assertFalse(any(c["method"] == "session/read" for c in fake.calls),
                         "v16 门禁: 不得出现 session/read 轮询调用")
        self.assertFalse(any(c["method"] == "session/send" for c in fake.calls),
                         "subscribe 失败即返回, send 不应发出")

    def test_pm7_unconfirmed_v16_subscribe_failure_demotes_to_polling(self):
        """PM7: 超时兜底判的 v16 (confirmed=False) → subscribe 失败推翻判定回退轮询

        zcode review P1-3 端到端: 探测超时不是实锤 (可能 ≤0.15 server 启动慢),
        此时 subscribe 失败本身就是旧协议实锤 → 推翻 v16 兜底, 回退 legacy 走
        原有轮询降级, 而非硬判 v16 报错把整个 server 判死 (实锤 v16 门禁见 PM4)。
        """
        # 先真跑一次超时探测, 拿到真实的超时兜底判定 (mode=v16, confirmed=False)
        probe_backend = self._detect_backend(None)
        real_time = time.time
        clock = [real_time()]
        try:
            time.time = lambda: (clock.__setitem__(0, clock[0] + 10), clock[0])[1]
            probe_backend._detect_protocol()
        finally:
            time.time = real_time
        self.assertEqual(probe_backend.protocol_mode, "v16")
        self.assertIs(probe_backend.protocol_confirmed, False)

        # 同一判定喂给 bridge: subscribe 失败 → 不得硬报错, 应回退轮询降级
        bridge, fake = self._new_bridge({
            "session/subscribe": {"response": {"error": {
                "code": -32602, "message": "deliveryKind required"}}},
            "session/send": {"response": {"result": {"accepted": True,
                                                     "stateRevision": 1}}},
            "session/read": {"response": [
                {"result": {"projection": {"status": "running", "totalTokenCount": 10,
                                            "contextWindow": 1000000}}},
                {"result": {"projection": {"status": "idle", "totalTokenCount": 20,
                                            "contextWindow": 1000000}}},
            ]},
            "session/messages": {"response": {"result": {"messages": [
                # 无 info.id: 与 PF3 同款 — 带 id 会被 prompt 前置 baseline
                # mark_seen 标为已见, diff 不再产出, 轮询收尾误判「无输出」
                {"info": {"role": "assistant"},
                 "parts": [{"type": "text", "text": "降级轮询的答案"}]},
            ], "todos": []}}},
        })
        fake.protocol_mode = probe_backend.protocol_mode            # "v16" (超时兜底)
        fake.protocol_confirmed = probe_backend.protocol_confirmed  # False (未确认)
        bridge.session_map["acp_pm7"] = "sess_pm7"
        resp = _run_with_guard(lambda: self._call(
            bridge, "session/prompt", {"sessionId": "acp_pm7", "prompt": "hi"}))
        self._assert_ok(resp, "超时兜底 v16 的 subscribe 失败应回退轮询, 不得硬报错")
        self.assertEqual(resp["result"]["stopReason"], "end_turn")
        called = [c["method"] for c in fake.calls]
        self.assertIn("session/send", called, "回退后轮询分支照常发 session/send")
        self.assertIn("session/read", called, "回退后应走 session/read 轮询降级")
        # 探测判定被推翻: 回退 legacy 且转为已确认 (subscribe 失败即旧协议实锤)
        self.assertEqual(fake.protocol_mode, "legacy",
                         "subscribe 失败应推翻超时兜底的 v16 判定")
        self.assertIs(fake.protocol_confirmed, True,
                      "推翻后回退 legacy 转为实锤确认")


    def test_pm5_state_updated_projection_merge(self):
        """PM5: state.updated 的 patch 合并进状态投影 (供事件流停滞检查)"""
        backend = self._bare_backend()
        # 实测形态: {patch:{status,...}, reason, revision, scope, sessionId}
        backend._merge_state_patch({"sessionId": "sess_pm5", "reason": "turn.started",
                                    "revision": 4, "scope": "session",
                                    "patch": {"status": "running"}})
        backend._merge_state_patch({"sessionId": "sess_pm5", "revision": 7,
                                    "patch": {"status": "idle", "mode": "build"}})
        proj = backend.get_projection("sess_pm5")
        self.assertEqual(proj, {"status": "idle", "mode": "build"},
                         "patch 按 update 语义合并 (后到覆盖同名字段, 其余保留)")
        # 返回的是副本: 改副本不污染内部投影
        proj["status"] = "hacked"
        self.assertEqual(backend.get_projection("sess_pm5")["status"], "idle")
        # 未知 session / 畸形 patch 不炸不污染
        self.assertIsNone(backend.get_projection("sess_unknown"))
        backend._merge_state_patch({"sessionId": "sess_pm5", "patch": "not-a-dict"})
        backend._merge_state_patch({"patch": {"status": "running"}})  # 无 sessionId
        self.assertEqual(backend.get_projection("sess_pm5")["status"], "idle")

    def test_pm8a_reset_projection_clears_stale(self):
        """PM8a: reset_projection 清空会话投影 (prompt 入口重置语义; zcode review P1-1)

        核实背景: 0.16.1 turn 期间 state.updated 实测只有 {status:running}
        (prompt_started) 与 {mode,model,...} (prompt_completed) 两种 patch,
        从不发 status:idle (Wave 2 reviewer-1) — 跨 turn 残留最多卡在 running,
        陈旧 idle 短路停滞检查的竞态在 0.16.1 不可达。reset 是零成本防御:
        未来版本若补发 idle patch, 也读不到上一 turn 的旧值。PM5 合并语义不变。
        """
        backend = self._bare_backend()
        backend._merge_state_patch({"sessionId": "sess_pm8a", "revision": 7,
                                    "patch": {"status": "idle", "mode": "build"}})
        self.assertEqual(backend.get_projection("sess_pm8a"),
                         {"status": "idle", "mode": "build"})
        backend.reset_projection("sess_pm8a")
        self.assertIsNone(backend.get_projection("sess_pm8a"),
                          "重置后陈旧投影不得再被停滞检查读到")
        # 未知 sid 重置不炸; 重置后本 turn 的新 patch 照常累积 (投影继续服务停滞检查)
        backend.reset_projection("sess_unknown")
        backend._merge_state_patch({"sessionId": "sess_pm8a", "revision": 8,
                                    "patch": {"status": "running"}})
        self.assertEqual(backend.get_projection("sess_pm8a"), {"status": "running"},
                         "重置后本 turn 的新 patch 照常合并 (prompt_started 实测形态)")

    def test_pm8_prompt_entry_resets_stale_projection(self):
        """PM8: session/prompt 入口重置该会话状态投影 (P1-1 桥侧契约)

        同一 sessionId 先形成含 status:idle 的陈旧投影; 新 turn 开始时入口
        必须清掉它 (先于 subscribe/send), 否则 _run_event_turn 停滞检查会把
        陈旧 idle 当成本 turn 完成的证据提前收尾。FakeBackend 默认无投影 seam
        (getattr 兜底不调用), 本用例用带投影 seam 的桩钉住入口重置行为。
        """
        mod = self.mod

        class _ProjFakeBackend(FakeBackend):
            """带状态投影 seam 的 FakeBackend (对齐 ZCodeBackend.reset_projection 语义)"""

            def __init__(self, script=None, projections=None):
                super().__init__(script)
                self._projections = dict(projections or {})
                self.sequence = []  # [("reset", sid) | ("request", method)] 时序记录

            def get_projection(self, sid):
                proj = self._projections.get(sid)
                return dict(proj) if proj else None

            def reset_projection(self, sid):
                self.sequence.append(("reset", sid))
                self._projections.pop(sid, None)

            def request(self, msg_id, method, params=None, timeout=30):
                self.sequence.append(("request", method))
                return super().request(msg_id, method, params, timeout)

        fake = _ProjFakeBackend(
            script={
                "session/subscribe": {"response": {"result": {"subscribed": True}}},
                "session/send": {
                    "response": {"result": {"accepted": True, "stateRevision": 2}},
                    "events": [
                        _session_event("turn.started", {"turnNumber": 1, "input": "hi",
                                                        "messageId": "msg_1"}, seq=1),
                        _session_event("model.streaming", {"kind": "text_delta",
                                                           "delta": "你好",
                                                           "assistantMessageId": "am_1"}, seq=2),
                        _session_event("turn.completed", {
                            "response": "你好",
                            "usage": {"inputTokens": 10, "outputTokens": 5,
                                      "totalTokens": 15, "contextWindow": 200000}}, seq=3),
                    ],
                },
            },
            # 上一 turn 残留的陈旧投影 (含 status:idle)
            projections={"sess_pm8": {"status": "idle", "mode": "build"}},
        )
        bridge = mod.ACPBridge()
        bridge.backend = fake
        bridge.session_map["acp_pm8"] = "sess_pm8"
        resp = _run_with_guard(lambda: self._call(
            bridge, "session/prompt", {"sessionId": "acp_pm8", "prompt": "hi"}))
        self._assert_ok(resp)
        reset_entries = [e for e in fake.sequence if e[0] == "reset"]
        self.assertEqual(reset_entries, [("reset", "sess_pm8")],
                         "prompt 入口应恰好重置一次该会话投影")
        req_methods = [m for kind, m in fake.sequence if kind == "request"]
        reset_pos = fake.sequence.index(("reset", "sess_pm8"))
        self.assertLess(reset_pos, fake.sequence.index(("request", "session/subscribe")),
                        "投影重置必须先于 subscribe (入口即清, 不等停滞检查)")
        self.assertLess(reset_pos, fake.sequence.index(("request", "session/send")),
                        "投影重置必须先于 session/send")
        self.assertIsNone(fake.get_projection("sess_pm8"),
                          "陈旧 idle 不得活到新 turn (停滞检查只认本 turn 新 patch)")
        self.assertIn("session/send", req_methods)

    def test_pm6_reader_routes_id_and_method_to_server_request(self):
        """PM6: reader 对 id+method 双有帧路由到 _handle_server_request 并应答"""
        backend = self._bare_backend()
        frames = [
            {"id": "server-1", "method": "session/requestRuntimePreferences",
             "params": {"sessionId": "sess_pm6", "scope": "user-execution"}},
            {"id": "server-2", "method": "workspace/someFutureCall", "params": {}},
        ]
        backend.proc = types.SimpleNamespace(stdout=io.StringIO(
            "".join(json.dumps(f) + "\n" for f in frames)))
        backend._reader_loop()
        by_id = {f.get("id"): f for f in backend.sent_frames if isinstance(f, dict)}
        self.assertEqual(by_id["server-1"].get("result"), RUNTIME_PREFS_RESULT,
                         "已知反向调用应路由到 runtime-preferences 应答")
        self.assertEqual(by_id["server-2"].get("error", {}).get("code"), -32601,
                         "未知反向调用应路由到 -32601 应答 (而非进响应队列/丢弃)")
        self.assertTrue(backend._reader_dead, "stdout EOF 后 reader 正常退出并标记 dead")

    # ---------- M: 存活方法回归 (规格书 §2 存活清单) ----------
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
        """M1b: backend 返回 error (非 -32601) → -32603"""
        bridge, fake = self._new_bridge({
            "session/setThoughtLevel": {"response": {
                "error": {"message": "model has no reasoning levels"}}},
        })
        resp = self._call(bridge, "session/setThoughtLevel",
                          {"sessionId": "sess_x", "thoughtLevel": "high"})
        self._assert_error_code(resp, -32603)

    def test_m2_update_runtime_model_config_with_revision(self):
        """M2: updateRuntimeModelConfig 透传含 revision 的 runtimeModel (保真)

        commander 0.16.1 实测: 该方法仍存活, 但 schema 新要求
        runtimeModel.revision (string) 必填; bridge 为透传不校验,
        缺字段由后端报错。F7-r 残留修正: 旧注释「不在存活清单、状态未证实」
        作废, 本用例钉住带 revision 的完整透传。
        """
        bridge, fake = self._new_bridge()
        runtime_model = {
            "revision": "rev-2026-08-06-1",
            "generatedAt": 1754460000000,
            "model": {"providerId": "zai", "modelId": "GLM-5.2"},
            "provider": {"providerId": "zai", "kind": "anthropic",
                         "baseURL": "https://api.z.ai/api/anthropic"},
            "thoughtLevel": "high",
        }
        resp = self._call(bridge, "session/updateRuntimeModelConfig", {
            "sessionId": "sess_x", "runtimeModel": runtime_model,
            "applyModelSelection": True,
        })
        self._assert_ok(resp)
        self.assertEqual(fake.calls[0]["method"], "session/updateRuntimeModelConfig")
        p = fake.calls[0]["params"]
        self.assertEqual(p["sessionId"], "sess_x")
        self.assertEqual(p["runtimeModel"], runtime_model,
                         "runtimeModel 整体原样透传 (含 revision, 不篡改不裁剪)")
        self.assertEqual(p["runtimeModel"].get("revision"), "rev-2026-08-06-1",
                         "0.16.1 schema 新必填的 revision 必须随透传到达后端")
        self.assertEqual(p["applyModelSelection"], True,
                         "可选 applyModelSelection 亦透传")

    def test_m2_update_runtime_model_config_missing(self):
        """M2a: 缺 runtimeModel → -32602 (桥本地校验; revision 缺省由后端报错)"""
        bridge, _ = self._new_bridge()
        resp = self._call(bridge, "session/updateRuntimeModelConfig",
                          {"sessionId": "sess_x"})
        self._assert_error_code(resp, -32602)

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

    # ---------- M6: _resolve_workspace 选择器 (与 session/create 同一构造) ----------
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
        """M6b: params["cwd"] → 构造选择器 (与 session/create 一致的 key)"""
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
        """M6d: workspace dict 但缺 workspacePath → 回退到路径构造"""
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
        captured = io.StringIO()
        provider = {
            "providerId": "custom",
            "apiKey": {"source": "inline", "value": "sk-LEAK-MARKER-xyz"},
            "models": [{"modelId": "m1"}],
        }
        with contextlib.redirect_stderr(captured):
            bridge, _ = self._new_bridge()
            bridge.handle_acp({"jsonrpc": "2.0", "id": 1,
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
        """M9g: upsertModelProvider 后端 error 回显脱敏 (error 路径不能泄露 apiKey)"""
        bridge, fake = self._new_bridge({
            "workspace/upsertModelProvider": {"response": {"error": {"message":
                "validation failed: apiKey invalid sk-LEAK-IN-ERR-9876 in provider custom"}}},
        })
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
        bridge, fake = self._new_bridge({
            "workspace/updateProviderRegistry": {"response": {"error": {"message":
                'provider[1] invalid: {"value":"sk-REG-ERR-5555"} rejected'}}},
        })
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
        bridge, fake = self._new_bridge({
            "workspace/removeModelProvider": {"response": {
                "error": {"message": "provider old not found"}}},
        })
        resp = self._call(bridge, "workspace/removeModelProvider",
                          {"workspacePath": "/p", "providerId": "old"})
        self._assert_error_code(resp, -32603)
        self.assertIn("not found", resp["error"]["message"])

    # ---------- M10: 存活的旧 handler 回归 ----------
    def test_m10_fork_routes(self):
        """M10: session/fork 仍正确路由 (§2 存活清单)"""
        bridge, fake = self._new_bridge()
        resp = self._call(bridge, "session/fork", {"sessionId": "sess_x"})
        self._assert_ok(resp)
        self.assertEqual(fake.calls[0]["method"], "session/fork")

    def test_m10_goal_show_routes(self):
        """M10a: session/goal show 路由 (不触发 turn 等待)"""
        bridge, fake = self._new_bridge()
        self._call(bridge, "session/goal", {"sessionId": "sess_x", "action": "show"})
        self.assertEqual(fake.calls[0]["method"], "session/goal")
        self.assertEqual(fake.calls[0]["params"]["action"], "show")

    def test_m10_compact_routes(self):
        """M10b: session/compact 路由"""
        bridge, fake = self._new_bridge()
        self._call(bridge, "session/compact", {"sessionId": "sess_x"})
        self.assertEqual(fake.calls[0]["method"], "session/compact")

    def test_m10_dispatch_registry_complete(self):
        """M10c: §2 存活的 12 个扩展方法都已在 dispatch 注册 (无遗漏)"""
        bridge, _ = self._new_bridge()
        for m in SURVIVING_EXTENSION_METHODS:
            resp = self._call(bridge, m, {"sessionId": "sess_x", "workspacePath": "/p",
                                          "thoughtLevel": "x", "modelId": "m",
                                          "model": {"modelId": "m"}, "mode": "yolo",
                                          "taskId": "t",
                                          "provider": {"models": [{"modelId": "m"}]},
                                          "providerId": "p",
                                          "registry": {"providers": []},
                                          "prompt": "x", "modelRef": {"modelId": "m"}})
            # 关键: 不能是 -32601 (未注册)。各方法要么成功, 要么因缺参报 -32602,
            # 但绝不应该是 "Method not supported"
            if "error" in resp:
                self.assertNotEqual(resp["error"]["code"], -32601,
                                    f"{m} 未注册到 dispatch (返回 -32601)")

    # ---------- D: 已删方法 -32601 降级 (规格书 §7) ----------
    def test_d1_deleted_methods_friendly_32601(self):
        """D1: steer/rewind/rewindCascade 已删 → -32601 + 「不支持此能力」文案

        规格书 §7: 调已删除方法收到 -32601 Method not found, bridge 应映射为
        「该 ZCode 版本不支持此能力」。短路与透传映射两条实现路径都接受,
        只断言最终 ACP 响应。
        """
        for m in DELETED_SESSION_METHODS:
            with self.subTest(method=m):
                bridge, _ = self._new_bridge({m: {"response": {
                    "error": {"code": -32601, "message": "Method not found"}}}})
                resp = self._call(bridge, m,
                                  {"sessionId": "sess_x", "content": "hi"})
                self._assert_error_code(resp, -32601)
                self.assertIn("不支持", resp["error"]["message"],
                              f"{m} 的 -32601 应映射为版本不支持文案 (§7)")

    def test_d2_deleted_method_not_32603(self):
        """D2: 已删方法不得再透传为 -32603 (README 旧描述作废, §7)"""
        for m in DELETED_SESSION_METHODS:
            with self.subTest(method=m):
                bridge, _ = self._new_bridge({m: {"response": {
                    "error": {"code": -32601, "message": "Method not found"}}}})
                resp = self._call(bridge, m, {"sessionId": "sess_x"})
                code = resp.get("error", {}).get("code")
                self.assertNotEqual(code, -32603,
                                    f"{m} 的 -32601 不得被吞成 -32603")

    def test_d3_deleted_method_missing_params_still_32601(self):
        """D3: 已删方法缺参调用也报能力缺失 (-32601), 不退化成 -32602 参数错误"""
        bridge, _ = self._new_bridge({"session/steer": {"response": {
            "error": {"code": -32601, "message": "Method not found"}}}})
        resp = self._call(bridge, "session/steer", {})
        self._assert_error_code(resp, -32601)

    # ---------- Z: 未知方法 ----------
    def test_z1_unknown_method_32601(self):
        """Z1: 真正未知的方法 → -32601 (bridge 自身文案, 与 D 系列降级文案区分)"""
        bridge, _ = self._new_bridge()
        resp = self._call(bridge, "session/nonexistent", {"sessionId": "sess_x"})
        self._assert_error_code(resp, -32601)


if __name__ == "__main__":
    unittest.main(verbosity=2)
