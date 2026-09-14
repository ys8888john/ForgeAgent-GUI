"""MCP 配置读取：把用户的 mcp.json 转成 ACP session/new 要的 mcpServers 结构。

配置格式沿用主流客户端（Claude Desktop / WorkBuddy 等）的习惯：

    {
      "mcpServers": {
        "echo":   { "command": "python", "args": ["server.py"], "env": {"K": "V"} },
        "remote": { "url": "https://example.com/mcp", "headers": {"Authorization": "..."} }
      }
    }

ACP 侧要的是**数组**，且 env/headers 是 `[{name, value}]` 列表；这里负责转换。
文件不存在/解析失败一律返回空列表（没配 MCP 是常态，不该报错）。

**必须补齐必填字段**（踩过的坑）：ACP SDK 的 `McpServerStdio` 把 `args` / `env`
都声明成必填，`HttpMcpServer` 还要 `type`。字段缺失时 pydantic 的 union 校验
**不会报错，而是静默把整个 mcpServers 变成空列表** —— 表现就是"配了 MCP 但
agentd 说没接到 server"，极难自查。所以这里宁可补空值也不省略。
"""

from __future__ import annotations

import json
import os
from pathlib import Path


def config_path() -> Path:
    """配置文件位置：FORGEAGENT_MCP_CONFIG 优先，否则 ~/.forgeagent/mcp.json。"""
    override = os.environ.get("FORGEAGENT_MCP_CONFIG")
    if override:
        return Path(override)
    return Path.home() / ".forgeagent" / "mcp.json"


def _as_pairs(raw: object) -> list[dict]:
    if isinstance(raw, dict):
        return [{"name": str(k), "value": str(v)} for k, v in raw.items()]
    if isinstance(raw, list):
        out = []
        for item in raw:
            if isinstance(item, dict) and item.get("name"):
                out.append({"name": str(item["name"]), "value": str(item.get("value", ""))})
        return out
    return []


def load_mcp_servers(path: str | Path | None = None) -> list[dict]:
    """读出 ACP 格式的 mcpServers 列表；没有/出错就返回空。"""
    p = Path(path) if path is not None else config_path()
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(raw, dict):
        return []
    servers = raw.get("mcpServers")
    if not isinstance(servers, dict):
        return []

    out: list[dict] = []
    for name, cfg in servers.items():
        if not isinstance(cfg, dict):
            continue
        entry: dict = {"name": str(name)}
        if cfg.get("url"):
            entry["url"] = str(cfg["url"])
            # 必填：headers（空也要给）+ type（http/sse）
            entry["headers"] = _as_pairs(cfg.get("headers"))
            entry["type"] = "sse" if str(cfg.get("type", "")).lower() == "sse" else "http"
        else:
            if not cfg.get("command"):
                continue  # 既没 url 也没 command，这条配置没意义
            entry["command"] = str(cfg["command"])
            # 必填：args / env（空也要给，否则 ACP 校验会静默丢掉整份配置）
            entry["args"] = [str(a) for a in cfg.get("args") or []]
            entry["env"] = _as_pairs(cfg.get("env"))
            if cfg.get("cwd"):
                entry["cwd"] = str(cfg["cwd"])
        out.append(entry)
    return out


def describe(path: str | Path | None = None) -> dict:
    """给界面看的一句话摘要。"""
    p = Path(path) if path is not None else config_path()
    servers = load_mcp_servers(p)
    return {
        "path": str(p),
        "exists": p.is_file(),
        "count": len(servers),
        "servers": [s.get("name") for s in servers],
    }
