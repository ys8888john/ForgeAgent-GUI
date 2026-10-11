"""多媒体模型设置（默认图像识别 / 图像生成模型）的测试。

锁的行为：
1. 保存 = 写进**当前激活 profile** 的 env（AGENTD_VISION_* / AGENTD_IMAGE_*）；
2. BASE_URL / API_KEY 从 profile 主凭据派生 —— 聊天和多媒体同一家 provider，
   不该让用户把同一把 key 抄三遍；
3. 清空保存 = 整组移除（= 关掉该能力）；
4. 保存后热配置文件（hotenv.json）同步重写 —— 运行中的 agentd 下一轮生效、不重启；
5. 没有激活 profile / 空字段时给 400 明确报错。
"""

from __future__ import annotations

import json
from urllib.error import HTTPError

import pytest

from forgeagent.gui import models as M
from forgeagent.gui.bridge import Bridge
from forgeagent.gui.server import UiServer


@pytest.fixture(autouse=True)
def _tmp_home(tmp_path, monkeypatch):
    """把 ~/.agentd/gui 指到临时目录，绝不碰用户真实配置。"""
    monkeypatch.setattr(M, "models_path", lambda: tmp_path / "models.json")
    monkeypatch.setattr(M, "hotenv_path", lambda: tmp_path / "hotenv.json")
    yield


class _FakeClient:
    def __init__(self):
        self.session_id = "sess_original123"
        self.restarts = 0

    async def close(self):
        pass

    async def start(self):
        self.restarts += 1


@pytest.fixture()
def server(tmp_path):
    fake = _FakeClient()
    srv = UiServer(bridge=Bridge(client=fake)).start()
    srv._fake = fake
    yield srv
    srv.stop()


def _post(c, path, body):
    """POST 并把 4xx 折成 {ok: False, error: ...}（LocalClient 遇 4xx 会直接抛）。"""
    try:
        return c.post(path, body)
    except HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8"))
        except Exception:  # noqa: BLE001 - 读不出 body 就只报状态码
            detail = {}
        out = {"ok": False, "status": exc.code}
        if isinstance(detail, dict):
            out.update(detail)
        return out


def _setup_active_profile(server, env):
    c = server.client()
    c.post("/api/models", {"data": {"active": None, "profiles": [
        {"id": "z1", "name": "智谱", "env": env},
    ]}})
    r = c.post("/api/model/select", {"id": "z1"})
    assert r["ok"], r
    return c


# ---- 保存与派生 ----


def test_media_save_writes_active_profile_and_derives_credentials(server):
    c = _setup_active_profile(server, {
        "AGENTD_LLM_BACKEND": "zhipu",
        "AGENTD_ZHIPU_API_KEY": "sk-abcdef123456",
        "AGENTD_ZHIPU_BASE_URL": "https://open.bigmodel.cn/api/paas/v4",
        "AGENTD_ZHIPU_MODEL": "glm-4.5-air",
    })
    r = c.post("/api/model/media", {
        "vision_model": "glm-4.6v",
        "image_model": "glm-image",
    })
    assert r["ok"], r

    data = M.load_models()
    profile = next(p for p in data["profiles"] if p["id"] == "z1")
    env = profile["env"]
    # 模型名落在激活 profile 上
    assert env["AGENTD_VISION_MODEL"] == "glm-4.6v"
    assert env["AGENTD_IMAGE_MODEL"] == "glm-image"
    # base_url / key 从主凭据派生 —— 同一把 key 不用抄三遍
    assert env["AGENTD_VISION_BASE_URL"] == "https://open.bigmodel.cn/api/paas/v4"
    assert env["AGENTD_IMAGE_BASE_URL"] == "https://open.bigmodel.cn/api/paas/v4"
    assert env["AGENTD_VISION_API_KEY"] == "sk-abcdef123456"
    assert env["AGENTD_IMAGE_API_KEY"] == "sk-abcdef123456"
    # 聊天配置原封不动
    assert env["AGENTD_ZHIPU_MODEL"] == "glm-4.5-air"


