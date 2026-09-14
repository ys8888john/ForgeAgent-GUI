"""GUI 桥接层测试。

不需要 pywebview，也不需要图形环境 —— Bridge 只依赖一个「长得像 AcpClient」
的对象，所以塞个假的 async client 就能测完整逻辑。这也正是把它跟
window.py 分开的原因：窗口层没法在 CI 里测，桥接层能。
"""

from __future__ import annotations

import asyncio
import time

import pytest

from forgeagent.acp_client import ToolCallState, Turn
from forgeagent.gui.bridge import Bridge


class _FakeClient:
    """最小 AcpClient 替身。

    只要实现 Bridge 真正用到的那几个成员：start / prompt / close /
    session_id / stderr_lines。故意不多实现 —— 多实现了就测不出
    Bridge 到底依赖了什么。
    """

    def __init__(
        self,
        chunks: tuple[str, ...] = ("你", "好", "呀"),
        *,
        fail_start: bool = False,
        fail_prompt: bool = False,
        gate=None,
    ) -> None:
        self._chunks = chunks
        self._fail_start = fail_start
        self._fail_prompt = fail_prompt
        # gate 不为 None 时，吐完增量后会一直等它被 set 才收尾。
        # 用来把「上一轮还没结束」变成确定状态，而不是靠时序碰运气。
        self._gate = gate
        self.session_id = "sess_test123456"
        self.stderr_lines = ["[agentd] 自动选用 Ollama 模型: qwen3.5:9b-text"]
        self.started = False
        self.closed = False

    async def start(self) -> None:
        if self._fail_start:
            raise RuntimeError("agentd 起不来")
        self.started = True

    async def close(self) -> None:
        self.closed = True

    async def new_session(self) -> str:
        # 给 Bridge.new_session 用的假实现：记下一个新 id 即可
        self.session_id = "sess_new789012"
        return self.session_id

    async def prompt(self, text: str):
        if self._fail_prompt:
            raise RuntimeError("prompt 炸了")
        turn = Turn(user=text)
        for chunk in self._chunks:
            turn.text += chunk
            yield turn
        if self._gate is not None:
            while not self._gate.is_set():
                await asyncio.sleep(0.01)
        turn.stop_reason = "end_turn"
        yield turn


def _drain(bridge: Bridge, timeout: float = 2.0) -> list[dict]:
    """取一批事件。超时返回空列表。"""
    return bridge.next_events(timeout=timeout)


def _drain_until_done(bridge: Bridge, rounds: int = 20) -> list[dict]:
    """一直取到出现 done 事件为止（防止测试挂死）。"""
    out: list[dict] = []
    for _ in range(rounds):
        batch = _drain(bridge, timeout=2.0)
        out.extend(batch)
        if any(e.get("type") == "done" for e in batch):
            break
    return out


# ---- start ----

def test_start_emits_connecting_then_ready():
    fake = _FakeClient()
    bridge = Bridge(client=fake)
    try:
        ok = bridge.start()
        assert ok["ok"] is True
        assert ok["session"] == "sess_test123456"

        evs = _drain(bridge)
        kinds = [e["type"] for e in evs]
        assert kinds == ["status", "status"]
        assert evs[0]["state"] == "connecting"
        assert evs[1]["state"] == "ready"
    finally:
        bridge.close()


def test_start_failure_emits_error_status_with_log():
    """启动失败必须把 agentd 的日志一起带出来 ——
    否则用户只能看到一句「起不来」，无从下手。"""
    fake = _FakeClient(fail_start=True)
    bridge = Bridge(client=fake)
    try:
        ok = bridge.start()
        assert ok["ok"] is False

        evs = _drain(bridge)
        last = evs[-1]
        assert last["type"] == "status"
        assert last["state"] == "error"
        assert "agentd 起不来" in last["message"]
        assert last["log"] == fake.stderr_lines
    finally:
        bridge.close()


# ---- send / 流式 ----

