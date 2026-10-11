"""图片生成：配置 + 调用兼容 OpenAI 的图像接口（images/generations）。

为什么是「OpenAI 兼容接口」：OpenAI / 智谱 CogView / MiMo / 通义万相 /
多数国内图像模型都暴露同一套 ``POST {base_url}/images/generations``，请求与
响应形状一致，GUI 不用为每家再写一套后端。

配置怎么复用现有约定：
  - API Key 与 base_url 复用 ``models.json`` 里 **openai_compat 类** profile
    的环境变量（``AGENTD_OPENAI_API_KEY`` / ``AGENTD_OPENAI_BASE_URL``）—— 和
    聊天模型走同一套凭据管理，用户不用重复填一遍。
  - 图像**专属**设置（选哪个 profile、用哪个图像模型、尺寸、张数）单独存
    ``imagegen.json``，不和聊天模型配置混在一起。

产物一律落盘到 agentd 的 tool_results 外置目录（已在产物白名单内），进程内
用不可猜的 id 登记，前端凭 id 经 ``/api/image/<id>`` 取图 —— 路径不出服务端。
"""

from __future__ import annotations

import base64
import json
import os
import re
import time
from pathlib import Path
from typing import Any

from .paths import GUI_DIR
from .models import load_models

_IMAGEGEN_FILE = "imagegen.json"

# 可选尺寸：OpenAI(DALL·E 3) 与多数兼容接口都认 1024x1024 / 1024x1792 /
# 1792x1024；512x512 给 DALL·E 2 / 小模型用；768x1344 等给 CogView 用。
# 这里给一份常见并集，具体哪个能用由 provider 决定（不支持的尺寸会返回错误，
# 前端会原样显示，不会崩）。
SIZES = ["1024x1024", "1024x1792", "1792x1024", "512x512", "768x1344", "864x1152"]
DEFAULT_SIZE = "1024x1024"
MIN_N, MAX_N = 1, 4

# 响应里拿不到 mime 时的兜底（OpenAI / CogView 默认都是 png）
_FALLBACK_MIME = "image/png"


def imagegen_path() -> Path:
    return GUI_DIR / _IMAGEGEN_FILE


def default_config() -> dict:
    return {"profile_id": None, "model": "", "size": DEFAULT_SIZE, "n": 1}


def _coerce_size(v: Any) -> str:
    v = str(v or "").strip()
    return v if v in SIZES else DEFAULT_SIZE


def _coerce_n(v: Any) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError):
        n = 1
    return max(MIN_N, min(MAX_N, n))


