"""models.py / 模型切换链路的测试。

核心要锁的行为：
1. profile 只能带 AGENTD_* 前缀的键（防「任意环境变量注入」）；
2. 编辑留空不丢旧 key（merge 语义）；
3. GET 层脱敏不回写真值 —— 掩码串绝不能覆盖真实 API Key；
4. 切换模型 = 把选中 profile 的 env 写进热配置文件（hotenv.json），
   运行中 agentd 下一轮即用，**不重启子进程**、会话历史保留。
"""

from __future__ import annotations

import json

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


def test_select_hot_swaps_without_restart(server):
    c = _client(server)
    c.post("/api/models", {"data": {"active": None, "profiles": [
        {"id": "z1", "name": "智谱", "env": {"AGENTD_LLM_BACKEND": "zhipu",
                                              "AGENTD_ZHIPU_API_KEY": "sk-x"}},
    ]}})
    r = c.post("/api/model/select", {"id": "z1"})
    assert r["ok"], r
    assert r["hot_swapped"] is True
    # 关键：热切换不重启 agentd —— 子进程没动过，会话历史自然保留
    assert server._fake.restarts == 0
    assert server._fake.session_id == "sess_original123"
    assert server._fake.env is None          # 不再把 env 注入子进程，改走热文件
    # 选中的 env 写进了热配置文件，agentd 下一轮读它即用
    hot = M.hotenv_path()
    assert hot.exists()
    data = json.loads(hot.read_text(encoding="utf-8"))
    assert data["AGENTD_LLM_BACKEND"] == "zhipu"
    assert data["AGENTD_ZHIPU_API_KEY"] == "sk-x"
    assert M.load_models()["active"] == "z1"


def test_select_switches_model_within_profile(server):
    """同一 provider 下填了多个模型：切到其中一个，MODEL 同时写进热文件与 models.json。

    落回 models.json 是为了让「当前模型」可被读取侧看到：前端刷新（读 models.json）
    要拿它高亮，重启 GUI 时启动逻辑也从这里取 —— 只写热文件的话，刷新会显示回默认
    模型、重启还会被默认模型覆盖。
    """
    c = _client(server)
    c.post("/api/models", {"data": {"active": None, "profiles": [
        {"id": "z1", "name": "智谱", "models": ["glm-4.5-air", "glm-4.6"],
         "env": {"AGENTD_LLM_BACKEND": "zhipu", "AGENTD_ZHIPU_API_KEY": "sk-x",
                 "AGENTD_ZHIPU_MODEL": "glm-4.5-air"}},
    ]}})
    r = c.post("/api/model/select", {"id": "z1", "model": "glm-4.6"})
    assert r["ok"], r
    assert r["model"] == "glm-4.6"
    data = json.loads(M.hotenv_path().read_text(encoding="utf-8"))
    assert data["AGENTD_ZHIPU_MODEL"] == "glm-4.6"
    assert data["AGENTD_ZHIPU_API_KEY"] == "sk-x"   # 其余字段沿用
    # 选择同时落了盘：别的字段与多模型清单都不受影响
    p = M.load_models()["profiles"][0]
    assert p["env"]["AGENTD_ZHIPU_MODEL"] == "glm-4.6"
    assert p["env"]["AGENTD_ZHIPU_API_KEY"] == "sk-x"
    assert p["models"] == ["glm-4.5-air", "glm-4.6"]


def test_selected_model_shows_up_in_listing(server):
    """切完模型后 GET /api/models 要反映新的当前模型 —— 前端二级列表高亮、header
    下拉回填都读它，读不到就会「切了却还显示默认模型」。"""
    c = _client(server)
    c.post("/api/models", {"data": {"active": "z1", "profiles": [
        {"id": "z1", "name": "智谱", "models": ["glm-4.5-air", "glm-4.6"],
         "env": {"AGENTD_LLM_BACKEND": "zhipu", "AGENTD_ZHIPU_MODEL": "glm-4.5-air"}},
    ]}})
    c.post("/api/model/select", {"id": "z1", "model": "glm-4.6"})
    p = c.get("/api/models")["profiles"][0]
    assert p["env"]["AGENTD_ZHIPU_MODEL"] == "glm-4.6"
    assert p["models"] == ["glm-4.5-air", "glm-4.6"]


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
    assert M.hotenv_path().exists()           # 切到 z1：热文件已写
    r = c.post("/api/model/select", {"id": ""})
    assert r["ok"]
    assert M.load_models()["active"] is None
    assert not M.hotenv_path().exists()       # 切回默认：热文件删掉，agentd 回落到自己 .env
    assert server._fake.env is None


