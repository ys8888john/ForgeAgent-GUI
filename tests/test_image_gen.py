"""图片生成测试。

分两层：
  1. 纯逻辑层（image_gen.py）：generate 的 b64 / url / 错误 / 缺参 路径，
     以及 config 读写、list_image_profiles 的「只列有 key 的 profile」。
  2. HTTP 层（server.py）：真起 UiServer 真发请求，验证
     /api/image/generate 落盘 + 注册 id、/api/image/<id> 取图、
     /api/image/config 读写、缺 provider 回 400。

两层都不依赖真实图像 API：逻辑层 monkeypatch 掉 HTTP 调用，HTTP 层
monkeypatch 掉 image_gen.generate。
"""

from __future__ import annotations

import base64
import json

import pytest

from forgeagent.gui import image_gen as ig
from forgeagent.gui import server as srv_mod
from forgeagent.gui.bridge import Bridge
from forgeagent.gui.server import UiServer
from test_gui_bridge import _FakeClient


# ---------------------------------------------------------------------------
# 逻辑层
# ---------------------------------------------------------------------------

def test_default_config_shapes():
    cfg = ig.default_config()
    assert cfg["profile_id"] is None
    assert cfg["size"] == ig.DEFAULT_SIZE
    assert cfg["n"] == 1


def test_save_and_load_config(tmp_path, monkeypatch):
    monkeypatch.setattr(ig, "imagegen_path", lambda: tmp_path / "imagegen.json")
    saved = ig.save_config({"profile_id": "p1", "model": "dall-e-3", "size": "512x512", "n": 3})
    assert saved["profile_id"] == "p1"
    assert saved["n"] == 3
    loaded = ig.load_config()
    assert loaded["profile_id"] == "p1"
    assert loaded["model"] == "dall-e-3"
    assert loaded["size"] == "512x512"
    assert loaded["n"] == 3


def test_load_config_bad_size_falls_back(tmp_path, monkeypatch):
    monkeypatch.setattr(ig, "imagegen_path", lambda: tmp_path / "imagegen.json")
    ig.save_config({"size": "999x999", "n": 99})
    cfg = ig.load_config()
    assert cfg["size"] == ig.DEFAULT_SIZE
    assert cfg["n"] == ig.MAX_N  # 上 clamp


def test_list_image_profiles_only_with_key(monkeypatch):
    monkeypatch.setattr(
        ig,
        "load_models",
        lambda: {
            "active": "a",
            "profiles": [
                {"id": "a", "name": "无 key", "env": {"AGENTD_LLM_BACKEND": "ollama"}},
                {
                    "id": "b",
                    "name": "有 key",
                    "env": {
                        "AGENTD_OPENAI_API_KEY": "sk-x",
                        "AGENTD_OPENAI_BASE_URL": "https://api.example.com/v1",
                        "AGENTD_OPENAI_MODEL": "cogview-3-plus",
                    },
                },
            ],
        },
    )
    profiles = ig.list_image_profiles()
    ids = [p["id"] for p in profiles]
    assert "a" not in ids
    assert "b" in ids
    assert profiles[0]["model"] == "cogview-3-plus"


def test_generate_empty_prompt(monkeypatch):
    monkeypatch.setattr(ig, "load_models", lambda: {"profiles": []})
    r = ig.generate("", {})
    assert r["ok"] is False
    assert "提示词" in r["error"]


def test_generate_missing_key(monkeypatch):
    monkeypatch.setattr(ig, "load_models", lambda: {"profiles": []})
    r = ig.generate("一只猫", {})
    assert r["ok"] is False
    assert "API Key" in r["error"]


def test_generate_missing_model(monkeypatch):
    monkeypatch.setattr(ig, "load_models", lambda: {"profiles": []})
    r = ig.generate("一只猫", {"AGENTD_OPENAI_API_KEY": "sk-x"})
    assert r["ok"] is False
    assert "图像模型" in r["error"]


def test_generate_b64_response(monkeypatch):
    payload = {
        "data": [
            {"b64_json": base64.b64encode(b"img-one").decode()},
            {"b64_json": base64.b64encode(b"img-two").decode()},
        ]
    }
    monkeypatch.setattr(ig, "_http_post_json", lambda *a, **k: (200, payload))
    r = ig.generate(
        "一只猫",
        {"AGENTD_OPENAI_API_KEY": "sk-x", "AGENTD_OPENAI_BASE_URL": "https://api.example.com/v1"},
        model="dall-e-3",
    )
    assert r["ok"] is True
    assert len(r["images"]) == 2
    # 回传的是解码后的 b64（未经再编码），内容应能被解回原字节
    assert base64.b64decode(r["images"][0]["b64"]) == b"img-one"
    assert r["model"] == "dall-e-3"


