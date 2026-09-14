"""本机 UI 服务（server.py）测试 —— 真起 HTTP 服务，真发请求。

这套测试的价值在于：它是**唯一能证明「前端那一跳」没问题又不需要图形环境**
的地方。窗口（pywebview / Tk）在 CI 里测不了，但页面要的每一个接口都能在这儿
用假 client 走一遍完整 HTTP 往返。

故意不起真 agentd：UiServer 接受注入的 Bridge，于是可以塞假 client，
把「HTTP 层」和「agent 层」分开验。
"""

from __future__ import annotations

import json

import pytest

from forgeagent.gui.bridge import Bridge
from forgeagent.gui.server import UiServer
from test_gui_bridge import _FakeClient


@pytest.fixture()
def server():
    srv = UiServer(bridge=Bridge(client=_FakeClient())).start()
    try:
        yield srv
    finally:
        srv.stop()


def test_url_carries_token_in_fragment(server):
    assert server.url.startswith("http://127.0.0.1:")
    assert f"#token={server.token}" in server.url


def test_index_html_is_served_with_token_inlined(server):
    """token 必须写进页面里 —— 实测靠 URL fragment 传不稳。"""
    raw = server.client()
    import urllib.request

    req = urllib.request.Request(f"http://127.0.0.1:{server.port}/")
    with urllib.request.urlopen(req, timeout=5) as r:
        body = r.read().decode("utf-8")
    assert "<!DOCTYPE html>" in body
    assert server.token in body
    assert "__FORGEAGENT_TOKEN__" not in body  # 占位符必须被替换掉
    assert raw is not None


def test_api_requires_token(server):
    import urllib.error
    import urllib.request

    req = urllib.request.Request(f"http://127.0.0.1:{server.port}/api/hello")
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=5)
    assert exc.value.code == 401


def test_hello_ok_with_token(server):
    assert server.client().get("/api/hello")["ok"] is True


def test_send_and_stream_over_http(server):
    """完整一轮：POST /api/send -> GET /api/events 取回流式增量。"""
    api = server.client()
    assert api.post("/api/send", {"text": "问个问题"})["ok"] is True

    got, done = "", None
    for _ in range(20):
        for ev in api.get("/api/events?timeout=2").get("events", []):
            if ev.get("type") == "delta":
                got += ev["text"]
            elif ev.get("type") == "done":
                done = ev
        if done:
            break

    assert done is not None
    assert got == "你好呀"          # _FakeClient 的三个增量
    assert done["stop"] == "end_turn"


def test_events_long_poll_returns_empty_on_timeout(server):
    assert server.client().get("/api/events?timeout=0.1")["events"] == []


def test_state_round_trip(server):
    """界面自报 -> 外部读，走的是真 HTTP。"""
    api = server.client()
    api.post("/api/state", {"bridge": "ready", "count": {"u": 1, "a": 1}})
    state = api.get("/api/state")["state"]
    assert state["bridge"] == "ready"
    assert state["count"] == {"u": 1, "a": 1}


def test_command_is_picked_up_by_events(server):
    api = server.client()
    api.post("/api/command", {"action": "send", "text": "来自外部"})
    evs = api.get("/api/events?timeout=1")["events"]
    assert evs[0] == {"type": "command", "cmd": {"action": "send", "text": "来自外部"}}


def test_stderr_endpoint(server):
    assert server.client().get("/api/stderr?n=10")["lines"] == [
        "[agentd] 自动选用 Ollama 模型: qwen3.5:9b-text"
    ]


def test_path_traversal_is_refused(server):
    import urllib.error
    import urllib.request

    req = urllib.request.Request(f"http://127.0.0.1:{server.port}/assets/../bridge.py")
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=5)
    assert exc.value.code in (403, 404)


def test_unknown_path_is_404(server):
    import urllib.error
    import urllib.request

    req = urllib.request.Request(
        f"http://127.0.0.1:{server.port}/nope",
        headers={"X-ForgeAgent-Token": server.token},
    )
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=5)
    assert exc.value.code == 404


# ---- 会话侧栏 / 续聊 ----

class _FakeSessions:
    """agentd 会话库的只读视图替身，塞给 UiServer，完全不碰磁盘。"""

    def __init__(self, *, sessions=None, history=None, exists=True):
        self._sessions = sessions or []
        self._history = history or []
        self._exists = exists

    def list_meta(self):
        return self._sessions

    def get_history(self, session_id: str):
        return self._history if self._exists else None

    def exists(self, session_id: str):
        return self._exists


def _server_with(sessions) -> UiServer:
    return UiServer(bridge=Bridge(client=_FakeClient()), sessions=sessions).start()


def test_sessions_endpoint_lists_and_requires_token():
    import urllib.error
    import urllib.request

    fake = _FakeSessions(sessions=[{"id": "s1", "created_at": 1.0, "msg_count": 3}])
    srv = _server_with(fake)
    try:
        # 没 token 必须 401
        req = urllib.request.Request(f"http://127.0.0.1:{srv.port}/api/sessions")
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=5)
        assert exc.value.code == 401
        # 有 token 返回列表
        assert srv.client().get("/api/sessions")["sessions"] == [
            {"id": "s1", "created_at": 1.0, "msg_count": 3}
        ]
    finally:
        srv.stop()


def test_session_history_endpoint_returns_history_or_404():
    fake = _FakeSessions(
        history=[{"role": "user", "name": None, "content": "hi"}]
    )
    srv = _server_with(fake)
    try:
        hist = srv.client().get("/api/session/s1")["history"]
        assert hist == [{"role": "user", "name": None, "content": "hi"}]

        # 库里没有这个 id -> 404
        missing = _FakeSessions(exists=False)
        srv2 = _server_with(missing)
        try:
            import urllib.error
            import urllib.request

            req = urllib.request.Request(
                f"http://127.0.0.1:{srv2.port}/api/session/nope",
                headers={"X-ForgeAgent-Token": srv2.token},
            )
            with pytest.raises(urllib.error.HTTPError) as exc:
                urllib.request.urlopen(req, timeout=5)
            assert exc.value.code == 404
        finally:
            srv2.stop()
    finally:
        srv.stop()


def test_session_resume_switches_client_session():
    """续聊：校验 id 存在后，bridge 把 client 的 sessionId 换成旧的。"""
    fake = _FakeSessions(exists=True)
    srv = _server_with(fake)
    try:
        res = srv.client().post("/api/session/resume", {"session_id": "old_session"})
        assert res["ok"] is True
        assert res["session"] == "old_session"
        # 关键：client 的 sessionId 真的被换掉了，后续 send 才会接着旧上下文
        assert srv.bridge._client.session_id == "old_session"
    finally:
        srv.stop()


def test_session_resume_rejects_unknown_id():
    import urllib.error
    import urllib.request

    fake = _FakeSessions(exists=False)
    srv = _server_with(fake)
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{srv.port}/api/session/resume",
            data=json.dumps({"session_id": "ghost"}).encode("utf-8"),
            method="POST",
            headers={"X-ForgeAgent-Token": srv.token, "Content-Type": "application/json"},
        )
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=5)
        assert exc.value.code == 404
    finally:
        srv.stop()


def test_session_new_creates_fresh_session():
    """新对话：bridge.new_session 走 ACP session/new，记下新 id。"""
    fake = _FakeSessions()
    srv = _server_with(fake)
    try:
        res = srv.client().post("/api/session/new", {})
        assert res["ok"] is True
        assert res["session"] == "sess_new789012"
        assert srv.bridge._client.session_id == "sess_new789012"
    finally:
        srv.stop()

