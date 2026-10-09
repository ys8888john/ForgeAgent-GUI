"""本机 UI 服务（server.py）测试 —— 真起 HTTP 服务，真发请求。

这套测试的价值在于：它是**唯一能证明「前端那一跳」没问题又不需要图形环境**
的地方。窗口（pywebview / Tk）在 CI 里测不了，但页面要的每一个接口都能在这儿
用假 client 走一遍完整 HTTP 往返。

故意不起真 agentd：UiServer 接受注入的 Bridge，于是可以塞假 client，
把「HTTP 层」和「agent 层」分开验。
"""

from __future__ import annotations

import json
from pathlib import Path

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


# ---- 产物卡片：/api/file_info 与 /api/open_path ----
#
# 前端从 write_file / edit / make_xlsx 的完成输出里提取路径，来这两个接口
# 换绝对路径/大小、点开文件。白名单 = 当前空间目录 + tool_results 外置目录。


def test_file_info_resolves_relative_to_space(server, tmp_path):
    """相对路径按当前空间目录折算，返回绝对路径 + 大小。"""
    server.current_space_dir = str(tmp_path)
    f = tmp_path / "out" / "航班.xlsx"
    f.parent.mkdir(parents=True)
    f.write_bytes(b"X" * 23900)
    r = server.client().post("/api/file_info", {"path": "out/航班.xlsx"})
    assert r["ok"] is True
    assert r["abs"] == str(f)
    assert r["name"] == "航班.xlsx"
    assert r["size"] == 23900


def test_file_info_accepts_absolute_path_inside_space(server, tmp_path):
    server.current_space_dir = str(tmp_path)
    f = tmp_path / "a.txt"
    f.write_text("hi", encoding="utf-8")
    r = server.client().post("/api/file_info", {"path": str(f)})
    assert r["ok"] is True
    assert r["abs"] == str(f)


def test_file_info_missing_file(server, tmp_path):
    server.current_space_dir = str(tmp_path)
    r = server.client().post("/api/file_info", {"path": "nope.xlsx"})
    assert r["ok"] is False
    assert "不存在" in r["error"]


def test_file_info_outside_allowlist_is_refused(server):
    """空间目录之外的绝对路径直接拒绝 —— 防页面被注入后拿它当文件探测器。"""
    r = server.client().post("/api/file_info", {"path": "C:/Windows/win.ini"})
    assert r["ok"] is False
    assert "允许范围" in r["error"]


def test_open_path_refuses_executable(server, tmp_path):
    """扩展名黑名单：os.startfile 对 .bat 等于执行，必须挡掉。"""
    server.current_space_dir = str(tmp_path)
    (tmp_path / "evil.bat").write_text("echo hi", encoding="utf-8")
    r = server.client().post("/api/open_path", {"path": "evil.bat"})
    assert r["ok"] is False
    assert "可执行" in r["error"]


def test_open_path_opens_with_default_app(server, tmp_path, monkeypatch):
    import forgeagent.gui.server as srv_mod

    opened = []
    monkeypatch.setattr(
        srv_mod, "_open_with_default_app", lambda p: opened.append(p) or {"ok": True}
    )
    server.current_space_dir = str(tmp_path)
    f = tmp_path / "report.md"
    f.write_text("# hi", encoding="utf-8")
    r = server.client().post("/api/open_path", {"path": "report.md"})
    assert r["ok"] is True
    assert opened == [f]


def test_cancel_endpoint_round_trip(server):
    """POST /api/cancel 必须到达协议层 —— 「停止」按钮的完整一跳。"""
    res = server.client().post("/api/cancel", {})
    assert res == {"ok": True}
    fake = server.bridge._client
    assert fake.cancel_calls == 1


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


# ---- MCP 配置 ----

def test_mcp_endpoint_reports_configured_servers():
    servers = [
        {"name": "echo", "command": "python", "args": ["echo.py"]},
        {"name": "remote", "url": "https://example.com/mcp"},
    ]
    srv = UiServer(
        bridge=Bridge(client=_FakeClient()), mcp_servers=servers
    ).start()
    try:
        info = srv.client().get("/api/mcp")
        assert info["ok"] is True
        assert info["count"] == 2
        assert info["servers"] == ["echo", "remote"]
        assert info["path"]  # 配置路径要露出来，方便用户找
    finally:
        srv.stop()


