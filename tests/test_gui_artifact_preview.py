"""产物图片内联预览（POST /api/artifact_preview）的测试。

锁的是三条边界，少一条这个接口就变成任意文件读取：

1. 白名单内的图片 → 返回 base64 + 正确的 MIME；
2. 非图片扩展名 → 拒绝（.txt / .exe 都不给读，哪怕它在白名单里）；
3. 白名单外的路径（如系统目录）→ 拒绝；
4. 超大文件 → 拒绝（别把 100MB 的图塞进 JSON-RPC 帧里）。

另外验一遍它是**带鉴权**的：这是它和 /api/file/<id> 的根本区别 —— 后者因为
<img src> 发不了自定义头才被迫免 token，而产物路径走前端 fetch，没必要开这个口子。
"""

from __future__ import annotations

import base64
import json
from urllib.error import HTTPError

import pytest

from forgeagent.gui import server as S
from forgeagent.gui.bridge import Bridge
from forgeagent.gui.server import UiServer

# 一张 1×1 的 PNG，够小、也够证明"字节原样回来了"
PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8AAAwAB/wFLBgEhAAAAAElFTkSuQmCC"
)


class _FakeClient:
    def __init__(self):
        self.session_id = "sess_x"
        self.restarts = 0

    async def close(self):
        pass

    async def start(self):
        self.restarts += 1


@pytest.fixture()
def server(tmp_path, monkeypatch):
    """空间目录 = tmp_path，白名单的第二处（tool_results）也指过去。"""
    fake = _FakeClient()
    srv = UiServer(bridge=Bridge(client=fake)).start()
    srv.current_space_dir = str(tmp_path)
    monkeypatch.setattr(S, "_tool_results_root", lambda: tmp_path / "tool_results")
    yield srv
    srv.stop()


def _preview(server, path):
    try:
        return server.client().post("/api/artifact_preview", {"path": path})
    except HTTPError as exc:
        try:
            return json.loads(exc.read().decode("utf-8"))
        except Exception:  # noqa: BLE001
            return {"ok": False, "status": exc.code}


def test_image_inside_whitelist_returns_bytes(server, tmp_path):
    p = tmp_path / "duck.png"
    p.write_bytes(PNG_1X1)
    r = _preview(server, str(p))
    assert r["ok"], r
    assert r["mime"] == "image/png"
    assert base64.b64decode(r["b64"]) == PNG_1X1


def test_jpeg_gets_jpeg_mime(server, tmp_path):
    p = tmp_path / "photo.jpg"
    p.write_bytes(b"\xff\xd8\xff\xe0")
    r = _preview(server, str(p))
    assert r["ok"], r
    assert r["mime"] == "image/jpeg"


def test_non_image_extension_is_rejected(server, tmp_path):
    p = tmp_path / "notes.txt"
    p.write_text("hello", encoding="utf-8")
    r = _preview(server, str(p))
    assert not r["ok"]
    assert "不是图片" in r["error"]


def test_path_outside_whitelist_is_rejected(server, tmp_path):
    """白名单之外的绝对路径必须挡住 —— 否则这就是一个任意文件读取漏洞。"""
    r = _preview(server, r"C:\Windows\System32\config\SAM")
    assert not r["ok"]
    assert "不在允许范围" in r["error"]


def test_oversized_image_is_rejected(server, tmp_path, monkeypatch):
    monkeypatch.setattr(S, "_ARTIFACT_PREVIEW_MAX_BYTES", 8)
    p = tmp_path / "big.png"
    p.write_bytes(PNG_1X1)
    r = _preview(server, str(p))
    assert not r["ok"]
    assert "过大" in r["error"]


def test_missing_file_is_rejected(server, tmp_path):
    r = _preview(server, str(tmp_path / "nope.png"))
    assert not r["ok"]


def test_endpoint_requires_token(server, tmp_path):
    """带鉴权：不带 X-ForgeAgent-Token 的请求一律 401。

    这是它区别于 /api/file/<id> 的关键 —— 产物预览由前端 fetch 发起，
    完全没必要为它开一个免 token 的口子。
    """
    p = tmp_path / "duck.png"
    p.write_bytes(PNG_1X1)
    base = f"http://127.0.0.1:{server.port}"
    req = __import__("urllib.request", fromlist=["Request"]).Request(
        base + "/api/artifact_preview",
        data=json.dumps({"path": str(p)}).encode("utf-8"),
        headers={"content-type": "application/json"},
        method="POST",
    )
    with pytest.raises(HTTPError) as ei:
        __import__("urllib.request", fromlist=["urlopen"]).urlopen(req, timeout=10)
    assert ei.value.code == 401
