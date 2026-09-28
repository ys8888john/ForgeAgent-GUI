"""SessionsSource（会话库只读视图）测试。

不起 agentd 进程：按 agentd 的真实 schema 在临时文件里建库塞数据，
验 get_history 对 role="tool_record" 行的解析（payload 是权威数据）与容错 ——
一行坏数据不能拖垮整个会话回放，这是"只读视图"的硬约束。
"""

from __future__ import annotations

import json
import sqlite3

from forgeagent.gui.sessions import SessionsSource

# 与 agentd/kernel/store.py 的 _SCHEMA 同构（GUI 的只读依赖就是这份形状）
_SCHEMA = """
CREATE TABLE sessions (
    id         TEXT PRIMARY KEY,
    created_at REAL NOT NULL
);
CREATE TABLE messages (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role       TEXT NOT NULL,
    content    TEXT NOT NULL DEFAULT '',
    name       TEXT,
    payload    TEXT NOT NULL,
    created_at REAL NOT NULL
);
"""


def _seed(db, rows: list[tuple[str, str, str]]) -> None:
    conn = sqlite3.connect(db)
    conn.executescript(_SCHEMA)
    for role, content, payload in rows:
        conn.execute(
            "INSERT INTO messages (session_id, role, content, name, payload, created_at)"
            " VALUES ('s1', ?, ?, NULL, ?, 0)",
            (role, content, payload),
        )
    conn.commit()
    conn.close()


def test_get_history_exposes_tool_record_cards(tmp_path):
    db = tmp_path / "sessions.db"
    card = {
        "call_id": "call_1",
        "title": "read_file",
        "kind": "read",
        "status": "completed",
        "output": "[1] 你好",
    }
    _seed(
        db,
        [
            ("user", "帮我读文件", json.dumps({"role": "user", "content": "帮我读文件"})),
            (
                "tool_record",
                "read_file",
                json.dumps({"role": "tool_record", "content": "read_file", "tool_record": card}),
            ),
            ("assistant", "读到了", json.dumps({"role": "assistant", "content": "读到了"})),
        ],
    )

    hist = SessionsSource(db).get_history("s1")
    assert hist is not None
    assert [m["role"] for m in hist] == ["user", "tool_record", "assistant"]
    # tool_record 行带完整卡片，普通行不带这个键（旧行为不变）
    assert hist[1]["tool_record"] == card
    assert "tool_record" not in hist[0]
    assert "tool_record" not in hist[2]
    assert hist[2]["content"] == "读到了"


def test_broken_payload_degrades_to_empty_card_not_crash(tmp_path):
    db = tmp_path / "sessions.db"
    _seed(db, [("tool_record", "read_file", "{ 不是 json")])

    hist = SessionsSource(db).get_history("s1")
    assert hist[0]["tool_record"] == {}
    # 冗余列 content 还在：前端至少能拿 title 画一张能看的卡
    assert hist[0]["content"] == "read_file"


def test_missing_empty_session_returns_none(tmp_path):
    db = tmp_path / "sessions.db"
    conn = sqlite3.connect(db)
    conn.executescript(_SCHEMA)
    conn.commit()
    conn.close()

    assert SessionsSource(db).get_history("s_nope") is None


def test_hide_persists_and_filters_list_meta(tmp_path):
    """hide 后 list_meta 必须剔除该会话；新开实例（等价重启 GUI）仍保持剔除。

    隐藏不等于删除：get_history 照样能查到完整历史。
    """
    db = tmp_path / "sessions.db"
    conn = sqlite3.connect(db)
    conn.executescript(_SCHEMA)
    for sid in ("s1", "s2"):
        conn.execute("INSERT INTO sessions (id, created_at) VALUES (?, 0)", (sid,))
        conn.execute(
            "INSERT INTO messages (session_id, role, content, payload, created_at)"
            " VALUES ('s1', 'user', ?, ?, 0)",
            (f"hi {sid}", json.dumps({"session_id": sid, "role": "user", "content": f"hi {sid}"})),
        )
    conn.commit()
    conn.close()

    src = SessionsSource(db)
    assert [m["id"] for m in src.list_meta()] == ["s1", "s2"]

    src.hide("s1")
    assert [m["id"] for m in src.list_meta()] == ["s2"]

    # 持久化：新实例（对齐 GUI 重启）读同一份 hidden_sessions.json
    fresh = SessionsSource(db)
    assert [m["id"] for m in fresh.list_meta()] == ["s2"]
    # 隐藏 ≠ 删除：历史照常可读
    assert fresh.get_history("s1") is not None
