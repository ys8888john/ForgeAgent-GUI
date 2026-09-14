"""MCP 配置读取测试。

mcp_config 是纯函数模块（读文件 → 转 ACP 结构），不碰网络也不碰 agentd，
所以可以完全用临时文件测，不需要图形环境。
"""

from __future__ import annotations

import json

import pytest

from forgeagent.gui.mcp_config import config_path, describe, load_mcp_servers


def _write(tmp_path, obj) -> str:
    p = tmp_path / "mcp.json"
    p.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    return str(p)


def test_missing_file_returns_empty(tmp_path):
    """没配 MCP 是常态，不该报错。"""
    assert load_mcp_servers(tmp_path / "nope.json") == []


def test_malformed_json_returns_empty(tmp_path):
    p = tmp_path / "mcp.json"
    p.write_text("{ 这不是 json", encoding="utf-8")
    assert load_mcp_servers(p) == []


def test_stdio_server_is_converted(tmp_path):
    path = _write(
        tmp_path,
        {
            "mcpServers": {
                "echo": {
                    "command": "python",
                    "args": ["server.py", "--x"],
                    "env": {"TOKEN": "abc"},
                }
            }
        },
    )
    servers = load_mcp_servers(path)
    assert servers == [
        {
            "name": "echo",
            "command": "python",
            "args": ["server.py", "--x"],
            "env": [{"name": "TOKEN", "value": "abc"}],
        }
    ]


def test_stdio_without_args_and_env_still_fills_required_fields(tmp_path):
    """回归：ACP 的 McpServerStdio 把 args/env 声明成必填，字段缺失时
    pydantic union 会**静默**把整个 mcpServers 变成空列表。所以必须补空。"""
    path = _write(tmp_path, {"mcpServers": {"echo": {"command": "python"}}})
    servers = load_mcp_servers(path)
    assert servers == [{"name": "echo", "command": "python", "args": [], "env": []}]


def test_http_server_is_converted(tmp_path):
    path = _write(
        tmp_path,
        {
            "mcpServers": {
                "remote": {
                    "url": "https://example.com/mcp",
                    "headers": {"Authorization": "Bearer k"},
                }
            }
        },
    )
    servers = load_mcp_servers(path)
    assert servers == [
        {
            "name": "remote",
            "url": "https://example.com/mcp",
            "headers": [{"name": "Authorization", "value": "Bearer k"}],
            "type": "http",
        }
    ]


def test_http_without_headers_fills_required_fields(tmp_path):
    path = _write(tmp_path, {"mcpServers": {"r": {"url": "https://e.com/mcp"}}})
    servers = load_mcp_servers(path)
    assert servers == [
        {"name": "r", "url": "https://e.com/mcp", "headers": [], "type": "http"}
    ]


def test_sse_type_is_preserved(tmp_path):
    path = _write(
        tmp_path, {"mcpServers": {"r": {"url": "https://e.com/sse", "type": "sse"}}}
    )
    assert load_mcp_servers(path)[0]["type"] == "sse"


def test_entry_without_command_or_url_is_skipped(tmp_path):
    path = _write(tmp_path, {"mcpServers": {"bad": {"foo": 1}}})
    assert load_mcp_servers(path) == []


def test_converted_servers_pass_acp_schema(tmp_path):
    """最关键的一条：转出来的结构必须真的能通过 ACP 的 NewSessionRequest 校验，
    否则会在线上被静默丢成 []（见上面那条回归说明）。"""
    path = _write(
        tmp_path,
        {
            "mcpServers": {
                "echo": {"command": "python"},
                "remote": {"url": "https://example.com/mcp"},
            }
        },
    )
    payload = {"cwd": ".", "mcpServers": load_mcp_servers(path)}
    try:
        from acp.schema import NewSessionRequest
    except ImportError:  # 没装 acp SDK 时跳过（可选依赖）
        pytest.skip("未安装 acp SDK")
    req = NewSessionRequest.model_validate(payload)
    assert [s.name for s in req.mcp_servers] == ["echo", "remote"]


def test_env_as_list_of_pairs_is_accepted(tmp_path):
    """有的客户端把 env 写成 [{name,value}]，两种都要认。"""
    path = _write(
        tmp_path,
        {
            "mcpServers": {
                "s": {"command": "x", "env": [{"name": "A", "value": "1"}]}
            }
        },
    )
    servers = load_mcp_servers(path)
    assert servers[0]["env"] == [{"name": "A", "value": "1"}]


def test_missing_mcp_servers_key_returns_empty(tmp_path):
    assert load_mcp_servers(_write(tmp_path, {"other": 1})) == []


def test_describe_reports_path_and_names(tmp_path):
    path = _write(tmp_path, {"mcpServers": {"a": {"command": "x"}}})
    info = describe(path)
    assert info["exists"] is True
    assert info["count"] == 1
    assert info["servers"] == ["a"]


def test_config_path_env_override(monkeypatch, tmp_path):
    target = tmp_path / "custom.json"
    monkeypatch.setenv("FORGEAGENT_MCP_CONFIG", str(target))
    assert config_path() == target
