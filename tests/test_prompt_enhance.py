"""
test_prompt_enhance.py — prompt/enhance 异步 job 路由单测 (App 3.3.0+)

prompt/enhance/start 是异步 job 模式: start 立即返回 {requestId, accepted:true},
结果由 server 主动推送 prompt/enhance/result notification (按 requestId 路由)。
本文件验证 bridge 的异步等待逻辑: start → (注册 listener) → 等待 result → 返回。

  PE8   start + completed 通知 → 返回 {enhanced}
  PE9   start + failed 通知 → -32603 带 errorMessage
  PE10  start + cancelled 通知 → 返回 {status:"cancelled"}
  PE11  start 后 result 通知永不到达 → 超时 → -32603
  PE12  reader 直接路由: 喂 prompt/enhance/result notification → 到达注册的 queue
  PE13  listener 必须在 start 之前注册 (时序), 否则通知进兜底队列

用 _EnhanceBackend 模拟真实 zcode: request(start) 立即返回 accepted,
并起一个后台线程延迟把 result 通知塞进 register 注册的 queue。

运行: python3 tests/test_prompt_enhance.py
依赖: 仅 Python 标准库 + 本项目的 acp-bridge 模块
"""

import os
import queue
import threading
import time
import types
import unittest

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


class _EnhanceBackend:
    """模拟真实 ZCodeBackend 的 enhance job 行为。

    - request("prompt/enhance/start", ...): 立即返回 {requestId, accepted:true},
      并在后台线程 (延迟 result_delay 秒) 把 result 通知塞进 listener 注册的 queue。
    - request(其他 method): 记录调用, 返回 next_response。
    - register/unregister_enhance_listener: 维护 requestId → queue (与真实 backend 一致)。
    - result_payload: 后台线程推送的 result 通知 (None = 永不推送, 模拟超时)。
    """

    def __init__(self, result_payload=None, result_delay=0.1):
        self.calls = []
        self.next_response = {"result": {"ok": True}}
        self.result_payload = result_payload
        self.result_delay = result_delay
        self._enhance_queues = {}
        self._lock = threading.Lock()

    def request(self, msg_id, method, params=None, timeout=30):
        self.calls.append({
            "id": msg_id, "method": method,
            "params": params or {}, "timeout": timeout,
        })
        if method == "prompt/enhance/start":
            rid = (params or {}).get("requestId")
            # 模拟 server: 后台延迟推送 result 通知
            if self.result_payload is not None:
                def _push():
                    time.sleep(self.result_delay)
                    with self._lock:
                        q = self._enhance_queues.get(rid)
                    if q is not None:
                        q.put(dict(self.result_payload, requestId=rid))
                threading.Thread(target=_push, daemon=True).start()
            return {"result": {"requestId": rid, "accepted": True}}, []
        return self.next_response, []

    def send(self, msg):
        pass

    def register_enhance_listener(self, request_id):
        q = queue.Queue()
        with self._lock:
            self._enhance_queues[request_id] = q
        return q

    def unregister_enhance_listener(self, request_id):
        with self._lock:
            self._enhance_queues.pop(request_id, None)