def test_server_startup_applies_active_profile(tmp_path):
    """用户报的 bug（2026-09-22）：配置了 provider、重启 GUI 后又回到默认
    ollama —— models.json 的 active 明明还在，但启动时没人把它交给 agentd。

    新行为：UiServer 启动时把 active profile 的 env 写进热配置文件（hotenv.json），
    并设 AGENTD_HOTENV 让 agentd 每轮都读它；重启 GUI 后 provider 自动生效，
    且**不再把 env 直接注入子进程**（避免和「真实环境变量最优先」的语义打架）。
    """
    M.save_models({"active": "z1", "profiles": [
        _profile("z1", env={"AGENTD_LLM_BACKEND": "zhipu",
                            "AGENTD_ZHIPU_API_KEY": "sk-live-key-123"}),
    ]})

    srv = UiServer()  # 真 Bridge + 真 AcpClient（不 start，不起进程）
    try:
        client = srv.bridge._client
        assert client._env is None  # env 不再注入子进程
        hot = M.hotenv_path()
        assert hot.exists()         # active profile 的 env 已写进热文件
        data = json.loads(hot.read_text(encoding="utf-8"))
        assert data == {"AGENTD_LLM_BACKEND": "zhipu",
                        "AGENTD_ZHIPU_API_KEY": "sk-live-key-123"}
    finally:
        srv.stop()


def test_server_startup_without_active_uses_no_env(tmp_path):
    """没配过 provider（active=None）：热文件应保持不存在，行为与从前一致。"""
    M.save_models({"active": None, "profiles": [_profile("z1")]})

    srv = UiServer()
    try:
        assert srv.bridge._client._env is None
        assert not M.hotenv_path().exists()
    finally:
        srv.stop()


# ---- per-profile 的 token 预算 ----
#
# 为什么必须 per-profile：本机 qwen3.5 常见窗口 4k~40k，云端 GLM / MiMo 是 128k，
# 差三倍以上。做成全局一个值，必然「本地那个溢出、云端那个浪费」，
# 而溢出的症状是模型悄悄把开头忘了 —— 用户只会觉得"它变笨了"。

def test_token_limits_survive_a_profile_roundtrip():
    env = {
        "AGENTD_LLM_BACKEND": "ollama",
        "AGENTD_MAX_CONTEXT_TOKENS": "8192",
        "AGENTD_MAX_OUTPUT_TOKENS": "1024",
    }
    M.save_models({"active": "p1", "profiles": [_profile("p1", env=env)]})

    assert M.env_for("p1") == env
    assert M.load_models()["profiles"][0]["env"]["AGENTD_MAX_CONTEXT_TOKENS"] == "8192"


def test_token_limits_are_not_masked_like_api_keys(server):
    """GET 出来给前端时，这两个值必须**原样**（不像 API Key 那样脱敏）。

    理由：编辑表单要回填它们。回填一个 "81…92" 的掩码串，用户一保存就把掩码
    写成了真配置 —— 症状是"预算设了但没生效"，而且数字变成 NaN 也不报错。
    """
    M.save_models({"active": "p1", "profiles": [
        _profile("p1", env={
            "AGENTD_MAX_CONTEXT_TOKENS": "8192",
            "AGENTD_MAX_OUTPUT_TOKENS": "1024",
            "AGENTD_ZHIPU_API_KEY": "abcdefghijklmnop",
        }),
    ]})

    listed = _client(server).get("/api/models")["profiles"][0]["env"]
    assert listed["AGENTD_MAX_CONTEXT_TOKENS"] == "8192"
    assert listed["AGENTD_MAX_OUTPUT_TOKENS"] == "1024"
    # KEY 语义的键照旧脱敏 —— 同一个出口必须能区分"秘密"和"只是个数字"
    assert "…" in listed["AGENTD_ZHIPU_API_KEY"]
    assert "abcdefghijklmnop" not in listed["AGENTD_ZHIPU_API_KEY"]


def test_token_limits_reach_the_hotenv_file(server):
    """切 profile 时预算要跟到热文件里 —— 否则「切了模型但预算没跟着变」，
    而且症状是随模型而变的（本地模型溢出、云端模型答一半被截），极难联系到
    "预算没切换"这一点。
    """
    M.save_models({"active": "p1", "profiles": [
        _profile("p1", env={
            "AGENTD_LLM_BACKEND": "openai_compat",
            "AGENTD_MAX_CONTEXT_TOKENS": "4096",
            "AGENTD_MAX_OUTPUT_TOKENS": "512",
        }),
    ]})

    r = _client(server).post("/api/model/select", {"id": "p1"})
    assert r["ok"] is True

    data = json.loads(M.hotenv_path().read_text(encoding="utf-8"))
    assert data["AGENTD_MAX_CONTEXT_TOKENS"] == "4096"
    assert data["AGENTD_MAX_OUTPUT_TOKENS"] == "512"


def test_zero_disables_the_limit_rather_than_keeping_the_old_one():
    """明确的 0 = 不限，必须**覆盖**旧值，不能被 merge 语义当成"留空不改"。

    这条最容易被写坏：UI 的「留空=不改」靠的是"键缺失"，而 0 是个真值，
    它必须穿透。
    """
    M.save_models({"active": "p1", "profiles": [
        _profile("p1", env={"AGENTD_MAX_CONTEXT_TOKENS": "8192"}),
    ]})
    M.save_models({"active": "p1", "profiles": [
        _profile("p1", env={"AGENTD_MAX_CONTEXT_TOKENS": "0"}),
    ]})

    assert M.env_for("p1")["AGENTD_MAX_CONTEXT_TOKENS"] == "0"
