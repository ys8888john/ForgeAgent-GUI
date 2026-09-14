"""读 agentd 的 SQLite 会话库，给 GUI 做「会话侧栏 / 续聊」。

只做**只读**查询，绝不写：agentd 自己才是写方（它持有主连接，每次 handle()
都把新消息 append 进库，见 kernel/handle.py）。这里另开一个连接读同一份文件，
靠 WAL 并发读互不阻塞，也避免 GUI 进程一写把 agentd 的库弄脏。

为什么不让 GUI 直接 import agentd 的 SqliteSessionStore：
    两个独立进程各持一个连接本来就是 WAL 的设计意图，GUI 只读不碰写最干净；
    而且 GUI 只关心"库在哪、怎么读"，路径判断自己复刻一份（和 boot.py 完全一致），
    不跟 agentd 的包强耦合 —— 哪天 agentd 换存储后端，GUI 这层也不用跟着改。

库位置（和 agentd/boot.py 的 load_settings 对齐）：
    AGENTD_DB_PATH 环境变量优先，否则 ~/.agentd/sessions.db。
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

# 默认库位置，复刻 agentd/kernel/store.py:default_db_path() —— 放用户目录而不是
# CWD，否则"从 VSCode 启动看不到从终端聊过的记录"这种灵异现象就来了。
DEFAULT_DB_PATH = Path.home() / ".agentd" / "sessions.db"


def db_path() -> Path:
    """会话库路径：环境变量优先，否则 ~/.agentd/sessions.db。"""
    return Path(os.environ.get("AGENTD_DB_PATH") or DEFAULT_DB_PATH)


def _open(path: Path) -> sqlite3.Connection | None:
    """开一个只读连接；库文件不存在或打不开就返回 None（让上层当「空」处理）。

    优先用 mode=ro（uri 形式），完全杜绝误写；万一 ro 在某些环境起不来
    （比如 WAL 的 -shm 暂不可写），退回普通打开——只读查询不会真的写盘。
    """
    if not path.is_file():
        return None
    for spec in (f"file:{path.as_posix()}?mode=ro", str(path)):
        try:
            return sqlite3.connect(spec, uri="?" in spec)
        except sqlite3.Error:
            continue
    return None


class SessionsSource:
    """agentd 会话库的只读视图。UiServer 持有一个，测试可注入假的。

    三个方法对应前端三件事：
        list_meta   侧栏列表（每条会话的标题 / 时间 / 条数 / 末条预览）
        get_history 点开某个会话看完整历史（续聊前先把它画出来）
        exists      续聊前确认这个 id 真在库里（防前端传个瞎编的 id）
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = db_path() if path is None else Path(path)

    def list_meta(self) -> list[dict]:
        """已有会话的摘要，按最近活动降序。给侧栏用。"""
        conn = _open(self.path)
        if conn is None:
            return []
        try:
            rows = conn.execute(
                """
                SELECT s.id, s.created_at,
                       COUNT(m.seq) AS msg_count,
                       (SELECT content FROM messages
                          WHERE session_id = s.id ORDER BY seq ASC LIMIT 1) AS first_msg,
                       (SELECT content FROM messages
                          WHERE session_id = s.id ORDER BY seq DESC LIMIT 1) AS last_msg
                FROM sessions s
                LEFT JOIN messages m ON m.session_id = s.id
                GROUP BY s.id
                ORDER BY s.created_at DESC
                """
            ).fetchall()
        finally:
            conn.close()

        out: list[dict] = []
        for sid, created, count, first, last in rows:
            title = (first or "").strip().split("\n", 1)[0] or "（空会话）"
            preview = (last or "").strip().replace("\r\n", "\n").split("\n", 1)[0]
            out.append(
                {
                    "id": sid,
                    "created_at": created,
                    "msg_count": count,
                    "title": title[:60],
                    "preview": preview[:80],
                }
            )
        return out

    def get_history(self, session_id: str) -> list[dict] | None:
        """某会话的完整历史；库里没有这个 id 返回 None（区分"有但空"）。

        返回的是 [{role, name, content}]，前端直接拿来渲染气泡。
        name 可能为 None（只有 tool 角色用），原样带过去，前端按需处理。
        """
        conn = _open(self.path)
        if conn is None:
            return None
        try:
            rows = conn.execute(
                "SELECT role, name, content FROM messages "
                "WHERE session_id = ? ORDER BY seq",
                (session_id,),
            ).fetchall()
        finally:
            conn.close()

        if not rows:
            # 一条消息都没有：要区分"会话根本不存在"和"存在但还没消息"
            conn2 = _open(self.path)
            if conn2 is not None:
                try:
                    exists = conn2.execute(
                        "SELECT 1 FROM sessions WHERE id = ?", (session_id,)
                    ).fetchone() is not None
                finally:
                    conn2.close()
            else:
                exists = False
            return [] if exists else None

        return [
            {"role": r, "name": n, "content": (c or "")} for r, n, c in rows
        ]

    def exists(self, session_id: str) -> bool:
        conn = _open(self.path)
        if conn is None:
            return False
        try:
            return conn.execute(
                "SELECT 1 FROM sessions WHERE id = ?", (session_id,)
            ).fetchone() is not None
        finally:
            conn.close()
