"""自定义模型配置：profile = 一组注入 agentd 子进程的 AGENTD_* 环境变量。

为什么做成「环境变量注入」而不是在 GUI 里重新实现一遍后端逻辑：
    agentd 已经把「怎么连一个模型」收口到 boot.py 的环境变量上（ollama /
    openai_compat / mimo / zhipu / fake / script）。GUI 要做的只是帮用户
    管理这几组变量、切换时带着它们重启 agentd 子进程 —— 协议、审批、
    工具循环全都不用动。将来 agentd 加新后端，GUI 自动就支持。

存储：~/.forgeagent/models.json（跟 mcp.json 同目录，同样的读写约定）：

    {
      "active": "zhipu-glm45air",
      "profiles": [
        {
          "id": "zhipu-glm45air",
          "name": "智谱 GLM-4.5-air",
          "env": {
            "AGENTD_LLM_BACKEND": "zhipu",
            "AGENTD_ZHIPU_API_KEY": "xxx.yyy",
            "AGENTD_ZHIPU_MODEL": "glm-4.5-air"
          }
        }
      ]
    }

安全注意：文件里会有明文 API key —— 与 ~/.forgeagent/mcp.json、Agentd/.env
同级同罪，都属「用户家目录里的本机私密配置」，不进任何仓库。
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

_FORGE_DIR = ".forgeagent"
_MODELS_FILE = "models.json"

# 环境变量名的白名单：profile 只允许带 AGENTD_ 前缀的键。
# 理由：这份文件是「模型配置」，不是「给子进程塞任意环境变量」的通用机制 ——
# PATH / LD_PRELOAD 这类键混进来就是另一个性质的问题了。
_ALLOWED_KEY = re.compile(r"^AGENTD_[A-Z0-9_]+$")

# id 只用小写字母数字和连字符，避免路径/JSON 里出怪字符
_ID_OK = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")

MAX_PROFILES = 20
_MAX_ENV_PER_PROFILE = 16


def models_path() -> Path:
    return Path.home() / _FORGE_DIR / _MODELS_FILE


def hotenv_path() -> Path:
    """GUI 写入 agentd 的「热配置文件」路径。

    agentd 的 current_settings() 每轮都读这个文件（路径由启动时的 AGENTD_HOTENV
    环境变量告诉它，GUI 负责设好）。切模型时 GUI 把选中的 profile 环境变量写进来，
    运行中的 agentd 下一轮即生效 —— 不用重启子进程。
    """
    return Path.home() / _FORGE_DIR / "hotenv.json"


def write_hotenv(env: dict) -> None:
    """把一组 AGENTD_* 环境变量写进热配置文件（GUI 切模型时不重启 agentd 的主通道）。

    只收白名单前缀的键；原子替换（先写 .tmp 再 os.replace），避免 agentd 读到半截 JSON。
    内容与 models.json 同级同权（可能含明文 API Key），不进仓库。
    """
    p = hotenv_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    clean = {str(k): str(v) for k, v in (env or {}).items() if _ALLOWED_KEY.match(str(k))}
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(clean, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(tmp, p)


def clear_hotenv() -> None:
    """没有激活的 profile（默认配置）时删掉热文件，让 agentd 回落到自己的 .env。"""
    try:
        hotenv_path().unlink()
    except FileNotFoundError:
        pass


def _read_raw() -> dict:
    p = models_path()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def load_models() -> dict:
    """读配置（容错：文件坏了返回空骨架）。返回 {"active": str|None, "profiles": [dict]}。"""
    raw = _read_raw()
    profiles = [p for p in raw.get("profiles") or [] if isinstance(p, dict) and p.get("id")]
    active = raw.get("active")
    if active and not any(p.get("id") == active for p in profiles):
        active = None
    return {"active": active, "profiles": profiles}


def save_models(data: dict) -> dict:
    """整份写回。带原子替换 + 上限校验，返回规范化后的数据。

    编辑语义（merge）：id 已存在的 profile，新 env 里**缺失**的键从旧值继承 ——
    「表单留空 = 不改」靠这个成立（API Key 输入框留空时，旧 key 不能丢）。
    同时挡住脱敏串：GET 列表的值是头3尾3的掩码，前端整份回传时那些掩码会
    混进来，若恰好等于旧值的脱敏形式则保留旧真值 —— 真 key 不会含 "…"。
    """
    profiles = [p for p in (data.get("profiles") or []) if isinstance(p, dict) and p.get("id")]
    if len(profiles) > MAX_PROFILES:
        raise ValueError(f"profile 太多（上限 {MAX_PROFILES}）")
    old = {p.get("id"): (p.get("env") or {}) for p in load_models()["profiles"]}

    merged: dict[str, dict] = {}
    for p in profiles:
        pid = p.get("id")
        env = dict(p.get("env") or {})
        if pid in old:
            for k, oldv in old[pid].items():
                newv = env.get(k)
                if newv is None or (isinstance(oldv, str) and isinstance(newv, str)
                                    and "…" in newv and newv == f"{oldv[:3]}…{oldv[-3:]}"):
                    env[k] = oldv  # 缺失（留空不改）或原样传回的脱敏掩码 → 保留旧值
        p = {**p, "env": {k: v for k, v in env.items() if _ALLOWED_KEY.match(str(k))}}
        # models：一个 provider 下可填多个模型（逗号分隔），供会话内随时切换。
        # 以表单提交为准：显式带 models（即使是空列表）就按它来；表单没带
        # （单模型表单）就不保留旧列表，避免「编辑时删掉多余模型却没生效」。
        new_models = p.get("models")
        if new_models is not None:
            ms = [str(m).strip() for m in new_models if str(m).strip()]
            if ms:
                seen: set[str] = set()
                p["models"] = [m for m in ms if not (m in seen or seen.add(m))]
            else:
                p.pop("models", None)
        merged[pid] = p  # id 重复：后者覆盖前者（列表顺序即优先级）

    active = data.get("active")
    if active and not any(p.get("id") == active for p in merged.values()):
        active = None
    out = {"active": active, "profiles": list(merged.values())}
    p = models_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(tmp, p)
    return out


def sanitize_profile(p: dict) -> dict:
    """规范化一个 profile：剥掉不允许的键、给空 id 起名、限制长度。

    id 冲突时带时间戳后缀 —— 保存方（UI）允许同名存在，读取侧靠 id 区分。
    允许带一个可选的 ``models`` 列表（一个 provider 下多个模型名），
    供会话内随时切换；纯元数据，不受 AGENTD_* 白名单约束。
    """
    pid = str(p.get("id") or "").strip()
    if not _ID_OK.match(pid):
        pid = f"model-{int(time.time()) % 100000}"
    name = str(p.get("name") or "").strip() or pid
    env_raw = p.get("env") if isinstance(p.get("env"), dict) else {}
    env = {}
    for k, v in list(env_raw.items())[:_MAX_ENV_PER_PROFILE]:
        k = str(k).strip()
        if not _ALLOWED_KEY.match(k):
            continue
        v = "" if v is None else str(v)
        env[k] = v
    out = {"id": pid, "name": name[:40], "env": env}
    models_raw = p.get("models")
    if isinstance(models_raw, (list, tuple)):
        seen: set[str] = set()
        models = [str(m).strip() for m in models_raw if str(m).strip()]
        models = [m for m in models if not (m in seen or seen.add(m))]
        if models:
            out["models"] = models
    return out


def env_for(profile_id: str | None) -> dict[str, str]:
    """取某个 profile 的环境变量字典；id 为空或不存在时返回 {}（= 用 agentd 自己的 .env/默认）。"""
    if not profile_id:
        return {}
    data = load_models()
    for p in data["profiles"]:
        if p.get("id") == profile_id:
            out = {}
            for k, v in (p.get("env") or {}).items():
                if _ALLOWED_KEY.match(k):
                    out[k] = str(v)
            return out
    return {}
