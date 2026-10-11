"""图片识别链路（ForgeAgent-GUI 侧）回归测试。

链路：GUI 选图 → POST /api/upload（存临时文件、发 id）→ POST /api/send 带
id 列表 → 服务端按 id 认领元数据 → bridge.send → AcpClient.prompt（读文件
编 base64 成 ACP image block）。这里用假 client 把每一跳都钉死：

- 上传：白名单 mime / 大小上限 / 非法 base64 拒收
- 认领：瞎编 id 跳过、合法 id 解析出服务端路径（路径永远不出服务端）
- /api/file/<id>：凭 id 免 token 头可访问（<img src> 发不了自定义头），
  404 当文件丢了
- user 事件：有图才带 images 键（无图保持旧形状，消费方零感知）
"""

from __future__ import annotations

import base64
import urllib.error
import urllib.request

import pytest

from forgeagent.gui.bridge import Bridge
from forgeagent.gui.server import UiServer
from test_gui_bridge import _FakeClient

# 1x1 红色 PNG，43 字节，标准的测试用最小图片
_TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


class _ImageCaptureClient(_FakeClient):
    """在 _FakeClient 之上记录 prompt 收到的 images 参数。"""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.prompt_calls: list[dict] = []

    async def prompt(self, text: str, images=None):
        self.prompt_calls.append({"text": text, "images": list(images or [])})
        async for t in super().prompt(text, images=images):
            yield t


@pytest.fixture()
def server():
    srv = UiServer(bridge=Bridge(client=_ImageCaptureClient())).start()
    try:
        yield srv
    finally:
        srv.stop()


def _upload(api, name="shot.png", mime="image/png", data=None):
    return api.post(
        "/api/upload",
        {"data": base64.b64encode(data or _TINY_PNG).decode("ascii"), "name": name, "mime": mime},
    )


def test_upload_stores_file_and_returns_meta(server):
    api = server.client()
    r = _upload(api)
    assert r["ok"] is True
    assert r["mime"] == "image/png"
    assert r["url"].startswith("/api/file/")
    # 文件真的落在临时目录里，字节一致
    from pathlib import Path

    assert Path(r["path"]).read_bytes() == _TINY_PNG


def test_upload_rejects_non_image_and_bad_payload(server):
    api = server.client()

    def _expect_http_error(path_body: dict, code: int) -> None:
        with pytest.raises(urllib.error.HTTPError) as exc:
            api.post("/api/upload", path_body)
        assert exc.value.code == code

    # mime 不在白名单 -> 415
    _expect_http_error(
        {"data": base64.b64encode(_TINY_PNG).decode("ascii"),
         "name": "evil.exe", "mime": "application/x-msdownload"},
        415,
    )
    # 空 base64 -> 400
    _expect_http_error({"data": "", "name": "a.png", "mime": "image/png"}, 400)
    # 非 base64 -> 400
    _expect_http_error(
        {"data": "!!!not-b64!!!", "name": "a.png", "mime": "image/png"}, 400
    )


def test_send_with_images_claims_by_id_and_passes_paths(server):
    api = server.client()
    up = _upload(api)
    up2 = _upload(api, name="b.jpg", mime="image/jpeg")

    assert api.post(
        "/api/send", {"text": "这是什么", "images": [up["id"], up2["id"], "bogus-id"]}
    )["ok"] is True

    fake = server.bridge._client
    # 等 _stream 把 prompt 调用记下来
    import time

    for _ in range(40):
        if fake.prompt_calls:
            break
        time.sleep(0.05)
    assert fake.prompt_calls, "fake client 没收到 prompt"
    call = fake.prompt_calls[0]
    assert call["text"] == "这是什么"
    paths = [m["path"] for m in call["images"]]
    assert paths == [up["path"], up2["path"]]  # 瞎编的 bogus-id 被跳过

    # user 事件带两张图的 url（前端画缩略图用）
    evs = []
    for _ in range(20):
        evs.extend(api.get("/api/events?timeout=1").get("events", []))
        if any(e.get("type") == "done" for e in evs):
            break
    user_ev = next(e for e in evs if e.get("type") == "user")
    assert user_ev["images"] == [up["url"], up2["url"]]


def test_send_text_only_user_event_has_no_images_key(server):
    """无图发送的 user 事件保持旧形状（不带 images 键）。"""
    api = server.client()
    assert api.post("/api/send", {"text": "纯文本"})["ok"] is True
    evs = []
    for _ in range(20):
        evs.extend(api.get("/api/events?timeout=1").get("events", []))
        if any(e.get("type") == "done" for e in evs):
            break
    user_ev = next(e for e in evs if e.get("type") == "user")
    assert "images" not in user_ev


def test_file_served_by_id_without_token_header(server):
    """<img src> 发不了 X-ForgeAgent-Token 头 —— /api/file/<id> 凭 id 放行。"""
    api = server.client()
    up = _upload(api)
    url = f"http://127.0.0.1:{server.port}{up['url']}"
    with urllib.request.urlopen(url, timeout=5) as r:
        assert r.read() == _TINY_PNG
        assert r.headers.get("Content-Type") == "image/png"
    # 不存在的 id -> 404
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(
            f"http://127.0.0.1:{server.port}/api/file/deadbeef", timeout=5
        )
    assert exc.value.code == 404


def test_prompt_builds_valid_image_blocks(tmp_path, monkeypatch):
    """真 AcpClient 的图片块构造（此前全被 _FakeClient 桩掉，漏过 real bug：

    _read_image_b64 返回 (base64, mime) 元组，调用处整个塞给了 data、
    把 base64[0] 当 mimeType —— agentd 侧 pydantic 校验炸 -32602。
    这里不发子进程，monkeypatch _call 抓 params，并用 acp SDK 的
    PromptRequest 模型做权威校验。
    """
    import asyncio
    import json

    from acp.schema import PromptRequest
    from forgeagent.acp_client import AcpClient

    png = tmp_path / "t.png"
    png.write_bytes(_TINY_PNG)

    client = AcpClient()  # __init__ 不拉子进程（spawn 在 start()）
    client.session_id = "sess_test"
    client._notifications = asyncio.Queue()
    captured: list[dict] = []

    async def fake_call(method, params):
        captured.append({"method": method, "params": params})
        return {"result": {"stopReason": "end_turn"}}

    monkeypatch.setattr(client, "_call", fake_call)

    async def run():
        async for _ in client.prompt(
            "介绍一下这张图片",
            images=[{"path": str(png), "mime": "image/png"}],
        ):
            pass

    asyncio.run(run())

    assert captured and captured[0]["method"].endswith("prompt")
    blocks = captured[0]["params"]["prompt"]
    assert blocks[0] == {"type": "text", "text": "介绍一下这张图片"}
    img = blocks[1]
    # data 必须是纯 base64 字符串（不是元组/列表），mimeType 是 mime
    assert isinstance(img["data"], str)
    assert img["mimeType"] == "image/png"
    assert base64.b64decode(img["data"]) == _TINY_PNG
    # 权威校验：agentd 侧 SDK 就是拿 PromptRequest 收的，过不了它就是 -32602
    PromptRequest.model_validate(captured[0]["params"])