def test_media_save_hot_swaps_without_restart(server):
    c = _setup_active_profile(server, {
        "AGENTD_LLM_BACKEND": "zhipu",
        "AGENTD_ZHIPU_API_KEY": "sk-x",
        "AGENTD_ZHIPU_MODEL": "glm-4.5-air",
    })
    r = c.post("/api/model/media", {"image_model": "glm-image"})
    assert r["ok"], r
    # 关键：不重启 agentd（会话与历史保留），热配置文件已更新
    assert server._fake.restarts == 0
    hot = M.hotenv_path()
    assert hot.exists()
    data = json.loads(hot.read_text(encoding="utf-8"))
    assert data["AGENTD_IMAGE_MODEL"] == "glm-image"
    assert data["AGENTD_IMAGE_API_KEY"] == "sk-x"


def test_media_clear_removes_whole_group(server):
    c = _setup_active_profile(server, {
        "AGENTD_LLM_BACKEND": "zhipu",
        "AGENTD_ZHIPU_API_KEY": "sk-x",
        "AGENTD_ZHIPU_MODEL": "glm-4.5-air",
    })
    # 先设上
    r = c.post("/api/model/media", {"image_model": "glm-image"})
    assert r["ok"]
    # 再清空 —— 整组置空串（agentd 侧空值 = 未配置）。
    # 注意必须是显式空串而不是 pop：save_models 的 merge 会把缺失键继承回旧值。
    r = c.post("/api/model/media", {"image_model": ""})
    assert r["ok"]
    env = next(p for p in M.load_models()["profiles"] if p["id"] == "z1")["env"]
    assert env["AGENTD_IMAGE_MODEL"] == ""
    assert env["AGENTD_IMAGE_BASE_URL"] == ""
    assert env["AGENTD_IMAGE_API_KEY"] == ""


def test_media_without_any_credentials_only_sets_model(server):
    c = _setup_active_profile(server, {"AGENTD_LLM_BACKEND": "ollama"})
    r = c.post("/api/model/media", {"image_model": "qwen-image"})
    assert r["ok"]
    env = next(p for p in M.load_models()["profiles"] if p["id"] == "z1")["env"]
    assert env["AGENTD_IMAGE_MODEL"] == "qwen-image"
    # 没有 key 可派生就留空，绝不编造占位值
    assert env.get("AGENTD_IMAGE_API_KEY", "") == ""


def test_media_ollama_vision_uses_host_and_image_uses_compat_endpoint(server):
    c = _setup_active_profile(server, {
        "AGENTD_LLM_BACKEND": "ollama",
        "AGENTD_OLLAMA_HOST": "http://127.0.0.1:11434",
    })
    r = c.post("/api/model/media", {"vision_model": "qwen3-vl", "image_model": "x"})
    assert r["ok"]
    env = next(p for p in M.load_models()["profiles"] if p["id"] == "z1")["env"]
    # Ollama 没有云端 BASE_URL：视觉走原生协议（HOST），图像走它的 OpenAI 兼容端点
    assert env["AGENTD_VISION_HOST"] == "http://127.0.0.1:11434"
    assert env["AGENTD_IMAGE_BASE_URL"] == "http://127.0.0.1:11434/v1"


# ---- 错误路径 ----


def test_media_requires_active_profile(server):
    c = server.client()
    r = _post(c, "/api/model/media", {"image_model": "glm-image"})
    assert r.get("ok") is False
    assert "激活" in str(r.get("error") or "")


def test_media_without_fields_is_400(server):
    _setup_active_profile(server, {"AGENTD_LLM_BACKEND": "zhipu", "AGENTD_ZHIPU_API_KEY": "sk-x"})
    r = _post(server.client(), "/api/model/media", {})
    assert r.get("ok") is False
    assert "字段" in str(r.get("error") or "")
