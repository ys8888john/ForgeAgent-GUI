"""models.py / 模型切换链路的测试。

核心要锁的行为：
1. profile 只能带 AGENTD_* 前缀的键（防「任意环境变量注入」）；
2. 编辑留空不丢旧 key（merge 语义）；
3. GET 层脱敏不回写真值 —— 掩码串绝不能覆盖真实 API Key；
4. 切换模型 = 用新 env 重启 agentd 子进程，会话 id 尽量保住。
"""

from __future__ import annotations

import json

import pytest

from forgeagent.gui import models as M
from forgeagent.gui.bridge import Bridge
from forgeagent.gui.server import UiServer


@pytest.fixture(autouse=True)
def _tmp_home(tmp_path, monkeypatch):
    """把 ~/.forgeagent 指到临时目录，绝不碰用户真实配置。"""
    monkeypatch.setattr(M, "models_path", lambda: tmp_path / "models.json")
    yield


def _profile(pid="p1", name="测试", env=None):
    return {"id": pid, "name": name, "env": env or {}}


# ---- 白名单 ----

def test_non_agentd_keys_are_rejected():
    p = M.sanitize_profile(_profile(env={"PATH": "/evil", "LD_PRELOAD": "x", "AGENTD_LLM_BACKEND": "zhipu"}))
    assert p["env"] == {"AGENTD_LLM_BACKEND": "zhipu"}


def test_env_for_returns_only_whitelisted():
    M.save_models({"active": "p1", "profiles": [_profile("p1", env={"AGENTD_LLM_BACKEND": "zhipu", "HOME": "/x"})]})
    assert M.env_for("p1") == {"AGENTD_LLM_BACKEND": "zhipu"}


def test_env_for_unknown_or_empty_returns_empty():
    assert M.env_for(None) == {}
    assert M.env_for("nope") == {}


# ---- 基本读写 ----

def test_save_and_load_roundtrip():
    out = M.save_models({"active": "a", "profiles": [_profile("a", "A"), _profile("b", "B")]})
    assert [p["id"] for p in out["profiles"]] == ["a", "b"]
    loaded = M.load_models()
    assert loaded["active"] == "a"
    assert len(loaded["profiles"]) == 2


def test_active_missing_profile_is_dropped():
    M.save_models({"active": "ghost", "profiles": [_profile("a")]})
    assert M.load_models()["active"] is None


def test_broken_file_yields_empty_skeleton():
    M.models_path().parent.mkdir(parents=True, exist_ok=True)
    M.models_path().write_text("{not json", encoding="utf-8")
    assert M.load_models() == {"active": None, "profiles": []}


# ---- merge 语义（编辑留空不丢 key）----

def test_edit_keeps_keys_left_blank():
    M.save_models({"active": "p1", "profiles": [
        _profile("p1", env={"AGENTD_LLM_BACKEND": "zhipu", "AGENTD_ZHIPU_API_KEY": "sk-secret-value-123",
                             "AGENTD_ZHIPU_MODEL": "glm-4.5-air"}),
    ]})
    # 编辑表单提交：key 留空（缺失），model 改了
    M.save_models({"active": "p1", "profiles": [
        _profile("p1", env={"AGENTD_LLM_BACKEND": "zhipu", "AGENTD_ZHIPU_MODEL": "glm-4.6"}),
    ]})
    env = M.env_for("p1")
    assert env["AGENTD_ZHIPU_API_KEY"] == "sk-secret-value-123"  # 旧 key 保住了
    assert env["AGENTD_ZHIPU_MODEL"] == "glm-4.6"               # 新值生效


def test_masked_value_roundtrip_does_not_overwrite_secret():
    """前端把 GET 到的掩码原样回传，不能覆盖真实 key。"""
    secret = "6eed7c9721214fd8a4fa84958de4f0fe.ipjot9HApSgBm6LG"
    M.save_models({"active": "p1", "profiles": [_profile("p1", env={"AGENTD_ZHIPU_API_KEY": secret})]})
    masked = f"{secret[:3]}…{secret[-3:]}"
    # 前端整份回传，key 的值是掩码
    M.save_models({"active": "p1", "profiles": [_profile("p1", env={"AGENTD_ZHIPU_API_KEY": masked})]})
    assert M.env_for("p1")["AGENTD_ZHIPU_API_KEY"] == secret


def test_duplicate_ids_last_wins():
    M.save_models({"active": None, "profiles": [
        _profile("p1", env={"AGENTD_LLM_BACKEND": "ollama"}),
        _profile("p1", env={"AGENTD_LLM_BACKEND": "zhipu"}),
    ]})
    profiles = M.load_models()["profiles"]
    assert len(profiles) == 1
    assert profiles[0]["env"]["AGENTD_LLM_BACKEND"] == "zhipu"


# ---- HTTP 层 ----

class _FakeClient:
    def __init__(self):
        self.session_id = "sess_original123"
        self.stderr_lines = []
        self.env = None
        self.restarts = 0

    def set_env(self, env):
        self.env = env

    async def close(self):
        pass

    async def start(self):
        self.restarts += 1
        self.session_id = "sess_fresh_" + str(self.restarts)


@pytest.fixture()
def server(tmp_path):
    fake = _FakeClient()
    srv = UiServer(bridge=Bridge(client=fake)).start()
    srv._fake = fake
    yield srv
    srv.stop()


def _client(srv):
    return srv.client()


def test_models_crud_over_http(server):
    c = _client(server)
    # 初始为空
    r = c.get("/api/models")
    assert r["ok"] and r["profiles"] == []

    # 新增
    body = {"data": {"active": None, "profiles": [
        {"id": "z1", "name": "智谱", "env": {"AGENTD_LLM_BACKEND": "zhipu",
                                              "AGENTD_ZHIPU_API_KEY": "sk-abcdef123456",
                                              "AGENTD_ZHIPU_MODEL": "glm-4.5-air"}},
    ]}}
    r = c.post("/api/models", body)
    assert r["ok"]
    p = r["data"]["profiles"][0]
    assert p["env"]["AGENTD_ZHIPU_MODEL"] == "glm-4.5-air"       # 非密钥不脱敏
    assert p["env"]["AGENTD_ZHIPU_API_KEY"] != "sk-abcdef123456"  # 密钥脱敏
    assert "sk-" in p["env"]["AGENTD_ZHIPU_API_KEY"]


def test_select_restarts_agent_and_keeps_session(server):
    c = _client(server)
    c.post("/api/models", {"data": {"active": None, "profiles": [
        {"id": "z1", "name": "智谱", "env": {"AGENTD_LLM_BACKEND": "zhipu"}},
    ]}})
    r = c.post("/api/model/select", {"id": "z1"})
    assert r["ok"], r
    assert r["session"] == "sess_original123"        # 旧会话保住了
    assert server._fake.restarts == 1                # 子进程重启过
    assert server._fake.env == {"AGENTD_LLM_BACKEND": "zhipu"}
    assert M.load_models()["active"] == "z1"


def test_select_unknown_profile_404(server):
    c = _client(server)
    with pytest.raises(Exception):
        c.post("/api/model/select", {"id": "ghost"})


def test_select_default_clears_active(server):
    c = _client(server)
    c.post("/api/models", {"data": {"active": None, "profiles": [
        {"id": "z1", "name": "智谱", "env": {"AGENTD_LLM_BACKEND": "zhipu"}},
    ]}})
    c.post("/api/model/select", {"id": "z1"})
    assert M.load_models()["active"] == "z1"
    r = c.post("/api/model/select", {"id": ""})
    assert r["ok"]
    assert M.load_models()["active"] is None
    assert server._fake.env is None
