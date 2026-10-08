"""本地 SQLite MCP server —— 零依赖（stdlib sqlite3 + 本机 mcp SDK），随仓库分发。

安全模型（数据库工具和文件工具不一样，默认收紧）：
1. **默认只读**：连接用 `mode=ro` 打开，任何写操作直接失败——模型想改数据
   不可能绕过。要放开写，显式用 `--allow-write` 启动（并接受 agentd 侧的
   审批弹窗，工具没声明只读）。
2. **单语句**：`execute()` 天然拒绝多语句，`;` 串多个 DROP 的注入形状不会通过。
3. **限额**：查询最多返回 200 行、输出截断 32KB —— 一个大表不该把上下文灌爆。
4. 与 agentd 附批注声明 `readOnlyHint`：只读三件套（list_tables / describe_table /
   run_query）在 agentd 侧映射为 `read` kind，不弹审批；写工具不声明 → `execute`
   → 审批。

运行（同 echo/memory 姿势，需要本机 python 有 mcp SDK —— 标准安装姿势下
GUI 与 agentd 同 venv）：

    python examples/sqlite_mcp_server.py --file path/to/db.sqlite
                                                 # 不传 --file 默认 ~/.agentd/gui/sqlite.db

工具：
    list_tables()                    表 + 视图清单（含 sqlite 内部表排除）
    describe_table(table)            列名/类型/可空/主键（PRAGMA table_info）
    run_query(sql)                   只读查询（SELECT/WITH），markdown 表格回显
    run_statement(sql)               写语句（INSERT/UPDATE/DELETE/CREATE…），
                                     仅 --allow-write 时可用；返回影响行数
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

server = MCPServer("sqlite")

_READONLY = ToolAnnotations(read_only_hint=True)

_state: dict = {"file": None, "allow_write": False}

_MAX_ROWS = 200
_MAX_CHARS = 32_000
_READ_ONLY_MSG = "[错误] 当前 server 是只读模式：重启并在启动参数里加 --allow-write 才能写（会走审批）"


def _path() -> Path:
    return Path(_state["file"])


def _connect_readonly() -> sqlite3.Connection:
    p = _path()
    if not p.is_file():
        raise FileNotFoundError(f"数据库文件不存在：{p}")
    # mode=ro 连到不存在的文件会直接 OperationalError —— 先给一句人话
    return sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)


class _FileError(Exception):
    pass


def _open_ro() -> sqlite3.Connection:
    """list/describe/query 的统一入口：缺文件给 [错误] 文本，不抛异常。

    SDK 会把工具内抛出的异常包成协议错误（调用方收到挂起的异常），
    破坏"错误以 [错误] 文本回给模型"的仓库约定。
    """
    try:
        return _connect_readonly()
    except FileNotFoundError as exc:
        raise _FileError(str(exc)) from exc


def _connect_write() -> sqlite3.Connection:
    if not _state["allow_write"]:
        raise PermissionError(_READ_ONLY_MSG)
    return sqlite3.connect(_state["file"])


def _first_keyword(sql: str) -> str:
    return (sql or "").lstrip().split(None, 1)[0].strip("(;").upper() if sql.strip() else ""


def _clip(text: str) -> str:
    return text if len(text) <= _MAX_CHARS else f"{text[:_MAX_CHARS]}\n…（输出截断，共 {len(text)} 字符）"


def _rows_to_markdown(cur: sqlite3.Cursor, rows: list) -> str:
    cols = [d[0] for d in cur.description] if cur.description else []
    if not cols:
        return "（无结果列）"
    def cell(v):
        s = "" if v is None else str(v)
        return s.replace("|", "\\|").replace("\n", "␤")
    head = "| " + " | ".join(cell(c) for c in cols) + " |"
    sep = "|" + "|".join(["---"] * len(cols)) + "|"
    body = ["| " + " | ".join(cell(v) for v in row) + " |" for row in rows]
    note = "" if len(rows) < _MAX_ROWS else f"\n（已截断：只显示前 {_MAX_ROWS} 行）"
    return "\n".join([head, sep, *body]) + note


@server.tool(annotations=_READONLY)
def list_tables() -> str:
    """列出数据库里的全部表和视图（不含 sqlite_ 内部表）。"""
    try:
        conn = _open_ro()
    except _FileError as exc:
        return f"[错误] {exc}"
    try:
        rows = conn.execute(
            "SELECT name, type FROM sqlite_master "
            "WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%' "
            "ORDER BY type, name"
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return "数据库为空（没有表和视图）"
    return "\n".join(f"[{typ}] {name}" for name, typ in rows)


@server.tool(annotations=_READONLY)
def describe_table(table: str) -> str:
    """查看某张表的列定义：参数 table 是表名（不含库名）。"""
    name = (table or "").strip()
    if not name or not name.replace("_", "").isalnum():
        return "[错误] 表名不合法（只要纯标识符）"
    try:
        conn = _open_ro()
    except _FileError as exc:
        return f"[错误] {exc}"
    try:
        info = conn.execute(f"PRAGMA table_info({name})").fetchall()
    except sqlite3.Error as exc:
        return f"[错误] {type(exc).__name__}: {exc}"
    finally:
        conn.close()
    if not info:
        return f"[错误] 表不存在（或没有列）：{name}"
    lines = ["cid | name | type | notnull | default | pk"]
    for cid, cname, ctype, notnull, dflt, is_pk in info:
        lines.append(f"{cid} | {cname} | {ctype or ''} | {bool(notnull)} | {dflt if dflt is not None else ''} | {bool(is_pk)}")
    return "\n".join(lines)


@server.tool(annotations=_READONLY)
def run_query(sql: str) -> str:
    """执行一条只读 SQL（SELECT / WITH … SELECT），返回 markdown 表格。

    单条语句；最多返回 200 行。写操作会被模式拒绝。
    """
    kw = _first_keyword(sql)
    if kw not in ("SELECT", "WITH"):
        return f"[错误] run_query 只收 SELECT / WITH 查询（收到 {kw or '空'}）。写语句请用 run_statement（需 --allow-write）。"
    try:
        conn = _open_ro()
    except _FileError as exc:
        return f"[错误] {exc}"
    try:
        cur = conn.execute(sql)
        rows = cur.fetchmany(_MAX_ROWS + 1)
        if len(rows) > _MAX_ROWS:
            rows = rows[:_MAX_ROWS]
        return _clip(_rows_to_markdown(cur, rows))
    except sqlite3.Error as exc:
        return f"[错误] {type(exc).__name__}: {exc}"
    finally:
        conn.close()


@server.tool()
def run_statement(sql: str) -> str:
    """执行一条写语句（INSERT / UPDATE / DELETE / CREATE / DROP…），返回影响行数。

    仅当 server 以 --allow-write 启动时可用；SELECT 请走 run_query。
    会先经过 agentd 的审批弹窗（本工具未声明只读）。
    """
    kw = _first_keyword(sql)
    if not kw:
        return "[错误] 空 SQL"
    if kw in ("SELECT", "WITH", "PRAGMA"):
        return f"[错误] run_statement 不收 {kw}：SELECT/WITH 走 run_query，PRAGMA 说明用途后另行讨论"
    try:
        conn = _connect_write()
    except PermissionError as exc:
        return str(exc)
    try:
        cur = conn.execute(sql)
        conn.commit()
        changed = cur.rowcount
        return f"已执行（影响 {changed if changed >= 0 else '未知'} 行）"
    except sqlite3.Error as exc:
        conn.rollback()
        return f"[错误] {type(exc).__name__}: {exc}"
    finally:
        conn.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="ForgeAgent 本地 SQLite MCP server")
    ap.add_argument(
        "--file",
        default=str(Path.home() / ".agentd" / "gui" / "sqlite.db"),
        help="数据库文件（默认 ~/.agentd/gui/sqlite.db；编辑 mcp.json 可指向任意库）",
    )
    ap.add_argument("--allow-write", action="store_true", help="允许写语句（默认只读；写工具仍会走审批）")
    args = ap.parse_args()
    _state["file"] = args.file
    _state["allow_write"] = bool(args.allow_write)
    server.run("stdio")
