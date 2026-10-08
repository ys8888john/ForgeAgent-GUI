"""空间（Space）功能测试 —— 沿用 test_gui_server 的「真 HTTP + 假 client」套路。

重点验三件事：
1. 默认空间自动建好、能列出来、能切换；
2. 新建会话把 session 绑定到「当前空间」，续聊再按绑定还原 cwd；
3. 切换空间会改后续会话的 cwd（不重启 agentd 子进程，靠 session/new、load 带 cwd）。

SpaceManager 的注册表文件（spaces.json / session_spaces.json）都落在 tmp_path，
保持测试密闭；只有 default 空间的目录仍会落到真实 ~/.agentd/spaces/default
（和 UiServer 写 hotenv.json 同款副作用，幂等无害）。
"""

from __future__ import annotations

import json
import pytest
from pathlib import Path

from forgeagent.gui import dialogs
from forgeagent.gui.bridge import Bridge
from forgeagent.gui.server import UiServer
from forgeagent.gui.spaces import SpaceManager
from test_gui_bridge import _FakeClient


def _server_with_spaces(tmp_path: Path, bindings: dict | None = None) -> UiServer:
    meta = tmp_path / "spaces.json"
    spath = tmp_path / "session_spaces.json"
    if bindings is not None:
        spath.write_text(__import__("json").dumps(bindings), encoding="utf-8")
    spaces = SpaceManager(meta_path=meta, session_spaces_path=spath)
    srv = UiServer(bridge=Bridge(client=_FakeClient()), spaces=spaces).start()
    return srv


class _FakeSessions:
    """最小 sessions 替身：只回答 exists()。"""

    def __init__(self, exists: bool = True) -> None:
        self._exists = exists

    def exists(self, session_id: str) -> bool:
        return self._exists


def test_default_space_is_active_on_first_run(tmp_path):
    srv = _server_with_spaces(tmp_path)
    try:
        res = srv.client().get("/api/spaces")
        assert res["ok"] is True
        names = [s["name"] for s in res["spaces"]]
        assert "default" in names
        # 默认空间就是激活的
        assert res["active"] == "default"
        # UiServer 把默认空间目录当作后续会话的 cwd
        assert res["path"].replace("\\", "/").endswith(".agentd/spaces/default")
        # 当前激活空间名也透到了 server 上
        assert srv.current_space_name == "default"
    finally:
        srv.stop()


def test_space_new_creates_and_activates(tmp_path):
    srv = _server_with_spaces(tmp_path)
    try:
        before = srv.client().get("/api/spaces")["active"]
        assert before == "default"

        res = srv.client().post("/api/space/new", {"name": "myproject"})
        assert res["ok"] is True
        assert res["active"] == "myproject"

        # 新建即进入：header 下拉的 active 应已是新空间
        after = srv.client().get("/api/spaces")
        assert after["active"] == "myproject"
        # 后续会话的 cwd 跟着切到了新空间目录
        assert srv.current_space_dir == res["space"]["path"]
        # 切换已落到 client（set_cwd 被调用过一次）
        assert "myproject" in srv.bridge._client._cwd.replace("\\", "/")
    finally:
        srv.stop()


def test_space_new_with_explicit_path(tmp_path):
    target = tmp_path / "existing_dir"
    target.mkdir()
    srv = _server_with_spaces(tmp_path)
    try:
        res = srv.client().post("/api/space/new", {"name": "ext", "path": str(target)})
        assert res["ok"] is True
        assert res["space"]["path"] == str(target)
        # 续聊/新建会话时 cwd 就是用户指定的目录
        assert srv.current_space_dir == str(target)
    finally:
        srv.stop()


def test_space_switch_changes_active_and_cwd(tmp_path):
    srv = _server_with_spaces(tmp_path)
    try:
        srv.client().post("/api/space/new", {"name": "alpha"})
        srv.client().post("/api/space/new", {"name": "beta"})
        res = srv.client().post("/api/space/switch", {"name": "alpha"})
        assert res["ok"] is True
        assert res["active"] == "alpha"
        assert srv.current_space_name == "alpha"
        # 切回 beta
        srv.client().post("/api/space/switch", {"name": "beta"})
        assert srv.client().get("/api/spaces")["active"] == "beta"
    finally:
        srv.stop()


def test_session_new_binds_to_current_space(tmp_path):
    srv = _server_with_spaces(tmp_path)
    try:
        srv.client().post("/api/space/new", {"name": "work"})
        res = srv.client().post("/api/session/new", {})
        assert res["ok"] is True
        sid = res["session"]
        assert res["space"]["name"] == "work"
        # 绑定落盘：session_spaces.json 记录了 sid -> work
        bound = __import__("json").loads(
            (tmp_path / "session_spaces.json").read_text(encoding="utf-8")
        )
        assert bound.get(sid) == "work"
    finally:
        srv.stop()


