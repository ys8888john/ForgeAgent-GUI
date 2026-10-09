"""活动标签（原「等待模型响应」占位气泡）的回归测试。

背景（2026-10-08 用户反馈）：按下发送到模型吐第一个字之间，屏幕上除了顶栏那行
小字**没有任何变化**；模型在排队或长思考时看着就像卡死。补了一个 .bubble.pending
占位气泡。

2026-10-09 升级（用户再反馈，附 make_xlsx 生成时的截图）：正文流完之后模型开始
攒工具调用参数（几百行航班数据全在 JSON 参数里）—— 这段既没有正文 delta、也没
有 ToolCallStart，旧逻辑在第一个 delta 就把占位收掉，屏幕又回到"看着像卡死"。
所以占位升级成**整轮常驻的活动标签**：delta / tool / permission 事件改文案跟随
（思考中… / 正在生成… / 正在执行 xxx… / 等待模型响应 / 等待你审批），只有 done
才收掉。

这里守住三根线：
  1. 标签必须在轮内每个事件出口被**续写**（改文案），不能被中途收掉；
  2. done 必须收掉 —— 漏了的话会话末尾留一颗永远在跳的标签，比没有提示更糟；
  3. 历史重放（renderHistory → upsertTool）绝不能产出标签 —— 旧会话结尾会被
     塞一颗假标签（2026-10-09 复查修掉的实锤 bug）。

之所以用源码结构断言而不是跑 JS：这一整套 DOM 逻辑没有可单测的纯函数切面，
真跑 JS 要拖一整个 DOM 桩进来，成本远大于收益。真机行为由 CDP 冒烟脚本覆盖，
这里只做"出口有没有写全"的静态校对。
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


def _handle_branch(branch_key: str) -> str:
    """handle() 里指定 ev.type 分支的文本（到下一个 ev.type 判断为止）。"""
    handler = _function_body("handle")
    marker = f'ev.type === "{branch_key}"'
    i = handler.index(marker)
    rest = handler[i + len(marker) :]
    nxt = rest.find("ev.type ===")
    return rest if nxt < 0 else rest[:nxt]


def test_pending_bubble_is_created_by_send():
    """按下发送（user 事件）就要有活动标签，否则那段沉默期是无反馈的。"""
    assert 'showPending("等待模型响应")' in _index_text(), (
        "user 事件里没有 showPending —— 等待模型响应的提示又没了"
    )


def test_tag_is_rewritten_by_every_in_turn_outlet():
    """轮内三个出口（delta / tool / permission）必须续写标签而不是收掉。

    漏一个出口，对应的静默期（攒工具参数、排队、等审批）里屏幕就没有动静了。
    """
    for key in ("delta", "tool", "permission"):
        assert "showPending(" in _handle_branch(key), (
            f'{key} 分支没有 showPending —— 标签被收掉，那段静默期又"像卡死"'
        )
        assert "clearPending()" not in _handle_branch(key), (
            f"{key} 分支里 clearPending —— 标签中途消失，静默期没有活着的标记"
        )


def test_tag_text_follows_context():
    """文案要跟状态走：思考中 / 正在生成 / 正在执行 <工具> / 等待审批。"""
    delta = _handle_branch("delta")
    assert '"思考中…"' in delta and '"正在生成…"' in delta, (
        "delta 分支没按角色区分文案 —— 思考和生成看着一个样"
    )
    tool = _handle_branch("tool")
    assert '"正在执行 "' in tool and "ev.title" in tool, (
        "tool 分支运行中没有点名工具 —— 用户不知道此刻是谁在跑"
    )
    assert '"等待模型响应"' in tool, "工具终态后没有转回「等待模型响应」"
    assert '"等待你审批"' in _handle_branch("permission"), (
        "permission 分支没有提示审批 —— 弹窗没注意到时像死机"
    )


def test_done_is_the_only_outlet_that_clears():
    """done 必须收掉标签；upsertTool（历史重放也走它）绝不能碰 showPending。"""
    assert "clearPending()" in _handle_branch("done"), (
        "done 分支没有 clearPending —— 会话末尾留一颗永远在跳的标签"
    )
    body = _function_body("upsertTool")
    assert "showPending" not in body and "clearPending" not in body, (
        "upsertTool 里动了活动标签 —— renderHistory 重放历史工具记录时会给"
        "旧会话结尾塞一颗假标签（2026-10-09 修掉的实锤 bug 不能回归）"
    )


def test_showPending_keeps_the_tag_at_the_end_of_the_flow():
    """标签后面出现新内容时要挪回消息流末尾，别卡在会话中间。"""
    body = _function_body("showPending")
    assert "appendChild(pendingBubble)" in body, (
        "showPending 没有把标签挪回末尾 —— 新气泡/卡片追加后标签悬在中间"
    )


def test_pending_bubble_is_reused_not_duplicated():
    """同一轮里工具跑完还要再等下一轮模型 —— 标签必须就地复用，不能每次新建。"""
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
