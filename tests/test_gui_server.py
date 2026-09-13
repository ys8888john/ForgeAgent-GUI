"""本机 UI 服务（server.py）测试 —— 真起 HTTP 服务，真发请求。

这套测试的价值在于：它是**唯一能证明「前端那一跳」没问题又不需要图形环境**
的地方。窗口（pywebview / Tk）在 CI 里测不了，但页面要的每一个接口都能在这儿
用假 client 走一遍完整 HTTP 往返。

故意不起真 agentd：UiServer 接受注入的 Bridge，于是可以塞假 client，
把「HTTP 层」和「agent 层」分开验。
"""

from __future__ import annotations

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