def test_session_resume_restores_space_cwd(tmp_path):
    """把一个会话预先绑到 spaceA，再把 active 切到 spaceB，续聊应切回 spaceA 的 cwd。"""
    # 预置绑定：会话 s1 属于 spaceA
    bindings = {"s1": "spaceA"}
    srv = _server_with_spaces(tmp_path, bindings=bindings)
    try:
        # 建两个空间
        srv.client().post("/api/space/new", {"name": "spaceA", "path": str(tmp_path / "a")})
        srv.client().post("/api/space/new", {"name": "spaceB", "path": str(tmp_path / "b")})
        # 当前 active 是 spaceB（最后建的）
        assert srv.current_space_name == "spaceB"

        # 续聊 s1：server 应查出它属于 spaceA，把 active 切回 spaceA 并带 spaceA 的 cwd
        srv.sessions = _FakeSessions(exists=True)
        res = srv.client().post("/api/session/resume", {"session_id": "s1"})
        assert res["ok"] is True
        assert srv.current_space_name == "spaceA"
        assert srv.current_space_dir.replace("\\", "/").endswith("/a")
        # bridge 把 client 的 cwd 切到了 spaceA 目录
        assert "a" in srv.bridge._client._cwd.replace("\\", "/")
        # 续聊确实走了 load_session
        assert "s1" in srv.bridge._client.load_calls
    finally:
        srv.stop()


def _post_expecting_error(srv, body):
    """发 POST，把 4xx 响应体也解析回来（LocalClient 对非零状态码会抛 HTTPError）。"""
    import urllib.error
    import urllib.request

    req = urllib.request.Request(
        f"http://127.0.0.1:{srv.port}/api/space/new",
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={"X-ForgeAgent-Token": srv.token, "Content-Type": "application/json"},
    )
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=30)
    assert exc.value.code == 400
    payload = json.loads(exc.value.read().decode("utf-8"))
    assert payload.get("ok") is False
    return payload


def test_space_new_rejects_duplicate_and_empty(tmp_path):
    srv = _server_with_spaces(tmp_path)
    try:
        # 先建一个
        ok = srv.client().post("/api/space/new", {"name": "dup"})
        assert ok["ok"] is True
        # 重名应 400
        _post_expecting_error(srv, {"name": "dup"})
        # 空名应 400
        _post_expecting_error(srv, {"name": ""})
    finally:
        srv.stop()


def test_pick_directory_returns_picked(tmp_path, monkeypatch):
    """/api/pick-directory 把对话框结果透传回来；取消返回 path=null 不报错。"""
    srv = _server_with_spaces(tmp_path)

    def fake_pick(initial):
        # initial 透传给对话框，确认前端把当前输入目录传下去了
        assert initial == "/some/start"
        return "/some/start/sub"

    monkeypatch.setattr(dialogs, "pick_directory", fake_pick)
    try:
        res = srv.client().post("/api/pick-directory", {"initial": "/some/start"})
        assert res["ok"] is True
        assert res["path"] == "/some/start/sub"

        # 取消 / 无对话框：path 为 None，仍 ok
        monkeypatch.setattr(dialogs, "pick_directory", lambda initial: None)
        res2 = srv.client().post("/api/pick-directory", {"initial": None})
        assert res2["ok"] is True
        assert res2["path"] is None
    finally:
        srv.stop()


def test_space_ensure_path_idempotent(tmp_path):
    """/api/space/ensure：目录落地成空间并激活；重复调用幂等，cwd 跟着走。"""
    target = tmp_path / "proj"
    target.mkdir()
    srv = _server_with_spaces(tmp_path)
    try:
        res = srv.client().post("/api/space/ensure", {"path": str(target)})
        assert res["ok"] is True
        assert res["active"] == "proj"
        assert res["space"]["path"] == str(target)
        # server 的当前空间与 client cwd 都切到了新目录
        assert srv.current_space_name == "proj"
        assert srv.current_space_dir == str(target)
        assert "proj" in srv.bridge._client._cwd.replace("\\", "/")

        # 同一路径再来一次：不报重名错，还是同一个空间（幂等）
        res2 = srv.client().post("/api/space/ensure", {"path": str(target)})
        assert res2["ok"] is True
        assert res2["active"] == "proj"
        assert [s["name"] for s in res2["spaces"]].count("proj") == 1
        assert srv.client().get("/api/spaces")["active"] == "proj"

        # 缺 path 应 400
        import urllib.error
        import urllib.request

        req = urllib.request.Request(
            f"http://127.0.0.1:{srv.port}/api/space/ensure",
            data=json.dumps({}).encode("utf-8"),
            method="POST",
            headers={"X-ForgeAgent-Token": srv.token, "Content-Type": "application/json"},
        )
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=30)
        assert exc.value.code == 400
    finally:
        srv.stop()
