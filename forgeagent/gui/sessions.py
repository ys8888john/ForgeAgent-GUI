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

import json
import os
import sqlite3
from pathlib import Path

# 默认库位置，复刻 agentd/kernel/store.py:default_db_path() —— 放用户目录而不是
# CWD，否则"从 VSCode 启动看不到从终端聊过的记录"这种灵异现象就来了。
DEFAULT_DB_PATH = Path.home() / ".agentd" / "sessions.db"

# 只给模型看、不参与 UI 回放的角色。
# role="tool" 的模型价值是"上一轮工具返回了什么"，而它恰好就是旁边那张工具卡
# 片的 output —— 画出来等于同一份内容显示两遍。（详见 get_history 的 docstring）
_MODEL_ONLY_ROLES = frozenset({"tool", "system"})


def _has_tool_calls(payload: str | None) -> bool:
    """这条 assistant 是"举手要调工具"吗 —— 是的话 UI 不该给它一个气泡。

    payload 解析不动就当普通 assistant：宁可多画一个气泡，也别把用户的回答吞了。
    """
    if not payload:
        return False
    try:
        parsed = json.loads(payload)
    except ValueError:
        return False
    return bool(isinstance(parsed, dict) and parsed.get("tool_calls"))


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
        # "删除"的落地方式：**侧栏隐藏**（hidden_sessions.json，与库同目录）。
        # agentd 的会话库写方是它自己（这里只读不碰），ACP 也没有 session/delete；
        # 对齐主流产品的做法是把会话移出列表。真要清数据用 agentd 的
        # scripts/sessions.py。不提供 unhide 界面，删 hidden 文件即可恢复显示。
        self.hidden_path = self.path.parent / "hidden_sessions.json"
        # "重命名"同理：不改库（标题列来自首条消息），本地记一份别名，
        # list_meta 里 alias 优先。空别名 = 恢复自动标题。
        self.aliases_path = self.path.parent / "session_aliases.json"

    def _load_hidden(self) -> set[str]:
        try:
            data = json.loads(self.hidden_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return set()
        return {sid for sid in data if isinstance(sid, str)} if isinstance(data, list) else set()

    def hide(self, session_id: str) -> None:
        """把会话移出版侧栏（幂等写回 hidden 名单）。"""
        hidden = self._load_hidden()
        hidden.add(session_id)
        self.hidden_path.parent.mkdir(parents=True, exist_ok=True)
        self.hidden_path.write_text(
            json.dumps(sorted(hidden), ensure_ascii=False), encoding="utf-8"
        )

    def _load_aliases(self) -> dict[str, str]:
        try:
            data = json.loads(self.aliases_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {str(k): str(v) for k, v in data.items() if str(v).strip()}

    def rename(self, session_id: str, title: str) -> str:
        """给会话起个人名（持久化到本地名单）；空标题 = 恢复自动标题。"""
        title = (title or "").strip()[:80]
        aliases = self._load_aliases()
        if title:
            aliases[session_id] = title
        else:
            aliases.pop(session_id, None)
        self.aliases_path.parent.mkdir(parents=True, exist_ok=True)
        self.aliases_path.write_text(
            json.dumps(aliases, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return title

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
        hidden = self._load_hidden()
        aliases = self._load_aliases()
        for sid, created, count, first, last in rows:
            if sid in hidden:
                continue  # 已被用户"删除"（隐藏）的会话不出现在侧栏
            aliased = aliases.get(sid)
            title = (first or "").strip().split("\n", 1)[0] or "（空会话）"
            if aliased:
                title = aliased  # 用户改过的名字优先
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

        role="tool_record" 是 agentd 侧的工具卡片记录（payload.tool_record 是
        整个调用：call_id/title/kind/status/output）。冗余列 content 只存了
        title，卡片要完整重放就得把 payload 解开带出去 —— 前端按
        renderToolCard 渲染。解析失败当空卡片处理，一行坏数据不能拖垮整个回放。

        另外两种**要跳过**的行：

        - ``assistant`` 且 payload.tool_calls 非空 —— "举手要调工具"那一行。
          它本身通常只有一句前言（甚至空串），紧接着的工具卡片已经把这次动作
          表达完了；画出来就是一串空白气泡。
        - ``role="tool"`` —— 工具的真实返回值，同时是紧邻那张卡片的 output。
          这一行存在的意义是**让模型下一轮还能看到自己读过什么**（跨轮上下文），
          跟 UI 回放无关，画出来就把同一份输出显示了两遍。

        两者都是给模型的、不是给人看的。
        """
        conn = _open(self.path)
        if conn is None:
            return None
        try:
            rows = conn.execute(
                "SELECT role, name, content, payload FROM messages "
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

        out: list[dict] = []
        for r, n, c, p in rows:
            if r in _MODEL_ONLY_ROLES:
                # 给模型看的行（见 docstring）：UI 回放一律跳过
                continue
            item = {"role": r, "name": n, "content": (c or "")}
            if r == "tool_record":
                try:
                    item["tool_record"] = (json.loads(p) or {}).get("tool_record") or {}
                except ValueError:
                    item["tool_record"] = {}
            elif r == "assistant" and _has_tool_calls(p):
                # 工具卡已经代表了这次动作，别再画一个多半是空的气泡
                continue
            out.append(item)
        return out

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