def test_send_emits_user_then_deltas_then_done():
    fake = _FakeClient(chunks=("你", "好"))
    bridge = Bridge(client=fake)
    try:
        assert bridge.send("问个问题")["ok"] is True

        evs = _drain_until_done(bridge)
        assert evs[0] == {"type": "user", "text": "问个问题"}

        deltas = [e for e in evs if e["type"] == "delta"]
        assert [d["role"] for d in deltas] == ["assistant", "assistant"]
        # 增量拼起来必须等于完整回复，不能重复也不能丢字
        assert "".join(d["text"] for d in deltas) == "你好"

        done = evs[-1]
        assert done["type"] == "done"
        assert done["stop"] == "end_turn"
        assert done["error"] == ""
    finally:
        bridge.close()


def test_send_rejects_empty_text():
    bridge = Bridge(client=_FakeClient())
    try:
        assert bridge.send("")["ok"] is False
        assert bridge.send("   ")["ok"] is False
    finally:
        bridge.close()


def test_second_send_while_busy_is_rejected():
    """上一轮没结束就再发会写乱历史，必须挡住。

    用 gate 把「上一轮还没结束」钉死：不加的话，假 client 跑得太快，
    后台事件循环可能已经把 _busy 清了，测试就变成碰运气。
    """
    import threading

    gate = threading.Event()
    bridge = Bridge(client=_FakeClient(gate=gate))
    try:
        assert bridge.send("第一轮")["ok"] is True
        # 等增量出来，确认这一轮确实进行中
        assert any(e["type"] == "delta" for e in _drain_until_done(bridge, rounds=3))
        assert bridge.send("第二轮")["ok"] is False

        gate.set()          # 放行，让第一轮收尾
        _drain_until_done(bridge)
        # 结束后又能发了
        assert bridge.send("第三轮")["ok"] is True
    finally:
        gate.set()
        bridge.close()


def test_prompt_exception_becomes_error_event_not_crash():
    fake = _FakeClient(fail_prompt=True)
    bridge = Bridge(client=fake)
    try:
        bridge.send("会炸的问题")
        evs = _drain_until_done(bridge)
        done = evs[-1]
        assert done["type"] == "done"
        assert "prompt 炸了" in done["error"]
        # 出错后必须解锁，否则界面永远卡在「生成中」
        assert bridge.send("还能发")["ok"] is True
    finally:
        bridge.close()


def test_thought_and_error_go_to_their_own_roles():
    """思考 / 正文 / 错误要分角色，界面才能分开渲染。"""

    class _MixedClient(_FakeClient):
        async def prompt(self, text: str):
            turn = Turn(user=text)
            turn.thought = "先想想"
            yield turn
            turn.text = "答案"
            yield turn
            turn.error = "后面出问题了"
            yield turn
            turn.stop_reason = "end_turn"
            yield turn

    bridge = Bridge(client=_MixedClient())
    try:
        bridge.send("混合输出")
        evs = _drain_until_done(bridge)
        roles = [e["role"] for e in evs if e["type"] == "delta"]
        assert roles == ["thought", "assistant", "error"]
    finally:
        bridge.close()


# ---- 工具调用事件 ----

class _ToolClient(_FakeClient):
    """吐一个带工具调用的 Turn：先运行中，再完成（带输出）。"""

    async def prompt(self, text: str):
        turn = Turn(user=text)
        tool = ToolCallState(
            call_id="c1", title="echo", kind="other", status="in_progress"
        )
        turn.tools.append(tool)
        turn.tool_map["c1"] = tool
        yield turn           # -> 应发一条 tool（in_progress）
        yield turn           # -> 状态没变，不该重复发
        tool.status = "completed"
        tool.output = "echo: hi"
        yield turn           # -> 应发一条 tool（completed + 输出）
        turn.stop_reason = "end_turn"
        yield turn


def test_tool_events_are_emitted_with_dedup():
    """工具状态变化要发 tool 事件；状态没变不能重复发（否则界面闪）。"""
    bridge = Bridge(client=_ToolClient())
    try:
        bridge.send("用个工具")
        evs = _drain_until_done(bridge)

        tools = [e for e in evs if e["type"] == "tool"]
        assert len(tools) == 2, tools
        assert tools[0] == {
            "type": "tool",
            "id": "c1",
            "title": "echo",
            "kind": "other",
            "status": "in_progress",
            "output": "",
        }
        assert tools[1]["status"] == "completed"
        assert tools[1]["output"] == "echo: hi"
    finally:
        bridge.close()


