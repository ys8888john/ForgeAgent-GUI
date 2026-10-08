"""本地记忆（notes）MCP server —— 自带的常用工具，随仓库分发、零外部依赖。

为什么自研而不用某个第三方 memory server：
    常见的 memory server 都是 npm 系（本机 npx 被沙箱拦死），或需要额外服务；
    而这里只需要"模型能跨轮记住一些事实"这一件事 —— 一个 JSON 文件持久化的
    笔记存取就够用，且可以离线全链路测试（agentd 的 McpHub 真连真调）。

与 agentd 原生工具的分界（刻意不重叠）：
    read_file/write_file 是"给用户改文件"的；这里是"给模型记事"的结构化
    事实记忆库（标签 + 检索 + 全文），存储固定在 ~/.agentd/gui/memory.json
    （可用 --file 覆盖），不碰用户的工作目录。

运行（跟 echo_mcp_server.py 同一套姿势，都要能 import mcp —— 标准安装姿势下
GUI 与 agentd 同 venv，直接可用）：

    python examples/memory_mcp_server.py           # 等 stdio 输入
    python examples/memory_mcp_server.py --file x.json

工具（模型视角）：
    save_note(text, tags=[])    存一条记忆，返回 id
    search_notes(query, tag?)   按（不区分大小写）子串/标签检索
    list_notes(tag?)            列出全部（新→旧）
    delete_note(note_id)        删除指定 id
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from mcp.server.mcpserver import MCPServer

server = MCPServer("memory")

# 运行时状态：一个文件一把锁的简化模型 —— MCP stdio server 本身就是单进程
# 顺序处理请求，不需要跨进程加锁；并发编辑由"写临时文件 + 原子替换"兜底。
_state: dict = {"file": None}


def _file() -> Path:
    return Path(_state["file"])


def _load() -> dict:
    p = _file()
    if not p.is_file():
        return {"notes": []}  # 没有历史是常态，不是错误
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # 一行坏数据不拖垮整个记忆库：损坏文件改名保留，从头开始
        try:
            p.replace(p.with_suffix(".corrupt"))
        except OSError:
            pass
        return {"notes": []}
    return data if isinstance(data, dict) and isinstance(data.get("notes"), list) else {"notes": []}


def _save(data: dict) -> None:
    tmp = _file().with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(_file())  # 原子替换：断电也不留半个文件


def _next_id(data: dict) -> str:
    return f"n{int(time.time() * 1000) % 10**10:010d}-{len(data['notes']) + 1}"


@server.tool()
def save_note(text: str, tags: list[str] | None = None) -> str:
    """把一条值得跨对话记住的事实存进记忆库（人物偏好、项目约定、结论等）。"""
    text = (text or "").strip()
    if not text:
        return "[错误] 记忆内容不能为空"
    data = _load()
    note = {
        "id": _next_id(data),
        "text": text[:2000],
        "tags": [str(t).strip() for t in (tags or []) if str(t).strip()],
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    data["notes"].append(note)
    _save(data)
    return f"已保存记忆 {note['id']}（现有 {len(data['notes'])} 条）"


@server.tool()
def search_notes(query: str = "", tag: str = "") -> str:
    """按关键词（子串，不区分大小写）或标签检索记忆；query 与 tag 都给时取交集。"""
    data = _load()
    q = (query or "").strip().lower()
    tg = (tag or "").strip()
    hits = [
        n
        for n in data["notes"]
        if (not q or q in str(n.get("text", "")).lower())
        and (not tg or tg in [str(t) for t in n.get("tags", [])])
    ]
    if not hits:
        return "没有匹配的记忆"
    hits = list(reversed(hits))  # 新→旧
    lines = [f"[{n['id']}] ({','.join(n.get('tags') or []) or '无标签'}) {n['text']}" for n in hits]
    return f"命中 {len(lines)} 条：\n" + "\n".join(lines[:50])


@server.tool()
def list_notes(tag: str = "") -> str:
    """列出记忆库里的全部条目（可按标签过滤），新→旧。"""
    return _list_impl(tag)


def _list_impl(tag: str = "") -> str:
    data = _load()
    notes = [n for n in data["notes"] if not tag or tag in [str(t) for t in n.get("tags", [])]]
    if not notes:
        return "记忆库为空"
    lines = [f"[{n['id']}] ({','.join(n.get('tags') or []) or '无标签'}) {n['text']}" for n in reversed(notes)]
    return f"共 {len(lines)} 条：\n" + "\n".join(lines[:50])


@server.tool()
def delete_note(note_id: str) -> str:
    """按 id 删除一条记忆。"""
    data = _load()
    kept = [n for n in data["notes"] if n.get("id") != note_id]
    if len(kept) == len(data["notes"]):
        return f"[错误] 没有这条记忆：{note_id}"
    data["notes"] = kept
    _save(data)
    return f"已删除 {note_id}（剩 {len(kept)} 条）"


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="ForgeAgent 本地记忆 MCP server")
    ap.add_argument(
        "--file",
        default=str(Path.home() / ".agentd" / "gui" / "memory.json"),
        help="存储文件（默认 ~/.agentd/gui/memory.json）",
    )
    args = ap.parse_args()
    _state["file"] = args.file
    server.run("stdio")