def test_mcp_endpoint_requires_token():
    import urllib.error
    import urllib.request

    srv = UiServer(bridge=Bridge(client=_FakeClient()), mcp_servers=[]).start()
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{srv.port}/api/mcp")
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=5)
        assert exc.value.code == 401
    finally:
        srv.stop()


def test_mcp_servers_reach_the_acp_client():
    """mcp_servers 要一路传到 AcpClient —— 它在 session/new 时带给 agentd。"""
    servers = [{"name": "echo", "command": "python", "args": ["echo.py"]}]
    srv = UiServer(bridge=None, mcp_servers=servers).start()  # 真 Bridge，但不连 agentd
    try:
        assert srv.bridge._client._mcp_servers == servers
    finally:
        srv.stop()


class _FakeSessions:
    """agentd 会话库的只读视图替身，塞给 UiServer，完全不碰磁盘。"""

    def __init__(self, *, sessions=None, history=None, exists=True):
        self._sessions = sessions or []
        self._history = history or []
        self._exists = exists
        self.hidden: list[str] = []
        self.renamed: list[tuple[str, str]] = []

    def hide(self, session_id: str) -> None:
        self.hidden.append(session_id)

    def rename(self, session_id: str, title: str) -> str:
        self.renamed.append((session_id, title))
        return title

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


# ---- 审批：agent -> 页面 -> agent 的往返 ----
#
# 这条路必须走 HTTP：页面的唯一回话通道就是 fetch。所以"能不能把审批请求送到
# 页面"和"页面能不能把答案送回去"这两件事都要在真 HTTP 上验一遍 ——
# 只测 bridge 层证明不了这一跳。

def _perm_server():
    from test_gui_bridge import _PermClient

    fake = _PermClient()
    srv = UiServer(bridge=Bridge(client=fake)).start()
    return srv, fake


def _sample_permission():
    from forgeagent.acp_client import PermissionRequest

    return PermissionRequest(
        request_id=77,
        session_id="s1",
        call_id="call_1",
        title="write_file",
        kind="edit",
        detail="path: notes.txt",
        options=[
            {"optionId": "allow_once", "name": "允许一次", "kind": "allow_once"},
            {"optionId": "reject", "name": "拒绝", "kind": "reject_once"},
        ],
    )


def test_permission_event_reaches_page_over_http():
    srv, fake = _perm_server()
    try:
        fake.on_permission(_sample_permission())
        evs = srv.client().get("/api/events?timeout=1")["events"]
        assert evs[0]["type"] == "permission"
        assert evs[0]["id"] == 77
        assert evs[0]["title"] == "write_file"
    finally:
        srv.stop()


def test_permission_answer_goes_back_over_http():
    """页面点"允许"后这一帧必须真的回到协议层 —— 不回就等于永久挂住。"""
    srv, fake = _perm_server()
    try:
        res = srv.client().post("/api/permission", {"id": 77, "option_id": "allow_once"})
        assert res["ok"] is True
        assert fake.answered == [(77, "allow_once")]
    finally:
        srv.stop()


def test_permission_answer_requires_token():
    """审批接口同样在安全边界内，不能裸奔。"""
    import urllib.error
    import urllib.request

    srv, _ = _perm_server()
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{srv.port}/api/permission",
            data=json.dumps({"id": 1, "option_id": "allow_once"}).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=5)
        assert exc.value.code == 401
    finally:
        srv.stop()


def test_mode_endpoint_round_trip(server):
    """POST /api/mode 必须原样转发 bridge.set_mode（背后是 session/set_mode）。"""
    res = server.client().post("/api/mode", {"mode_id": "single"})
    assert res == {"ok": True, "mode_id": "single"}
    assert server.bridge._client.set_mode_calls == ["single"]


def test_session_hide_round_trip():
    """POST /api/session/hide 落到会话源的 hide（侧栏隐藏，数据库不动）。"""
    fake_sessions = _FakeSessions()
    srv = _server_with(fake_sessions)
    try:
        res = srv.client().post("/api/session/hide", {"session_id": "sess_abc123"})
        assert res == {"ok": True, "session": "sess_abc123"}
        assert fake_sessions.hidden == ["sess_abc123"]
    finally:
        srv.stop()