def load_config() -> dict:
    """读配置（容错：文件坏了/不存在返回默认骨架）。"""
    p = imagegen_path()
    raw: dict = {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    cfg = default_config()
    for k in ("profile_id", "model", "size"):
        if k in raw and isinstance(raw[k], str):
            cfg[k] = raw[k]
    cfg["size"] = _coerce_size(cfg.get("size"))
    cfg["n"] = _coerce_n(raw.get("n"))
    return cfg


def save_config(cfg: dict) -> dict:
    """整份写回（原子替换）。"""
    norm = default_config()
    if isinstance(cfg, dict):
        for k in ("profile_id", "model", "size"):
            if isinstance(cfg.get(k), str):
                norm[k] = cfg[k]
        norm["size"] = _coerce_size(norm.get("size"))
        norm["n"] = _coerce_n(cfg.get("n"))
    p = imagegen_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(norm, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(tmp, p)
    return norm


def list_image_profiles() -> list[dict]:
    """返回「能用来画图」的 profile 列表：有 AGENTD_OPENAI_API_KEY 的。

    这些 profile 的 base_url 与 key 可以直接喂给 images/generations 接口。
    Ollama / fake / script 这类没有图像接口的 profile 不列出来，避免用户选了
    却生成失败。
    """
    data = load_models()
    out = []
    for p in data.get("profiles") or []:
        env = p.get("env") or {}
        if not isinstance(env, dict):
            continue
        if not env.get("AGENTD_OPENAI_API_KEY"):
            continue
        out.append(
            {
                "id": p.get("id"),
                "name": p.get("name") or p.get("id"),
                "base_url": str(env.get("AGENTD_OPENAI_BASE_URL") or ""),
                "model": str(env.get("AGENTD_OPENAI_MODEL") or ""),
            }
        )
    return out


# ---------------------------------------------------------------------------
# HTTP 调用（抽成模块级函数，测试可 monkeypatch）
# ---------------------------------------------------------------------------

def _http_post_json(url: str, payload: dict, headers: dict, timeout: float) -> tuple[int, dict]:
    """POST JSON，返回 (status, 解析后的 body dict)。失败 status=0。"""
    import urllib.error
    import urllib.request

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={**headers, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = json.loads(r.read().decode("utf-8", errors="replace"))
            return int(r.status), body if isinstance(body, dict) else {}
    except urllib.error.HTTPError as exc:
        snippet = ""
        try:
            snippet = exc.read().decode("utf-8", errors="replace")[:500]
        except Exception:  # noqa: BLE001 - 读不到就空着
            pass
        return int(exc.code), {"error": snippet or exc.reason}
    except Exception as exc:  # noqa: BLE001 - 网络/超时/非 JSON 一视同仁
        return 0, {"error": f"{type(exc).__name__}: {exc}"}


def _http_get_bytes(url: str, headers: dict, timeout: float) -> tuple[bytes, str]:
    """GET 一段二进制（url 型返回需要回源取字节），返回 (bytes, content_type)。"""
    import urllib.request

    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read(), r.headers.get_content_type()
    except Exception as exc:  # noqa: BLE001 - 取不到字节就报错，由调用方处理
        raise RuntimeError(f"回源取图失败: {type(exc).__name__}: {exc}") from exc


def generate(
    prompt: str,
    profile_env: dict[str, str],
    *,
    model: str | None = None,
    size: str = DEFAULT_SIZE,
    n: int = 1,
    timeout: float = 120.0,
) -> dict:
    """调一次图像接口，返回 {ok: True, images:[{b64, mime}]} 或 {ok:False, error}。

    ``profile_env`` 是某个 openai_compat profile 的环境变量字典（含
    ``AGENTD_OPENAI_API_KEY`` / ``AGENTD_OPENAI_BASE_URL``）。``model`` 若为空
    则退回 profile 里的 ``AGENTD_OPENAI_MODEL``；再为空就报错——图像接口几乎都
    要求显式指定模型名。

    两种响应都支持：
      - ``b64_json``：直接用；
      - ``url``：回源 GET 取字节（带同样的鉴权头），避免某些 provider 的 URL
        需要代理/会过期、前端拿不到图。
    """
    prompt = (prompt or "").strip()
    if not prompt:
        return {"ok": False, "error": "提示词为空"}

    if not isinstance(profile_env, dict):
        profile_env = {}
    api_key = profile_env.get("AGENTD_OPENAI_API_KEY") or ""
    if not api_key:
        return {"ok": False, "error": "未配置图像 API Key（请在模型管理里加一个 openai_compat 类 provider）"}
    base_url = (profile_env.get("AGENTD_OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
    # model：参数优先，其次 profile 里的 OPENAI_MODEL
    model = (model or profile_env.get("AGENTD_OPENAI_MODEL") or "").strip()
    if not model:
        return {"ok": False, "error": "未指定图像模型（在生成面板里填一个，如 dall-e-3 / cogview-3-plus）"}

    n = max(MIN_N, min(MAX_N, int(n or 1)))
    size = _coerce_size(size)

    url = f"{base_url}/images/generations"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    }
    payload = {
        "prompt": prompt,
        "model": model,
        "n": n,
        "size": size,
        "response_format": "b64_json",
    }

    status, body = _http_post_json(url, payload, headers, timeout)
    if status == 0:
        return {"ok": False, "error": body.get("error") or "图像接口请求失败"}
    if status >= 400:
        err = body.get("error")
        detail = ""
        if isinstance(err, dict):
            detail = (err.get("message") or json.dumps(err, ensure_ascii=False))[:400]
        else:
            detail = str(err or "")[:400]
        return {"ok": False, "error": f"图像接口返回 {status}：{detail}"}

    items = body.get("data") if isinstance(body.get("data"), list) else None
    if not items:
        return {"ok": False, "error": "图像接口未返回任何图像数据"}

    images: list[dict] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        b64 = it.get("b64_json")
        mime = _FALLBACK_MIME
        if b64:
            images.append({"b64": str(b64), "mime": str(it.get("mime_type") or mime)})
            continue
        # url 型：回源取字节，前端凭 id 走我们的 /api/image/<id> 取，避免过期 URL
        u = it.get("url")
        if u:
            try:
                raw, ctype = _http_get_bytes(str(u), headers, timeout)
            except RuntimeError as exc:
                return {"ok": False, "error": str(exc)}
            images.append({"b64": base64.b64encode(raw).decode("ascii"), "mime": ctype or mime})

    if not images:
        return {"ok": False, "error": "图像接口返回的数据无法解析（既不是 b64_json 也不是 url）"}
    return {"ok": True, "images": images, "model": model}
