"""模式下拉 + 审批状态下拉的回归测试（源码结构断言）。

2026-10-17 改版：Agent/Plan/Ask 三档 pill 收成一个下拉，同一行右侧新增
审批状态下拉（对标 WorkBuddy 底部「允许完全访问 ∨」）。四条红线：

1. 模式下拉的触发器必须常显当前模式名 —— 当初做 pill 是怕模式藏起来被
   误当成 plan；收成下拉后这层保护只剩"触发器上有文字"，不能丢；
2. 审批下拉必须三个档位齐全且名字与 agentd 的 POLICIES 对得上 —— 值是
   原样传给 agentd ext_method 的，写错一个字母就是一次静默失败；
3. 「允许完全访问」必须染红（--danger 主题令牌）—— 这是后果最重的档位，
   和 WorkBuddy 一样要一眼扫出来；
4. 弹窗里点「完全信任」后必须刷新审批下拉 —— 否则同一屏上弹窗说"已信任"、
   下拉还挂「每次询问」，两份状态漂移。

跑真 JS 要拖一整个 DOM 桩，成本远大于收益；真机行为由跨仓 e2e 覆盖。
"""

from __future__ import annotations

import re
from pathlib import Path

_INDEX = Path(__file__).resolve().parent.parent / "forgeagent" / "gui" / "assets" / "index.html"


def _index_text() -> str:
    assert _INDEX.is_file(), f"找不到界面资源：{_INDEX}"
    return _INDEX.read_text(encoding="utf-8")


def test_mode_dropdown_trigger_shows_current_name():
    """模式下拉的触发器必须渲染当前模式名，不能只画图标/箭头。"""
    src = _index_text()
    assert "renderModeSwitches" in src, "模式下拉的渲染函数不见了"
    m = re.search(r'triggerHtml:\s*"<span>"\s*\+\s*escapeHtml\(cur\.name \|\| cur\.id\)', src)
    assert m, (
        "模式触发器没有渲染当前模式名 —— 三档 pill 收进下拉后，"
        "触发器上的文字是『现在是什么模式』的唯一线索"
    )


def test_composer_tools_row_has_mode_and_approval_on_both_pages():
    """对话页 / 欢迎页都要有「左模式、右审批」的工具条行。"""
    src = _index_text()
    assert src.count('class="composer-tools"') == 2, (
        "composer-tools 工具条应有对话页 + 欢迎页两份"
    )
    assert src.count('class="approval-switch"') == 2, (
        "approval-switch 容器应有对话页 + 欢迎页两份"
    )


def test_approval_dropdown_has_all_three_policies():
    """三个档位的 id 与名字必须和 agentd 的 POLICIES / 弹窗语义对得上。"""
    src = _index_text()
    for pid, label in [
        ("native", "每次询问"),
        ("all", "所有操作都要审批"),
        ("none", "允许完全访问"),
    ]:
        m = re.search(r'\{\s*id:\s*"%s",\s*name:\s*"([^"]+)"' % pid, src)
        assert m, f"审批下拉缺少档位 {pid}"
        assert m.group(1) == label, f"{pid} 的显示名应该是 {label!r}，实际 {m.group(1)!r}"


def test_approval_none_is_red_with_theme_tokens():
    """「允许完全访问」触发器染红，且必须用 --danger 主题令牌。"""
    src = _index_text()
    assert "policy-none" in src, "审批下拉没有 none 档的红色状态类"
    m = re.search(r"\.approval-switch\.policy-none \.approval-trigger\s*\{([^}]*)\}", src)
    assert m, "没有 policy-none 的 CSS 规则"
    css = m.group(1)
    assert "var(--danger)" in css, (
        f"none 档没用 --danger 令牌：{css.strip()!r} —— 写死颜色深色主题下会瞎"
    )


def test_approval_dropdown_aligned_right_and_hint_for_explicit_env():
    """审批下拉靠右对齐（在工具条右半边）；env 显式固定时菜单里要有提示。"""
    src = _index_text()
    i = src.find("function renderApprovalSwitches")
    j = src.find("function loadApprovalState", i)
    assert i >= 0 and j > i, "找不到审批下拉的渲染函数段"
    seg = src[i:j]
    assert "alignRight: true" in seg, "审批下拉应靠右对齐，否则菜单会戳出窗口右缘"
    assert "AGENTD_TOOLS_APPROVE" in seg, (
        "env 显式固定策略时菜单里没有提示 —— 用户会以为切了就永久生效"
    )


def test_trust_all_dialog_click_refreshes_approval_dropdown():
    """弹窗点「完全信任」后必须重新拉审批状态，两处 UI 不能各说各话。"""
    src = _index_text()
    m = re.search(r'markPermissionAnswered\(req, optionId\);([\s\S]{0,400})showNextPermission\(\);', src)
    assert m, "找不到 answerPermission 的回帧后处理段"
    assert 'loadApprovalState();' in m.group(1), (
        "点「完全信任」后没有刷新审批下拉 —— 弹窗与下拉的状态会漂移"
    )