def test_mcp_raw_round_trip(tmp_path, monkeypatch):
    """MCP 原文读写：GET 拿原文 → POST 合法 mcp.json → 落盘 + bridge 重启生效。"""
    import urllib.error

    cfg = tmp_path / "mcp.json"
    monkeypatch.setenv("FORGEAGENT_MCP_CONFIG", str(cfg))
    srv = _server_with(_FakeSessions())
    try:
        # 初始：文件不存在 → 空文本
        assert srv.client().get("/api/mcp/raw")["text"] == ""

        body = {
            "text": json.dumps(
                {
                    "mcpServers": {
                        "echo": {"command": "python", "args": ["echo.py"], "env": {}}
                    }
                }
            )
        }
        res = srv.client().post("/api/mcp/raw", body)
        assert res["ok"] is True and res["count"] == 1
        assert json.loads(cfg.read_text(encoding="utf-8"))["mcpServers"]["echo"]["command"] == "python"
        # owner 的内存副本同步更新（/api/mcp 摘要说有 1 个）
        assert srv.mcp_servers[0]["name"] == "echo"

        # 非法 JSON：400，文件不动
        bad = {"text": "{ 不是 json"}
        with pytest.raises(urllib.error.HTTPError) as exc:
            srv.client().post("/api/mcp/raw", bad)
        assert exc.value.code == 400
    finally:
        srv.stop()


def test_mcp_presets_endpoint_and_bundled_add(tmp_path, monkeypatch):
    """预设列表 + bundled（memory）一键添加：写盘、同步内存、经 restart 生效。"""
    cfg = tmp_path / "mcp.json"
    monkeypatch.setenv("FORGEAGENT_MCP_CONFIG", str(cfg))
    srv = _server_with(_FakeSessions())
    try:
        listing = srv.client().get("/api/mcp/presets")
        names = {p["name"]: p for p in listing["presets"]}
        assert {"time", "fetch", "git", "memory", "sqlite", "browser"} <= set(names)
        assert names["memory"]["bundled"] is True
        assert names["memory"]["in_config"] is False

        res = srv.client().post("/api/mcp/presets/add", {"name": "memory"})
        assert res["ok"] is True and res["count"] == 1
        saved = json.loads(cfg.read_text(encoding="utf-8"))
        args = saved["mcpServers"]["memory"]["args"]
        # 路径断言要跨平台兼容：Windows 上 str(Path) 出来是反斜杠，
        # endswith("examples/memory_mcp_server.py") 这种写死斜杠的写法必然挂。
        script = Path(args[0])
        assert len(args) == 1 and script.name == "memory_mcp_server.py"
        assert script.parent.name == "examples"
        # bundled 不假装能 pip：没有 package
        assert saved["mcpServers"]["memory"]["command"].endswith(("python", "python3", "bin/python")) or "python" in saved["mcpServers"]["memory"]["command"]

        # 再加一次：chips 的"已添加"链路 —— 幂等（同名覆盖，不报错）
        res2 = srv.client().post("/api/mcp/presets/add", {"name": "memory"})
        assert res2["ok"] is True and res2["count"] == 1

        # 未知预设：404
        import urllib.error
        with pytest.raises(urllib.error.HTTPError) as exc:
            srv.client().post("/api/mcp/presets/add", {"name": "nope"})
        assert exc.value.code == 404
    finally:
        srv.stop()



def test_session_rename_round_trip():
    """POST /api/session/rename 落到会话源的 rename（本地 alias，不改库）。"""
    fake = _FakeSessions()
    srv = _server_with(fake)
    try:
        res = srv.client().post(
            "/api/session/rename", {"session_id": "sess_abc123", "title": "我的项目"}
        )
        assert res == {"ok": True, "session": "sess_abc123", "title": "我的项目"}
        assert fake.renamed == [("sess_abc123", "我的项目")]
    finally:
        srv.stop()


def test_session_rename_requires_id(server):
    import urllib.error
    with pytest.raises(urllib.error.HTTPError) as exc:
        server.client().post("/api/session/rename", {})
    assert exc.value.code == 400
