"""plan（计划）模式在 GUI 侧的测试。

锁三层：
1. reducer（acp_client）：plan 通知折叠成 Turn.plan 全量快照；条目 content 绝不
   被当成正文文本收走（顺序守卫）；垃圾数据不炸；
2. bridge：计划快照变化才发 plan 事件（差量思路同工具卡片），不变不刷屏；
3. index.html 静态校对（同 test_gui_blank_pill.py 的思路）：计划卡片渲染器、
   handle 的 plan 分支、新回合重置都在位 —— DOM 逻辑没有可单测的纯函数切面，
   真机渲染由 out/ 下的 Electron harness 冒烟覆盖。

agentd 侧的行为（规划/审批/逐步执行）由 Agentd 仓库的 tests/test_plan_mode.py 锁。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from forgeagent.acp_client import AcpClient, Turn
from forgeagent.gui.bridge import Bridge

_INDEX = Path(__file__).resolve().parent.parent / "forgeagent" / "gui" / "assets" / "index.html"


def _plan_frame(entries: list[dict]) -> dict:
    """拼一个 ACP 的 plan 通知帧（AgentPlanUpdate 的 wire 形状）。"""
    return {
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {
            "sessionId": "s1",
            "update": {"sessionUpdate": "plan", "entries": entries},
        },
    }


_ENTRIES_V1 = [
    {"content": "列出文件", "priority": "medium", "status": "pending"},
    {"content": "读取配置", "priority": "medium", "status": "pending"},
]


# ---- reducer：plan 通知 → Turn.plan ----


def test_plan_update_folds_into_turn_plan():
    client, turn = AcpClient(), Turn()
    client._apply_update(turn, _plan_frame(_ENTRIES_V1))
    assert turn.plan == _ENTRIES_V1


def test_plan_update_replaces_wholesale():
    """全量快照语义：第二次通知整体替换，不与第一次合并。"""
    client, turn = AcpClient(), Turn()
    client._apply_update(turn, _plan_frame(_ENTRIES_V1))
    client._apply_update(
        turn,
        _plan_frame(
            [
                {"content": "列出文件", "priority": "medium", "status": "completed"},
                {"content": "读取配置", "priority": "medium", "status": "in_progress"},
                {"content": "汇报", "priority": "high", "status": "pending"},
            ]
        ),
    )
    assert [e["status"] for e in turn.plan] == ["completed", "in_progress", "pending"]
    assert turn.plan[2]["priority"] == "high"


def test_plan_content_not_swallowed_as_text():
    """顺序守卫：plan 分支必须在 _collect_text 之前，否则条目 content 会漏进正文。"""
    client, turn = AcpClient(), Turn()
    client._apply_update(turn, _plan_frame(_ENTRIES_V1))
    assert turn.text == ""
    assert turn.thought == ""
    assert turn.unknown == []


def test_plan_update_survives_garbage():
    """缺字段 / 非列表 entries 不能炸，也不能污染已有计划。"""
    client, turn = AcpClient(), Turn()
    client._apply_update(turn, _plan_frame(_ENTRIES_V1))
    # entries 缺失：保持原样（不是清空）
    client._apply_update(
        turn,
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {"sessionId": "s1", "update": {"sessionUpdate": "plan"}},
        },
    )
    assert turn.plan == _ENTRIES_V1
    # 列表里混垃圾：空 content 丢弃，缺 status 补 pending
    client._apply_update(
        turn,
        _plan_frame([{"content": "  "}, "not-a-dict", {"content": "有效步骤"}]),
    )
    assert turn.plan == [{"content": "有效步骤", "priority": "medium", "status": "pending"}]


# ---- bridge：快照变了才发 ----


class _PlanFakeClient:
    """prompt() 期间计划状态在演的假 client。"""

    def __init__(self, snapshots: list[list[dict]]) -> None:
        self._snapshots = snapshots
        self.session_id = "sess_plan123456"
        self.stderr_lines: list[str] = []

    async def start(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def prompt(self, text: str, images=None):
        for snap in self._snapshots:
            turn = Turn(user=text)
            turn.plan = [dict(e) for e in snap]
            yield turn
        turn = Turn(user=text)
        turn.plan = [dict(e) for e in self._snapshots[-1]]
        turn.stop_reason = "end_turn"
        yield turn


def _drain_until_done(bridge: Bridge, rounds: int = 20) -> list[dict]:
    out: list[dict] = []
    for _ in range(rounds):
        batch = bridge.next_events(timeout=2.0)
        out.extend(batch)
        if any(e.get("type") == "done" for e in batch):
            break
    return out


def test_bridge_emits_plan_events_only_on_change():
    """计划快照变化才发；连续相同的快照不重复发（差量思路同工具卡片）。"""
    done_step = {"content": "列出文件", "priority": "medium", "status": "completed"}
    pending = {"content": "读取配置", "priority": "medium", "status": "pending"}
    snapshots = [
        _ENTRIES_V1,                                        # 建计划
        [{**_ENTRIES_V1[0], "status": "in_progress"}, pending],  # 步骤 1 开始
        [done_step, pending],                               # 步骤 1 完成
        [done_step, pending],                               # 没变 —— 不该再发
    ]
    bridge = Bridge(client=_PlanFakeClient(snapshots))
    try:
        bridge.start()
        bridge.send("看看项目")
        evs = _drain_until_done(bridge)
        plans = [e for e in evs if e["type"] == "plan"]
        assert len(plans) == 3, [e.get("entries") for e in plans]
        assert [e["entries"][0]["status"] for e in plans] == [
            "pending",
            "in_progress",
            "completed",
        ]
        # done 仍然正常收尾
        assert any(e["type"] == "done" for e in evs)
    finally:
        bridge.close()


# ---- index.html 静态校对 ----


def _index_text() -> str:
    assert _INDEX.is_file(), f"找不到界面资源：{_INDEX}"
    return _INDEX.read_text(encoding="utf-8")


def test_index_has_plan_card_renderer():
    src = _index_text()
    assert "function upsertPlan(ev)" in src
    assert 'className = "bubble plan"' in src
    # 三个状态都要有图标（漏一个界面就出空白行）
    for frag in ('pending: "○"', 'in_progress: "▶"', 'completed: "✓"'):
        assert frag in src, f"计划状态图标少了：{frag}"


def test_index_handle_branches_plan_event():
    src = _index_text()
    assert 'if (ev.type === "plan")' in src
    assert "upsertPlan(ev);" in src


def test_index_resets_plan_card_on_new_turn():
    """新回合必须放掉计划卡引用，否则旧卡片被就地更新成新计划。"""
    src = _index_text()
    assert "planCard = null;" in src


def test_index_has_plan_card_css():
    src = _index_text()
    assert ".bubble.plan {" in src
    assert ".plan-item.in_progress .plan-icon" in src
