"""examples/memory_mcp_server.py 的全链路测试。

用 agentd 的 McpHub **真起子进程、真连、真调** —— echo/memory 这类
"真子进程"测试从来不在 CI 主链（README 有约），本机验证用：

    cd ForgeAgent-GUI && <Agentd venv 的 python> -m pytest tests/test_memory_mcp_server.py

解释器需要装着 mcp SDK（agentd 的 venv 就有）；仓库不在（纯 GUI CI）
整组 skip。存储文件钉在 tmp_path（--file），绝不碰用户的 ~/.agentd/gui。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("agentd", reason="需要 agentd 包（McpHub 在 agentd.kernel.mcp）")

from agentd.kernel.mcp import McpHub  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
AGENTD_VENV_PY = ROOT.parent / "Agentd" / ".venv" / "bin" / "python"

pytestmark = pytest.mark.skipif(
    not AGENTD_VENV_PY.is_file(),
    reason=f"需要 Agentd venv 的解释器才能起 MCP server 子进程：{AGENTD_VENV_PY}",
)


def _config(db_file: Path) -> dict:
    return {
        "name": "memory",
        "command": str(AGENTD_VENV_PY),
        "args": [str(ROOT / "examples" / "memory_mcp_server.py"), "--file", str(db_file)],
    }


async def test_memory_tools_roundtrip(tmp_path):
    db = tmp_path / "memory.json"
    async with McpHub([_config(db)], cwd=str(tmp_path)) as hub:
        tools = {item["function"]["name"] for item in hub.tool_schema()}
        assert tools == {
            "memory__save_note",
            "memory__search_notes",
            "memory__list_notes",
            "memory__delete_note",
        }

        out = await hub.call("memory__save_note", json.dumps({"text": "用户偏好回答中文", "tags": ["偏好"]}))
        assert "已保存记忆" in out

        out = await hub.call("memory__search_notes", json.dumps({"query": "中文"}))
        assert "用户偏好回答中文" in out

        out = await hub.call("memory__list_notes", json.dumps({}))
        assert "共 1 条" in out

    # 持久化：重开一个 hub（对齐"新一轮会话"）记忆还在
    async with McpHub([_config(db)], cwd=str(tmp_path)) as hub:
        out = await hub.call("memory__search_notes", json.dumps({"query": "偏好"}))
        assert "用户偏好回答中文" in out


async def test_memory_delete_and_empty(tmp_path):
    db = tmp_path / "memory.json"
    async with McpHub([_config(db)], cwd=str(tmp_path)) as hub:
        out = await hub.call("memory__save_note", json.dumps({"text": "项目用 ACP 对话"}))
        assert "已保存" in out
        listing = await hub.call("memory__list_notes", json.dumps({}))
        nid = listing.split("[", 1)[1].split("]", 1)[0]

        out = await hub.call("memory__delete_note", json.dumps({"note_id": nid}))
        assert "已删除" in out

        out = await hub.call("memory__search_notes", json.dumps({"query": "ACP"}))
        assert out == "没有匹配的记忆"

        out = await hub.call("memory__delete_note", json.dumps({"note_id": "n-missing"}))
        assert out.startswith("[错误]")  # 工具层报错（非异常）：模型能读到并绕路


async def test_memory_empty_input_rejected(tmp_path):
    async with McpHub([_config(tmp_path / "memory.json")], cwd=str(tmp_path)) as hub:
        out = await hub.call("memory__save_note", json.dumps({"text": "   "}))
        assert out.startswith("[错误]")