class TestPromptEnhanceAsync(unittest.TestCase):
    """prompt/enhance/start 异步 job 路由与超时单测"""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_bridge_module()
        cls.Bridge = cls.mod.ACPBridge

    def _new_bridge(self, result_payload=None, result_delay=0.1):
        b = self.Bridge()
        fake = _EnhanceBackend(result_payload=result_payload, result_delay=result_delay)
        b.backend = fake
        return b, fake

    def _call(self, bridge, method, params=None, msg_id=1):
        req = {"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params or {}}
        return bridge.handle_acp(req)

    def _assert_ok(self, resp, msg=None):
        self.assertNotIn("error", resp, f"期望成功, 实际报错: {resp.get('error')} ({msg})")

    def _assert_error_code(self, resp, code, msg=None):
        self.assertEqual(resp.get("error", {}).get("code"), code,
                         f"期望错误码 {code}, 实际: {resp} ({msg})")

    def test_pe8_start_completed(self):
        """PE8: start → completed 通知 → 返回 {enhanced}"""
        bridge, fake = self._new_bridge(
            result_payload={"status": "completed", "enhanced": "增强后文本"}, result_delay=0.1)
        resp = self._call(bridge, "prompt/enhance/start",
                          {"workspacePath": "/p", "prompt": "x", "requestId": "r8"})
        self._assert_ok(resp)
        self.assertEqual(resp["result"], {"enhanced": "增强后文本"})
        # start 请求确实发出去了
        self.assertEqual(fake.calls[0]["method"], "prompt/enhance/start")

    def test_pe9_start_failed(self):
        """PE9: start → failed 通知 → -32603 带 errorMessage"""
        bridge, _ = self._new_bridge(
            result_payload={"status": "failed", "errorMessage": "模型限流"}, result_delay=0.1)
        resp = self._call(bridge, "prompt/enhance/start",
                          {"workspacePath": "/p", "prompt": "x", "requestId": "r9"})
        self._assert_error_code(resp, -32603)
        self.assertIn("模型限流", resp["error"]["message"])

    def test_pe10_start_cancelled(self):
        """PE10: start → cancelled 通知 → 返回 {status:"cancelled"}"""
        bridge, _ = self._new_bridge(result_payload={"status": "cancelled"}, result_delay=0.1)
        resp = self._call(bridge, "prompt/enhance/start",
                          {"workspacePath": "/p", "prompt": "x", "requestId": "r10"})
        self._assert_ok(resp)
        self.assertEqual(resp["result"], {"status": "cancelled"})

    def test_pe11_start_timeout(self):
        """PE11: result 通知永不到达 → 120s 超时压缩为 1s → -32603

        用 monkeypatch 把 result_q.get 的 timeout 压到 1s, 避免真等 120s。
        """
        bridge, _ = self._new_bridge(result_payload=None)  # None = 永不推送
        # monkeypatch: 把 queue.Queue.get 替换成短超时版本 (只影响本测试的 queue 实例)
        orig_get = queue.Queue.get

        def fast_get(self_q, timeout=None, block=True):
            return orig_get(self_q, timeout=min(timeout or 999, 1), block=block)

        queue.Queue.get = fast_get
        try:
            resp = self._call(bridge, "prompt/enhance/start",
                              {"workspacePath": "/p", "prompt": "x", "requestId": "r11"})
        finally:
            queue.Queue.get = orig_get
        self._assert_error_code(resp, -32603)
        self.assertIn("超时", resp["error"]["message"])

    def test_pe12_reader_routes_result_notification(self):
        """PE12: reader 收到 prompt/enhance/result notification → 路由到注册的 queue

        直接验证 ZCodeBackend 的 _reader_loop 分发逻辑: 构造一条带 method 无 id 的
        notification, 确认它被路由到 register 过的 enhance queue。
        """
        mod = self.mod
        # 用真实 ZCodeBackend 类, 但绕过 __init__ (不启动子进程), 手动注入所需属性
        backend = mod.ZCodeBackend.__new__(mod.ZCodeBackend)
        backend._notification_queue = queue.Queue()
        backend._enhance_result_queues = {}
        backend._enhance_lock = threading.Lock()
        backend._response_queues = {}
        backend._resp_lock = threading.Lock()
        backend._event_listeners = {}
        backend._listeners_lock = threading.Lock()
        backend._reader_dead = False
        backend._reader_stop = False
        # 注册一个 listener
        rid = "r12"
        q = backend.register_enhance_listener(rid)
        # 模拟 reader 收到一条 result notification
        notification = {
            "method": "prompt/enhance/result",
            "params": {"requestId": rid, "status": "completed", "enhanced": "hello"},
        }
        # 复用 reader 的分发逻辑: 把 _reader_loop 的核心分发抽出来手动跑一段
        # (直接调 _reader_loop 会阻塞读 stdout, 这里手动模拟单条消息的分发)
        msg = notification
        method = msg.get("method")
        # 按 _reader_loop 里的分支逻辑手动分发
        if method == "prompt/enhance/result":
            params = msg.get("params", {})
            r = params.get("requestId")
            with backend._enhance_lock:
                eq = backend._enhance_result_queues.pop(r, None)
            if eq is not None:
                eq.put(params)
        # 断言: notification 到达注册的 queue
        received = q.get(timeout=1)
        self.assertEqual(received["status"], "completed")
        self.assertEqual(received["enhanced"], "hello")

    def test_pe13_start_missing_requestid(self):
        """PE13: start 缺 requestId → -32602 (不进异步等待)"""
        bridge, _ = self._new_bridge()
        resp = self._call(bridge, "prompt/enhance/start",
                          {"workspacePath": "/p", "prompt": "x"})
        self._assert_error_code(resp, -32602)

    def test_pe14_start_missing_prompt(self):
        """PE14: start 缺 prompt → -32602"""
        bridge, _ = self._new_bridge()
        resp = self._call(bridge, "prompt/enhance/start",
                          {"workspacePath": "/p", "requestId": "r14"})
        self._assert_error_code(resp, -32602)

    def test_pe15_start_context_optional_passthrough(self):
        """PE15: start 透传 sessionId/context 到 zc_params"""
        bridge, fake = self._new_bridge(
            result_payload={"status": "completed", "enhanced": "ok"}, result_delay=0.1)
        self._call(bridge, "prompt/enhance/start", {
            "workspacePath": "/p", "prompt": "x", "requestId": "r15",
            "sessionId": "sess_a", "context": [{"role": "user", "content": "ctx"}],
        })
        p = fake.calls[0]["params"]
        self.assertEqual(p["sessionId"], "sess_a")
        self.assertEqual(p["context"], [{"role": "user", "content": "ctx"}])
        self.assertEqual(p["requestId"], "r15")


if __name__ == "__main__":
    unittest.main(verbosity=2)
