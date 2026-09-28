"""examples/sqlite_mcp_server.py 的全链路测试（McpHub 真连真调，skipif 同 memory）。

覆盖两层安全模型：
- 默认只读：run_query 只收 SELECT；run_statement 在没有 --allow-write 时给
  "[错误]"（工具层报错，不抛异常 —— 模型能读到并绕路）；
- --allow-write：写语句生效并持久化；SELECT 仍被拒（让 run_query 干它的事）。

存储文件钉在 tmp_path，不碰用户的 ~/.forgeagent。
"""

from __future__ import annotations

import json
import sqlite3
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

_SCRIPT = str(ROOT / "examples" / "sqlite_mcp_server.py")


def _seed(db: Path) -> None:
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE contacts (id INTEGER PRIMARY KEY, name TEXT, phone TEXT)")
    conn.executemany(
        "INSERT INTO contacts (name, phone) VALUES (?, ?)", [("Alice", "111"), ("Bob", "222")]
    )
    conn.commit()
    conn.close()


def _config(db: Path, allow_write: bool = False) -> dict:
    args = [_SCRIPT, "--file", str(db)]
    if allow_write:
        args.append("--allow-write")
    return {"name": "sqlite", "command": str(AGENTD_VENV_PY), "args": args}


async def test_readonly_mode_full_flow(tmp_path):
    db = tmp_path / "data.sqlite"
    _seed(db)
    async with McpHub([_config(db)], cwd=str(tmp_path)) as hub:
        tools = {item["function"]["name"] for item in hub.tool_schema()}
        assert tools == {
            "sqlite__list_tables",
            "sqlite__describe_table",
            "sqlite__run_query",
            "sqlite__run_statement",
        }

        out = await hub.call("sqlite__list_tables", json.dumps({}))
        assert "[table] contacts" in out

        out = await hub.call("sqlite__describe_table", json.dumps({"table": "contacts"}))
        assert "name" in out and "phone" in out

        out = await hub.call(
            "sqlite__run_query", json.dumps({"sql": "SELECT name FROM contacts ORDER BY id"})
        )
        assert "Alice" in out and "Bob" in out

        # 非 SELECT 被 run_query 拒，并指路写工具
        out = await hub.call("sqlite__run_query", json.dumps({"sql": "UPDATE contacts SET name='X'"}))
        assert out.startswith("[错误]") and "run_statement" in out

        # 写语句在只读模式下被拒（人话，不抛异常）
        out = await hub.call(
            "sqlite__run_statement", json.dumps({"sql": "INSERT INTO contacts (name) VALUES ('X')"})
        )
        assert out.startswith("[错误]") and "只读" in out


async def test_allow_write_mode(tmp_path):
    db = tmp_path / "data.sqlite"
    _seed(db)
    async with McpHub([_config(db, allow_write=True)], cwd=str(tmp_path)) as hub:
        out = await hub.call(
            "sqlite__run_statement",
            json.dumps({"sql": "INSERT INTO contacts (name, phone) VALUES ('Carol', '333')"}),
        )
        assert "已执行" in out

        out = await hub.call("sqlite__run_query", json.dumps({"sql": "SELECT COUNT(*) AS n FROM contacts"}))
        assert "3" in out

        out = await hub.call("sqlite__run_statement", json.dumps({"sql": "SELECT 1"}))
        assert out.startswith("[错误]") and "run_query" in out

    # 持久化：新进程（等价新一轮会话）读同一个文件
    async with McpHub([_config(db)], cwd=str(tmp_path)) as hub:
        out = await hub.call(
            "sqlite__run_query", json.dumps({"sql": "SELECT name FROM contacts WHERE name='Carol'"})
        )
        assert "Carol" in out


async def test_file_missing_is_clean_error(tmp_path):
    async with McpHub([_config(tmp_path / "nope.sqlite")], cwd=str(tmp_path)) as hub:
        out = await hub.call("sqlite__list_tables", json.dumps({}))
        assert out.startswith("[错误]") and "不存在" in out
