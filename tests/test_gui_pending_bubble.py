"""「等待模型响应」占位气泡的回归测试。

背景（2026-10-08 用户反馈）：按下发送到模型吐第一个字之间，屏幕上除了顶栏那行
小字**没有任何变化**；模型在排队或长思考时看着就像卡死。补了一个 .bubble.pending
占位气泡。

这里守住的是**最容易松掉的那根线**：占位气泡必须被每个"已经开始产出内容"的出口
收掉。漏一个出口的后果比没有提示更糟 —— 会话里会永远留着一个跳动的"等待模型响应"，
而顶栏同时写着"已连接"，用户会以为它一直在等。

之所以用源码结构断言而不是跑 JS：这一整套 DOM 逻辑没有可单测的纯函数切面，
真跑 JS 要拖一整个 DOM 桩进来，成本远大于收益。真机行为由 CDP 冒烟脚本覆盖
（tools 外的 scripts 目录），这里只做"出口有没有写全"的静态校对。
"""

from __future__ import annotations

import re
from pathlib import Path

_INDEX = Path(__file__).resolve().parent.parent / "forgeagent" / "gui" / "assets" / "index.html"


def _index_text() -> str:
    assert _INDEX.is_file(), f"找不到界面资源：{_INDEX}"
    return _INDEX.read_text(encoding="utf-8")


def _function_body(name: str) -> str:
    """抠出 `[async] function name(...) { ... }` 的函数体（按大括号配对，别用贪婪正则）。

    `async` 前缀要一起容忍 —— newChat 就是 async 的，只匹配 `function ` 会漏。
    """
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


def test_pending_bubble_is_created_by_send():
    """按下发送（user 事件）就要有占位气泡，否则那段沉默期是无反馈的。"""
    assert 'showPending("等待模型响应")' in _index_text(), (
        "user 事件里没有 showPending —— 等待模型响应的提示又没了"
    )


def test_pending_is_cleared_by_every_content_outlet():
    """三个出口都必须收掉占位：delta（出字）、tool（工具卡片）、done（空回复/取消）。"""
    # delta / tool 分支在 handle() 里，done 也在；逐个按分支文本定位
    handler = _function_body("handle")
    for branch in ('ev.type === "delta"', 'ev.type === "tool"', 'ev.type === "done"'):
        i = handler.index(branch)
        # 取该分支开头的一小段（到下一个分支判断为止），检查里面有没有 clearPending
        rest = handler[i + len(branch) :]
        nxt = rest.find('ev.type ===')
        seg = rest if nxt < 0 else rest[:nxt]
        assert "clearPending()" in seg, (
            f'{branch} 分支没有 clearPending() —— 会让占位气泡残留成"永远在等"'
        )


def test_pending_bubble_is_reused_not_duplicated():
    """同一轮里工具跑完还要再等下一轮模型 —— 占位必须就地复用，不能每次新建。"""
    body = _function_body("showPending")
    assert "if (!pendingBubble)" in body, (
        "showPending 没有复用已有节点 —— 多步 tool 轮次会在会话里堆一串等待气泡"
    )


def test_clearing_the_message_list_also_drops_the_reference():
    """清空 #messages 的两处（历史重放 / 新会话）必须把引用置空。

    否则 pendingBubble 指向一个已脱离文档的节点：下一次 showPending 会往一个
    看不见的旧节点上写文案，屏幕上什么都不出现。
    """
    src = _index_text()
    for fn in ("renderHistory", "newChat"):
        body = _function_body(fn)
        assert 'messages.innerHTML = ""' in body, f"{fn} 里没找到清空 #messages"
        assert "pendingBubble = null" in body, (
            f"{fn} 清空了 #messages，却没把 pendingBubble 置空 —— 引用会悬空"
        )


def test_pending_bubble_has_a_dot_animation():
    """静态文案会被忽略，得有个"在动"的信号（顶栏可能被视线跳过）。"""
    src = _index_text()
    assert ".bubble.pending" in src and ".pending-dots" in src, "占位气泡的样式丢了"
    assert "@keyframes pendingBounce" in src, "三个点的跳动动画丢了"
    # 错峰：同一个 keyframes 靠 delay 走出"依次跳"
    assert ".pending-dots i:nth-child(2)" in src and ".pending-dots i:nth-child(3)" in src


def test_pending_bubble_is_not_counted_as_an_assistant_bubble():
    """domStats 的计数会喂给冒烟脚本判断"回答渲染出来没有"，占位不能混进去。

    它按 class 精确分类（user / assistant / thought / error），占位气泡的 class 是
    "bubble pending"，不进任何一类；tool 另有单独计数。这里盯的是"别哪天顺手加了
    个 else 把 pending 归进 assistant"。
    """
    body = _function_body("domStats")
    assert 'c.contains("assistant")' in body, "domStats 的分类逻辑改写了？"
    assert "pending" not in body, (
        "domStats 里出现了 pending —— 占位气泡会被算成回答，冒烟脚本的计数就假了"
    )