def test_generate_url_response_fetches_bytes(monkeypatch):
    def fake_post(*a, **k):
        return (200, {"data": [{"url": "https://cdn.example.com/x.png"}]})

    def fake_get(url, headers, timeout):
        assert url.endswith("/x.png")
        return (b"remote-bytes", "image/png")

    monkeypatch.setattr(ig, "_http_post_json", fake_post)
    monkeypatch.setattr(ig, "_http_get_bytes", fake_get)
    r = ig.generate(
        "一只猫",
        {"AGENTD_OPENAI_API_KEY": "sk-x"},
        model="cogview-3-plus",
    )
    assert r["ok"] is True
    assert base64.b64decode(r["images"][0]["b64"]) == b"remote-bytes"
    assert r["images"][0]["mime"] == "image/png"


def test_generate_http_error_surfaced(monkeypatch):
    monkeypatch.setattr(
        ig, "_http_post_json", lambda *a, **k: (400, {"error": {"message": "bad size"}})
    )
    r = ig.generate("x", {"AGENTD_OPENAI_API_KEY": "sk-x"}, model="dall-e-3")
    assert r["ok"] is False
    assert "400" in r["error"]
    assert "bad size" in r["error"]


def test_generate_network_failure(monkeypatch):
    monkeypatch.setattr(
        ig, "_http_post_json", lambda *a, **k: (0, {"error": "URLError: timeout"})
    )
    r = ig.generate("x", {"AGENTD_OPENAI_API_KEY": "sk-x"}, model="dall-e-3")
    assert r["ok"] is False
    assert "timeout" in r["error"]


# ---------------------------------------------------------------------------
# HTTP 层
# ---------------------------------------------------------------------------

@pytest.fixture()
def img_server(tmp_path, monkeypatch):
    # 产物落盘到临时目录，不污染 ~/.agentd
    monkeypatch.setattr(srv_mod, "_tool_results_root", lambda: tmp_path / "tool_results")
    # 配置（imagegen.json）也落到临时目录，避免跨测试污染真实家目录
    monkeypatch.setattr(srv_mod._image_gen, "imagegen_path", lambda: tmp_path / "imagegen.json")
    # 让 generate 直接返回假图，不碰真实 API
    monkeypatch.setattr(
        srv_mod._image_gen,
        "generate",
        lambda prompt, profile_env, **kw: {
            "ok": True,
            "images": [
                {"b64": base64.b64encode(b"fake-png-1").decode(), "mime": "image/png"},
                {"b64": base64.b64encode(b"fake-png-2").decode(), "mime": "image/png"},
            ],
            "model": kw.get("model") or "dall-e-3",
        },
    )
    # generate 需要 profile 有 key：env_for 直接给一个
    # 注意 handler 调的是 _models.env_for（模块属性），不是 server 顶层导入名
    monkeypatch.setattr(srv_mod._models, "env_for", lambda pid: {"AGENTD_OPENAI_API_KEY": "sk-x"})
    s = UiServer(bridge=Bridge(client=_FakeClient())).start()
    try:
        yield s
    finally:
        s.stop()


def test_image_config_get(img_server):
    res = img_server.client().get("/api/image/config")
    assert res["ok"] is True
    assert "config" in res
    assert "sizes" in res and "1024x1024" in res["sizes"]


def test_image_generate_and_serve(img_server):
    api = img_server.client()
    res = api.post(
        "/api/image/generate",
        {"prompt": "一只猫", "profile_id": "p1", "model": "dall-e-3", "size": "1024x1024", "n": 2},
    )
    assert res["ok"] is True
    assert len(res["images"]) == 2
    im = res["images"][0]
    assert im["url"].startswith("/api/image/")
    assert im["path"] and im["name"]

    # 凭 url 取图：HTTP 层直接打 url（不走 token），拿到原始字节
    import urllib.request

    raw = urllib.request.urlopen(f"http://127.0.0.1:{img_server.port}{im['url']}", timeout=5)
    assert raw.read() == b"fake-png-1"


def test_image_generate_missing_profile(img_server):
    import urllib.error

    # 不传 profile_id → 服务端回 400（LocalClient 对 4xx 会抛，故直接打原始请求）
    req = urllib.request.Request(
        f"http://127.0.0.1:{img_server.port}/api/image/generate",
        data=json.dumps({"prompt": "一只猫"}).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-ForgeAgent-Token": img_server.token,
        },
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=5)
    assert exc.value.code == 400


def test_image_serve_unknown_id(img_server):
    import urllib.error

    req = urllib.request.Request(f"http://127.0.0.1:{img_server.port}/api/image/deadbeef")
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=5)
    assert exc.value.code == 404


def test_image_config_save_roundtrip(img_server):
    api = img_server.client()
    saved = api.post(
        "/api/image/config",
        {"config": {"profile_id": "p2", "model": "cogview-3-plus", "size": "512x512", "n": 3}},
    )
    assert saved["ok"] is True
    assert saved["config"]["profile_id"] == "p2"
    assert saved["config"]["n"] == 3
