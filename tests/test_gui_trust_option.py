"""审批弹窗「完全信任」按钮的回归测试（源码结构断言）。

为什么值得单测：这个按钮会把审批**永久**关掉（写 ~/.agentd/trust_all.json），
是弹窗里后果最重的一个选项。三条红线：

1. 必须按 **option_id** 认，不能按 kind —— 它的 kind 是 allow_always，和
   「本会话总是允许」同值，按 kind 分就会被 primary 蓝色吞掉，用户看不出
   这两个按钮的后果差着一个数量级；
2. 必须有 tooltip 讲清撤销方式（删标记文件）—— 点之前就有反悔路径，
   比点完再弹一个"你确定吗"更尊重用户；
3. CSS 必须用主题令牌（--ok/--ok-bg/--ok-border）—— 写死颜色的话深色主题
   下这按钮会亮得刺眼，而它恰恰是用户最容易盯住的按钮。

跑真 JS 要拖一整个 DOM 桩，成本远大于收益；真机行为由跨仓 e2e 覆盖。
"""

from __future__ import annotations

import re
from pathlib import Path

_INDEX = Path(__file__).resolve().parent.parent / "forgeagent" / "gui" / "assets" / "index.html"


def _index_text() -> str:
    assert _INDEX.is_file(), f"找不到界面资源：{_INDEX}"
    return _INDEX.read_text(encoding="utf-8")


def test_trust_button_is_matched_by_option_id_not_kind():
    """trust_all 的 kind 与 allow_session 同为 allow_always，只能按 id 分。"""
    src = _index_text()
    assert 'o.id === "trust_all"' in src, (
        "弹窗渲染没有按 option_id 认 trust_all —— 它会被当成普通"
        "「本会话总是允许」渲染成同一个蓝色按钮，用户看不出后果差别"
    )


def test_trust_button_carries_undo_hint():
    """tooltip 必须写明撤销方式（删标记文件），路径要指对。"""
    src = _index_text()
    assert "trust_all.json" in src, (
        "trust 按钮的 tooltip 没提撤销路径 —— 永久关审批却不告诉用户怎么反悔"
    )
    # tooltip 要挂在按钮上（b.title），而不是只出现在注释里
    m = re.search(r'o\.id === "trust_all"\)\s*\{(.{0,400}?)\}', src, re.S)
    assert m, "找不到 trust_all 分支"
    assert "b.title" in m.group(1), "trust 分支没有设置按钮 tooltip"


def test_trust_button_uses_theme_tokens():
    """CSS 用主题令牌，不写死颜色（深色主题也要可读）。"""
    src = _index_text()
    m = re.search(r"\.perm-actions button\.trust\s*\{([^}]*)\}", src)
    assert m, "没有 .perm-actions button.trust 的 CSS 规则"
    css = m.group(1)
    assert "var(--ok)" in css and "var(--ok-bg)" in css, (
        f"trust 按钮没用主题令牌：{css.strip()!r} —— 写死颜色会在深色主题下刺眼"
    )
