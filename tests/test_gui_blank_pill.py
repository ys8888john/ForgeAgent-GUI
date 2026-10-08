"""空白气泡（空白药丸）的回归测试。

背景（2026-10-08 用户截图）：Thinking 块和工具卡片之间夹着几颗**空的**药丸形
气泡。根因：模型（GLM 实测）在"思考段结束 → 工具调用"之间会发一个只有 "\n"
的正文 delta；GUI 的 delta 分支照常按角色新建气泡，于是产出一个只有空白、
渲染成空药丸的节点。

修复：delta 分支里，纯空白的 delta 在**没有同角色气泡可落**时直接跳过；
已有同角色气泡时照常 append（词句间的空格换行是正文的一部分，不能丢）。

和 test_gui_pending_bubble.py 同一套思路：DOM 逻辑没有可单测的纯函数切面，
真跑 JS 要拖一整个 DOM 桩，成本远大于收益；真机行为由 CDP 冒烟覆盖，这里
只做"守卫写没写对位置、有没有被顺手删掉"的静态校对。
"""

from __future__ import annotations

import re
from pathlib import Path

_INDEX = Path(__file__).resolve().parent.parent / "forgeagent" / "gui" / "assets" / "index.html"


def _index_text() -> str:
    assert _INDEX.is_file(), f"找不到界面资源：{_INDEX}"
    return _INDEX.read_text(encoding="utf-8")


def _function_body(name: str) -> str:
    """抠出 `[async] function name(...) { ... }` 的函数体（按大括号配对，别用贪婪正则）。"""
    src = _index_text()
    m = re.search(rf"\n  (?:async )?function {re.escape(name)}\(", src)
    assert m, f"index.html 里找不到 function {name} —— 改名了？"
    i = src.index("{", m.end() - 1)
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[i : j + 1]
    raise AssertionError(f"function {name} 的大括号没闭合")


_GUARD = 'if (!(ev.text || "").trim() && (!current || current.dataset.role !== want))'


def _delta_branch() -> str:
    """handle() 里 delta 分支的文本（到下一个 ev.type 判断为止）。"""
    handler = _function_body("handle")
    i = handler.index('ev.type === "delta"')
    rest = handler[i + len('ev.type === "delta"') :]
    nxt = rest.find('ev.type ===')
    return rest if nxt < 0 else rest[:nxt]


def test_blank_delta_does_not_open_a_bubble():
    """纯空白 delta 且没有同角色气泡可落时，必须直接跳过，不能新建气泡。"""
    assert _GUARD in _delta_branch(), (
        "delta 分支的空白守卫丢了 —— GLM 在思考段和工具调用之间发的 '\\n' 正文"
        " delta 又会建出空白药丸"
    )


def test_guard_runs_before_bubble_creation():
    """守卫必须挡在 newBubble 之前 —— 建完再判断等于没防。"""
    seg = _delta_branch()
    assert seg.index(_GUARD) < seg.index("newBubble("), (
        "空白守卫在 newBubble 之后才出现 —— 气泡已经建出来了，守卫形同虚设"
    )


def test_guard_does_not_swallow_whitespace_appends():
    """守卫的条件必须带 `(!current || current.dataset.role !== want)`。

    只有这样，已有同角色气泡时的空白 append 才能照常走 appendText ——
    把条件简化成 `!(ev.text || "").trim()` 会把正文里的空格换行整个吞掉。
    """
    assert "(!current || current.dataset.role !== want)" in _GUARD
    # append 这条路还在：delta 分支末尾必须仍是 appendText
    assert "appendText(current, ev.text)" in _delta_branch(), (
        "delta 分支的 appendText 没了 —— 正文没法流式渲染了"
    )