def test_no_tool_events_for_plain_reply():
    """没有工具调用时不该冒 tool 事件（这是普通对话的常态）。"""
    bridge = Bridge(client=_FakeClient(chunks=("好",)))
    try:
        bridge.send("普通问题")
        evs = _drain_until_done(bridge)
        assert [e for e in evs if e["type"] == "tool"] == []
    finally:
        bridge.close()


# ---- 取事件 / 关闭 ----

def test_next_events_returns_empty_on_timeout():
    bridge = Bridge(client=_FakeClient())
    try:
        assert bridge.next_events(timeout=0.1) == []
    finally:
        bridge.close()


def test_next_events_after_close_returns_empty():
    bridge = Bridge(client=_FakeClient())
    bridge.close()
    assert bridge.next_events(timeout=0.1) == []


# ---- 反向通道：Python 让界面干活 / 界面自报状态 ----

def test_push_command_comes_back_as_command_event():
    """外部（含冒烟测试）要能让界面自己发消息，而不是 Python 代发。"""
    bridge = Bridge(client=_FakeClient())
    try:
        bridge.push_command({"action": "send", "text": "你好"})
        evs = bridge.next_events(timeout=0.5)
        assert evs == [{"type": "command", "cmd": {"action": "send", "text": "你好"}}]
    finally:
        bridge.close()


def test_command_is_returned_immediately_without_waiting_for_events():
    """命令不该被 2 秒长轮询拖住 —— 它优先级更高。"""
    bridge = Bridge(client=_FakeClient())
    try:
        bridge.push_command({"action": "report"})
        t0 = time.time()
        evs = bridge.next_events(timeout=5.0)
        assert time.time() - t0 < 1.0
        assert evs[0]["type"] == "command"
    finally:
        bridge.close()


def test_push_command_ignores_non_dict():
    bridge = Bridge(client=_FakeClient())
    try:
        bridge.push_command("oops")  # type: ignore[arg-type]
        assert bridge.next_events(timeout=0.2) == []
    finally:
        bridge.close()


def test_ui_report_accumulates_and_is_readable_from_outside():
    """界面自报状态：外部（含冒烟测试）靠它判断「界面到底渲染出来没有」。"""
    bridge = Bridge(client=_FakeClient())
    try:
        bridge.ui_report({"bridge": "ready"})
        bridge.ui_report({"status": "stop=end_turn"})
        assert bridge.ui_state() == {"bridge": "ready", "status": "stop=end_turn"}
    finally:
        bridge.close()


def test_ui_report_ignores_non_dict():
    bridge = Bridge(client=_FakeClient())
    try:
        bridge.ui_report("oops")  # type: ignore[arg-type]
        assert bridge.ui_state() == {}
    finally:
        bridge.close()


def test_ui_state_returns_a_copy():
    """外面的代码改了副本不能影响内部状态。"""
    bridge = Bridge(client=_FakeClient())
    try:
        bridge.ui_report({"bridge": "ready"})
        snap = bridge.ui_state()
        snap["bridge"] = "tampered"
        assert bridge.ui_state()["bridge"] == "ready"
    finally:
        bridge.close()


def test_close_stops_client_and_thread():
    fake = _FakeClient()
    bridge = Bridge(client=fake)
    bridge.close()
    assert fake.closed is True
    assert bridge._thread.is_alive() is False


def test_stderr_tail_exposes_agent_log():
    """日志面板靠这个拿 agentd 的输出（里面能看到自动选了哪个模型）。"""
    bridge = Bridge(client=_FakeClient())
    try:
        assert bridge.stderr_tail(10) == [
            "[agentd] 自动选用 Ollama 模型: qwen3.5:9b-text"
        ]
    finally:
        bridge.close()


def test_close_is_idempotent():
    bridge = Bridge(client=_FakeClient())
    bridge.close()
    bridge.close()  # 关两次不能抛（窗口关闭事件可能重复触发）
