"""协议层单元测试 —— 不启子进程、不装 Textual 也能跑。

重点测 reducer：把 ACP 事件流折叠成 Turn 的正确性。
这块错了 UI 必然错，而且在流式输出里靠肉眼极难发现——所以必须钉死。

另外还测**反向请求**（agent -> client 的 session/request_permission）：
它是请求，不回帧对方就永久阻塞，所以"有没有回""回成什么形状"都必须钉。
"""

from __future__ import annotations

import asyncio
import json

from forgeagent.acp_client import ERROR_MARK, NOTICE_MARK, AcpClient, Turn, _collect_text


def _notify(kind: str, text: str) -> dict:
    """拼一个 session/update 通知帧。"""
    return {
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {
            "sessionId": "s1",
            "update": {
                "sessionUpdate": kind,
                "content": {"type": "text", "text": text},
            },
        },
    }


# ---- _collect_text：宽容提取 ----

def test_collect_text_from_nested_dict():
    node = {"update": {"content": {"type": "text", "text": "hello"}}}
    assert _collect_text(node) == "hello"


def test_collect_text_from_list():
    node = [{"text": "a"}, {"text": "b"}]
    assert _collect_text(node) == "ab"


def test_collect_text_ignores_non_string():
    # text 字段不是字符串时不能被拼进去（否则会抛 TypeError）
    assert _collect_text({"text": 123, "other": {"text": "ok"}}) == "ok"


# ---- reducer：事件折叠成 Turn ----

def test_message_chunks_accumulate():
    client, turn = AcpClient(), Turn()
    client._apply_update(turn, _notify("agent_message_chunk", "你"))
    client._apply_update(turn, _notify("agent_message_chunk", "好"))
    assert turn.text == "你好"
    assert turn.thought == ""


def test_thought_goes_to_separate_channel():
    client, turn = AcpClient(), Turn()
    client._apply_update(turn, _notify("agent_thought_chunk", "思考中"))
    client._apply_update(turn, _notify("agent_message_chunk", "答案"))
    assert turn.thought == "思考中"
    assert turn.text == "答案"


def test_user_message_chunk_is_not_echoed():
    # 自己说的话不该被重复渲染进正文
    client, turn = AcpClient(), Turn()
    client._apply_update(turn, _notify("user_message_chunk", "我的问题"))
    assert turn.text == ""


def test_unknown_kind_is_recorded_not_lost():
    # 内核还没产出的事件类型（plan 已经实现了，见 test_gui_plan.py），先记下来，
    # 别静默吞掉
    client, turn = AcpClient(), Turn()
    client._apply_update(turn, _notify("some_future_kind", "将来的事件"))
    assert turn.unknown == ["some_future_kind"]


# ---- 工具调用：折叠进 Turn.tools ----

def _tool_frame(session_update: str, **fields) -> dict:
    """拼一个工具调用通知帧（tool_call / tool_call_update）。"""
    update = {"sessionUpdate": session_update}
    update.update(fields)
    return {
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {"sessionId": "s1", "update": update},
    }


def test_tool_call_is_recorded_as_tool_state():
    client, turn = AcpClient(), Turn()
    client._apply_update(
        turn,
        _tool_frame(
            "tool_call",
            toolCallId="c1",
            title="读取文件",
            kind="read",
            status="in_progress",
        ),
    )
    assert turn.unknown == []          # 不再落进 unknown
    assert len(turn.tools) == 1
    t = turn.tools[0]
    assert (t.call_id, t.title, t.kind, t.status) == ("c1", "读取文件", "read", "in_progress")


def test_tool_call_update_folds_into_same_state():
    """同一 call_id 的 update 就地更新，不新开一条。"""
    client, turn = AcpClient(), Turn()
    client._apply_update(
        turn, _tool_frame("tool_call", toolCallId="c1", title="跑命令", status="pending")
    )
    client._apply_update(
        turn,
        _tool_frame("tool_call_update", toolCallId="c1", status="completed", rawOutput="ok"),
    )
    assert len(turn.tools) == 1
    assert turn.tools[0].status == "completed"
    assert turn.tools[0].output == "ok"


def test_tool_call_without_id_is_ignored():
    """没有 call_id 就归并不了，宁可不画也不画错。"""
    client, turn = AcpClient(), Turn()
    client._apply_update(turn, _tool_frame("tool_call", title="无 id"))
    assert turn.tools == []


def test_user_denial_is_restored_from_failed_to_cancelled():
    """回归：ACP 没有 cancelled 状态，"用户拒绝"在协议上被迫折成 failed。

    只能靠输出文案把它还原回来 —— 否则界面上"用户点了拒绝"和"命令炸了"
    长得一模一样，用户会以为是自己把环境搞坏了。
    """
    from forgeagent.acp_client import DENY_MARK

    client, turn = AcpClient(), Turn()
    client._apply_update(
        turn, _tool_frame("tool_call", toolCallId="c1", title="write_file", kind="edit")
    )
    client._apply_update(
        turn,
        _tool_frame(
            "tool_call_update",
            toolCallId="c1",
            status="failed",
            rawOutput=DENY_MARK + "执行 write_file",
        ),
    )
    assert turn.tools[0].status == "cancelled"


def test_real_failure_stays_failed():
    """真失败不能被误判成"用户拒绝"。"""
    client, turn = AcpClient(), Turn()
    client._apply_update(
        turn,
        _tool_frame(
            "tool_call_update",
            toolCallId="c1",
            status="failed",
            rawOutput="[错误] 命令退出码 1：pytest",
        ),
    )
    assert turn.tools[0].status == "failed"


def test_empty_kind_falls_back_to_message():
    # SDK 各版本字段名有出入，空 kind 宁可当正文也别丢字
    client, turn = AcpClient(), Turn()
    client._apply_update(turn, _notify("", "兜底文本"))
    assert turn.text == "兜底文本"


# ---- Turn 状态 ----

def test_turn_is_running_until_stopped():
    turn = Turn()
    assert turn.running is True
    turn.stop_reason = "end_turn"
    assert turn.running is False


def test_turn_error_stops_running():
    turn = Turn(error="连接断了")
    assert turn.running is False


# ---- 错误识别 ----
#
# ACP 的 stop_reason 只有 end_turn/max_tokens/max_turn_requests/refusal/cancelled
# 五种，没有 error。agentd 只好把错误塞 thought 通道 + 打上 [错误] 前缀。
# 这里验证前端能把它认出来，并且不再当成"思考"显示。

def test_error_marker_is_split_out_of_thought():
    client, turn = AcpClient(), Turn()
    client._apply_update(turn, _notify("agent_thought_chunk", ERROR_MARK + " 模型不存在"))
    assert turn.error == "模型不存在"
    assert turn.thought == ""
    assert turn.running is False


def test_normal_thought_is_not_treated_as_error():
    client, turn = AcpClient(), Turn()
    client._apply_update(turn, _notify("agent_thought_chunk", "让我想想"))
    assert turn.thought == "让我想想"
    assert turn.error == ""


def test_split_error_chunks_all_go_to_error():
    """错误被拆成多个 chunk 时，后续 chunk 不能掉回 thought。"""
    client, turn = AcpClient(), Turn()
    client._apply_update(turn, _notify("agent_thought_chunk", ERROR_MARK + " Ollama HTTP 404："))
    client._apply_update(turn, _notify("agent_thought_chunk", "model not found"))
    assert turn.error == "Ollama HTTP 404：model not found"
    assert turn.thought == ""


# ---- 系统提示识别 ----
#
# ACP 同样没有"系统提示"通道，agentd 的 Notice 事件（上下文裁剪提示之类）
# 也借 thought 通道下发 + 打 [提示] 前缀。逻辑与错误同路，只是落点不同：
# 错误是故障要红色，提示是说明要淡灰，两者都不能混进模型的思考文本。

def test_notice_marker_is_split_out_of_thought():
    client, turn = AcpClient(), Turn()
    client._apply_update(turn, _notify("agent_thought_chunk", NOTICE_MARK + " 上下文超出预算"))
    assert turn.notice == "上下文超出预算"
    assert turn.thought == ""


def test_notice_is_not_confused_with_error():
    """[提示] 和 [错误] 共用一条通道，认错会把系统说明显示成红色故障。"""
    client, turn = AcpClient(), Turn()
    client._apply_update(turn, _notify("agent_thought_chunk", NOTICE_MARK + " 已省略 3 条"))
    assert turn.error == ""
    client._apply_update(turn, _notify("agent_thought_chunk", ERROR_MARK + " 真的炸了"))
    assert turn.notice == "已省略 3 条"
    assert turn.error == "真的炸了"


def test_split_notice_chunks_all_go_to_notice():
    client, turn = AcpClient(), Turn()
    client._apply_update(turn, _notify("agent_thought_chunk", NOTICE_MARK + " 上下文超出预算，"))
    client._apply_update(turn, _notify("agent_thought_chunk", "省略了较早的 3 条消息"))
    assert turn.notice == "上下文超出预算，省略了较早的 3 条消息"
    assert turn.thought == ""


def test_error_and_text_can_coexist():
    """先出错、后又有正文的情况（比如降级回复），两个都不能丢。"""
    client, turn = AcpClient(), Turn()
    client._apply_update(turn, _notify("agent_thought_chunk", ERROR_MARK + " boom"))
    client._apply_update(turn, _notify("agent_message_chunk", "仍然可用的回复"))
    assert turn.error == "boom"
    assert turn.text == "仍然可用的回复"


# ---- 反向请求：agent -> client 的审批 ----
#
# ACP 不是单向的：agent 会反过来请求客户端（审批、读文件、开终端）。
# 这些是**请求**，每个都必须回一帧，不回对方就永久卡在 await 上，
# 而且两边日志都干干净净 —— 最难查的一类问题。所以这块要逐条钉死。

PERM_PARAMS = {
    "sessionId": "s1",
    "toolCall": {
        "toolCallId": "call_1",
        "title": "run_command",
        "kind": "execute",
        "rawInput": {"detail": "command: echo hi"},
    },
    "options": [
        {"optionId": "allow_once", "name": "允许一次", "kind": "allow_once"},
        {"optionId": "allow_session", "name": "本会话总是允许", "kind": "allow_always"},
        {"optionId": "trust_all", "name": "完全信任（永不再问）", "kind": "allow_always"},
        {"optionId": "reject", "name": "拒绝", "kind": "reject_once"},
    ],
}


class _FakeStdin:
    """只记下写出去的东西。"""

    def __init__(self) -> None:
        self.frames: list[dict] = []

    def write(self, data: bytes) -> None:
        self.frames.append(json.loads(data.decode("utf-8")))

    async def drain(self) -> None:
        return None


class _FakeProc:
    # returncode 必须给：_read_stdout 的 finally 靠它判断 agentd 死没死
    # （cancel 时 return 也会走 finally）。缺了它 AttributeError 会把整个
    # 清理逻辑（失败 pending、结掉待审批）跳过，测试就少了真保真度。
    returncode = None

    def __init__(self, stdout=None) -> None:
        self.stdin = _FakeStdin()
        self.stdout = stdout


async def _settle(client: AcpClient, rounds: int = 100) -> None:
    """给后台任务一点时间把回帧写出去（没有真实 IO，只能让出事件循环）。"""
    for _ in range(rounds):
        if client._proc is not None and client._proc.stdin.frames:
            return
        await asyncio.sleep(0.01)


async def test_permission_request_is_exposed_with_options():
    client = AcpClient()
    client._proc = _FakeProc()
    seen: list = []
    client.on_permission = seen.append

    await client._handle_incoming(42, "session/request_permission", PERM_PARAMS)
    assert len(seen) == 1
    req = seen[0]
    assert (req.request_id, req.session_id, req.call_id) == (42, "s1", "call_1")
    assert (req.title, req.kind) == ("run_command", "execute")
    assert req.detail == "command: echo hi"
    assert req.allow_ids == ["allow_once", "allow_session", "trust_all"]
    assert req.reject_id == "reject"


async def test_permission_without_handler_replies_cancelled():
    """没人可问 = 拒绝。绝不能"没人答就放行" —— 那审批就成了摆设。"""
    client = AcpClient()
    client._proc = _FakeProc()

    await client._handle_incoming(5, "session/request_permission", PERM_PARAMS)
    await _settle(client)

    assert client._proc.stdin.frames == [
        {"jsonrpc": "2.0", "id": 5, "result": {"outcome": {"outcome": "cancelled"}}}
    ]


async def test_permission_hook_exception_is_treated_as_denial():
    client = AcpClient()
    client._proc = _FakeProc()

    def boom(req):
        raise RuntimeError("界面炸了")

    client.on_permission = boom
    await client._handle_incoming(5, "session/request_permission", PERM_PARAMS)
    await _settle(client)
    assert client._proc.stdin.frames[0]["result"]["outcome"]["outcome"] == "cancelled"


async def test_allow_answer_goes_back_as_selected():
    client = AcpClient()
    client._proc = _FakeProc()
    client.on_permission = lambda req: None

    await client._handle_incoming(6, "session/request_permission", PERM_PARAMS)
    assert await client.answer_permission(6, "allow_once") is True
    await _settle(client)
    assert client._proc.stdin.frames[0]["result"] == {
        "outcome": {"outcome": "selected", "optionId": "allow_once"}
    }


async def test_reject_option_is_selected_not_cancelled():
    """选了"拒绝"那个选项 = selected（我们确实给了它一个 optionId）。

    cancelled 专指"压根没选"（关掉弹窗/超时）。区分这两者是为了让 agentd
    端的日志能分辨"用户明确拒绝"和"没人处理"，排查时差别很大。
    """
    client = AcpClient()
    client._proc = _FakeProc()
    client.on_permission = lambda req: None

    await client._handle_incoming(7, "session/request_permission", PERM_PARAMS)
    await client.answer_permission(7, "reject")
    await _settle(client)
    assert client._proc.stdin.frames[0]["result"]["outcome"] == {
        "outcome": "selected",
        "optionId": "reject",
    }


async def test_answer_unknown_request_returns_false():
    client = AcpClient()
    client._proc = _FakeProc()
    # 没收到过这个请求，或者已经答过了 —— 都不是错误，只是没答成
    assert await client.answer_permission(999, "allow_once") is False


async def test_second_answer_is_ignored():
    """重复点击不能写两帧（agentd 那边只等一帧，多写会污染下一个请求的配对）。"""
    client = AcpClient()
    client._proc = _FakeProc()
    client.on_permission = lambda req: None

    await client._handle_incoming(8, "session/request_permission", PERM_PARAMS)
    assert await client.answer_permission(8, "allow_once") is True
    assert await client.answer_permission(8, "reject") is False
    await _settle(client)
    assert len(client._proc.stdin.frames) == 1


async def test_cancel_pending_permissions_settles_them():
    """切会话/关窗口时必须把挂着的审批结掉，否则 agentd 那个 await 永不返回。"""
    client = AcpClient()
    client._proc = _FakeProc()
    client.on_permission = lambda req: None

    await client._handle_incoming(11, "session/request_permission", PERM_PARAMS)
    await client._handle_incoming(12, "session/request_permission", PERM_PARAMS)
    assert await client.cancel_pending_permissions() == 2
    await _settle(client)
    outcomes = [f["result"]["outcome"]["outcome"] for f in client._proc.stdin.frames]
    assert outcomes == ["cancelled", "cancelled"]
    # 已经结掉的再结一次是 0，不是报错
    assert await client.cancel_pending_permissions() == 0


async def test_unknown_agent_request_gets_method_not_found():
    """没实现的协议扩展也必须回帧 —— 静默丢掉就是一个永不醒的 await。"""
    client = AcpClient()
    client._proc = _FakeProc()

    await client._handle_incoming(9, "fs/read_text_file", {"path": "x"})
    frame = client._proc.stdin.frames[0]
    assert frame["id"] == 9
    assert frame["error"]["code"] == -32601


async def test_read_stdout_routes_by_method_not_by_id():
    """回归：id 会撞车，必须靠"有没有 method 字段"判断方向。

    两个方向各自从 1 开始编号，所以 agent 的 1 号审批请求和我们自己的
    1 号请求是同一个 id。早先的写法是"带 id 就是给我的响应"，结果审批请求
    被当成 prompt 的响应 —— prompt 提前结束、审批永远没人回、agentd 永久挂住。
    """
    client = AcpClient()
    reader = asyncio.StreamReader()
    client._proc = _FakeProc(stdout=reader)

    pending: asyncio.Future = asyncio.get_running_loop().create_future()
    client._pending[1] = pending  # 我们发出去的第 1 号请求还在等响应

    got = asyncio.Event()
    client.on_permission = lambda req: got.set()

    task = asyncio.ensure_future(client._read_stdout())
    try:
        frame = {
            "jsonrpc": "2.0",
            "id": 1,  # 和上面那个 pending 撞车
            "method": "session/request_permission",
            "params": PERM_PARAMS,
        }
        reader.feed_data(json.dumps(frame).encode("utf-8") + b"\n")
        await asyncio.wait_for(got.wait(), timeout=2)

        assert not pending.done(), "审批请求被误当成 prompt 的响应了"
        assert 1 in client._permissions
    finally:
        task.cancel()


async def test_plain_notification_still_goes_to_queue():
    """没有 id 的（通知）仍然要入队给 reducer，别被分流逻辑吃掉。"""
    client = AcpClient()
    reader = asyncio.StreamReader()
    client._proc = _FakeProc(stdout=reader)

    task = asyncio.ensure_future(client._read_stdout())
    try:
        reader.feed_data(json.dumps(_notify("agent_message_chunk", "嗨")).encode() + b"\n")
        msg = await asyncio.wait_for(client._notifications.get(), timeout=2)
        assert msg["method"] == "session/update"
    finally:
        task.cancel()


# ---- 额外工作区（additionalDirectories）----

def test_parse_additional_dirs_handles_missing_and_relative(monkeypatch):
    """缺值/空串 → []；绝对路径留下、相对路径忽略、~ 展开。

    别硬编码 POSIX 路径（"/abs/a"）：Windows 上 Path("/abs/a") 会被解析成
    当前盘符下的 "\\abs\\a"，str() 出来分隔符变了，断言必挂。所以按平台
    挑一个本机真实存在的绝对路径来喂，"相对段被丢弃"这条另外单独测。
    """
    from forgeagent.acp_client import _parse_additional_dirs
    import os
    from pathlib import Path

    assert _parse_additional_dirs(None) == []
    assert _parse_additional_dirs("") == []

    abs_dir = str(Path("C:\\Windows").resolve()) if os.name == "nt" else "/usr"
    monkeypatch.setenv(
        "FORGEAGENT_ADDITIONAL_DIRS",
        abs_dir + os.pathsep + "relative" + os.pathsep + "~/docs",
    )
    out = _parse_additional_dirs(os.environ["FORGEAGENT_ADDITIONAL_DIRS"])
    # "relative" 是非绝对路径，实现明确要求丢弃，所以只剩两条
    assert out == [abs_dir, str(Path("~/docs").expanduser())]


def test_client_reads_additional_dirs_from_env(monkeypatch):
    import os
    from pathlib import Path
    # 分隔符同样要平台化，不能写死 ":"（Windows 是 ";"）
    d1 = str(Path("C:\\Windows").resolve()) if os.name == "nt" else "/tmp/x"
    d2 = str(Path("C:\\Users").resolve()) if os.name == "nt" else "/tmp/y"
    monkeypatch.setenv("FORGEAGENT_ADDITIONAL_DIRS", d1 + os.pathsep + d2)
    client = AcpClient()
    assert client.additional_directories == [d1, d2]


def test_client_additional_dirs_default_empty():
    assert AcpClient().additional_directories == []


def test_current_mode_update_syncs_client_state():
    """agentd 广播的 current_mode_update 要更新 client 状态（不做正文本折叠）。"""
    client = AcpClient()
    assert client.current_mode == "agent"
    turn = Turn(user="hi")
    client._apply_update(
        turn,
        {
            "params": {
                "update": {
                    "sessionUpdate": "current_mode_update",
                    "currentModeId": "single",
                }
            }
        },
    )
    assert client.current_mode == "single"
    assert turn.text == "" and turn.thought == ""   # 不污染正文/思考


# ---- prompt 超时：不是简单报错，要停掉 agentd 那轮 + 隔离僵尸余波 ----
#
# 2026-10-14 实测：agentd 单轮 15 次工具循环要 315s，而客户端 300s 就判超时
# —— 正常的长回答在生成到一半时被杀（半截答案 + 一行 AcpError）。两个教训：
#   1. 超时值必须高于合法轮次的耗时（现在默认 900s，env 可覆盖）；
#   2. 超时只是"客户端不再等"，agentd 那轮还在跑。不通知它停，它就以僵尸轮
#      跑完：占着会话锁、把残文发进通知队列污染下一轮。所以超时要连带发
#      session/cancel + 孤儿隔离（_read_stdout 里按 rid 认领）。

import pytest  # noqa: E402

from forgeagent import acp_client  # noqa: E402
from forgeagent.acp_client import (  # noqa: E402
    M_CANCEL,
    M_PERMISSION,
    M_PROMPT,
    AcpTimeoutError,
)


def test_default_timeout_covers_legit_long_turns(monkeypatch):
    """300s 低于 agentd 单轮上限（16 步 × 120s），实测 15 轮就要 315s。"""
    monkeypatch.delenv("FORGEAGENT_PROMPT_TIMEOUT", raising=False)
    assert acp_client.DEFAULT_TIMEOUT >= 900.0
    # 没设环境变量时，默认值就是 DEFAULT_TIMEOUT
    assert acp_client._env_timeout() == acp_client.DEFAULT_TIMEOUT


def test_timeout_env_override(monkeypatch):
    """FORGEAGENT_PROMPT_TIMEOUT 可覆盖；非法/非正数回落默认值。"""
    monkeypatch.setenv("FORGEAGENT_PROMPT_TIMEOUT", "42")
    assert acp_client._env_timeout() == 42.0
    monkeypatch.setenv("FORGEAGENT_PROMPT_TIMEOUT", "abc")
    assert acp_client._env_timeout() == acp_client.DEFAULT_TIMEOUT
    monkeypatch.setenv("FORGEAGENT_PROMPT_TIMEOUT", "-1")
    assert acp_client._env_timeout() == acp_client.DEFAULT_TIMEOUT


async def test_prompt_timeout_sends_cancel_and_raises_timeout_error():
    """超时必须转化成对 agentd 的停止指令，否则那轮会变僵尸继续跑。"""
    client = AcpClient(timeout=0.05)
    client._proc = _FakeProc()
    client.session_id = "s1"

    with pytest.raises(AcpTimeoutError):
        await client._call(M_PROMPT, {"sessionId": "s1", "prompt": []})

    frames = client._proc.stdin.frames
    assert frames[0]["method"] == M_PROMPT
    assert frames[1] == {
        "jsonrpc": "2.0",
        "method": M_CANCEL,
        "params": {"sessionId": "s1"},
    }
    assert client._orphan_rid == 1  # 被放弃的那轮被记下，隔离从此生效
    # 子类关系不能丢：现有 `except AcpError` 的调用方（TUI/bridge）行为不变
    assert isinstance(AcpTimeoutError("x"), acp_client.AcpError)


async def test_zombie_notifications_dropped_until_its_response():
    """僵尸轮的通知不能入队 —— 会被下一轮 prompt 消费，污染下一轮正文。"""
    client = AcpClient()
    reader = asyncio.StreamReader()
    client._proc = _FakeProc(stdout=reader)
    client._orphan_rid = 7  # 模拟：第 7 号 prompt 已超时放弃

    task = asyncio.ensure_future(client._read_stdout())
    try:
        reader.feed_data(
            json.dumps(_notify("agent_message_chunk", "僵尸残文")).encode() + b"\n"
        )
        reader.feed_data(
            json.dumps(
                {"jsonrpc": "2.0", "id": 7, "result": {"stopReason": "cancelled"}}
            ).encode()
            + b"\n"
        )
        for _ in range(100):
            if client._orphan_rid is None:
                break
            await asyncio.sleep(0.01)

        assert client._notifications.empty()  # 残文没进队列
        assert client._orphan_rid is None  # 隔离随它的响应帧到达而解除
        assert client._orphan_stop_reason == "cancelled"
        assert client._orphan_settled.is_set()
    finally:
        task.cancel()


async def test_zombie_permission_request_is_auto_cancelled():
    """僵尸轮发来的审批不能弹给用户（那轮已经结束了），但帧必须回。"""
    client = AcpClient()
    reader = asyncio.StreamReader()
    client._proc = _FakeProc(stdout=reader)
    client._orphan_rid = 7
    seen: list = []
    client.on_permission = seen.append

    task = asyncio.ensure_future(client._read_stdout())
    try:
        frame = {
            "jsonrpc": "2.0",
            "id": 3,
            "method": M_PERMISSION,
            "params": PERM_PARAMS,
        }
        reader.feed_data(json.dumps(frame).encode() + b"\n")
        await _settle(client)

        assert seen == []  # 没弹给界面
        assert client._proc.stdin.frames == [
            {"jsonrpc": "2.0", "id": 3, "result": {"outcome": {"outcome": "cancelled"}}}
        ]
    finally:
        task.cancel()


async def test_non_orphan_response_still_processed_while_isolating():
    """隔离只针对僵尸轮：并发的其他请求（如 set_mode）响应照常放行。"""
    client = AcpClient()
    reader = asyncio.StreamReader()
    client._proc = _FakeProc(stdout=reader)
    client._orphan_rid = 7
    other: asyncio.Future = asyncio.get_running_loop().create_future()
    client._pending[9] = other

    task = asyncio.ensure_future(client._read_stdout())
    try:
        reader.feed_data(
            json.dumps({"jsonrpc": "2.0", "id": 9, "result": {"modes": None}}).encode()
            + b"\n"
        )
        msg = await asyncio.wait_for(other, timeout=2)
        assert msg["result"] == {"modes": None}
        assert client._orphan_rid == 7  # 隔离没被误解除
    finally:
        task.cancel()


async def test_prompt_timeout_flow_stops_zombie_and_reports_actionably(monkeypatch):
    """端到端：超时 → 先提示"正在停止" → 僵尸收尾 → 可操作的错误文案。

    同时钉死"下一轮不被污染"：僵尸轮在收尾前发的残文，一条都不许进通知队列。
    """
    monkeypatch.setattr(acp_client, "_ZOMBIE_SETTLE", 0.2)
    client = AcpClient(timeout=0.05)
    reader = asyncio.StreamReader()
    client._proc = _FakeProc(stdout=reader)
    client.session_id = "s1"

    task = asyncio.ensure_future(client._read_stdout())
    gen = client.prompt("帮我规划一个rag的项目")
    try:
        # 第一帧：超时后的"正在停止"提示（是说明，不是错误）
        first = await asyncio.wait_for(gen.__anext__(), timeout=2)
        assert "停止本轮" in first.notice
        assert client._orphan_rid == 1

        # 僵尸轮的余波：残文 + 它自己的响应帧（stopReason=cancelled）
        reader.feed_data(
            json.dumps(_notify("agent_message_chunk", "僵尸残文")).encode() + b"\n"
        )
        reader.feed_data(
            json.dumps(
                {"jsonrpc": "2.0", "id": 1, "result": {"stopReason": "cancelled"}}
            ).encode()
            + b"\n"
        )

        with pytest.raises(acp_client.AcpError) as ei:
            await asyncio.wait_for(gen.__anext__(), timeout=2)
        assert "本轮已停止" in str(ei.value)
        # 文案里的秒数用实例自己的 timeout，别把默认值 900 写进来
        assert "0.05s" in str(ei.value)
        assert client._notifications.empty()  # 残文没留给下一轮

        methods = [f["method"] for f in client._proc.stdin.frames]
        assert methods == [M_PROMPT, M_CANCEL]
    finally:
        task.cancel()
        await gen.aclose()


async def test_prompt_timeout_reports_completed_but_undelivered(monkeypatch):
    """僵尸轮其实自己跑完了（stopReason=end_turn）：告诉用户回复已落库，
    别让人以为白跑了 —— 完整答案重新打开会话就能看到。"""
    monkeypatch.setattr(acp_client, "_ZOMBIE_SETTLE", 0.2)
    client = AcpClient(timeout=0.05)
    reader = asyncio.StreamReader()
    client._proc = _FakeProc(stdout=reader)
    client.session_id = "s1"

    task = asyncio.ensure_future(client._read_stdout())
    gen = client.prompt("hi")
    try:
        await asyncio.wait_for(gen.__anext__(), timeout=2)  # "正在停止"提示
        reader.feed_data(
            json.dumps(
                {"jsonrpc": "2.0", "id": 1, "result": {"stopReason": "end_turn"}}
            ).encode()
            + b"\n"
        )
        with pytest.raises(acp_client.AcpError) as ei:
            await asyncio.wait_for(gen.__anext__(), timeout=2)
        assert "已生成完" in str(ei.value)
        assert "会话历史" in str(ei.value)
    finally:
        task.cancel()
        await gen.aclose()


async def test_prompt_timeout_reports_still_settling_when_zombie_wont_stop(monkeypatch):
    """僵尸轮迟迟不收尾：文案说清"仍在收尾"，且隔离继续生效。"""
    monkeypatch.setattr(acp_client, "_ZOMBIE_SETTLE", 0.05)
    client = AcpClient(timeout=0.05)
    reader = asyncio.StreamReader()
    client._proc = _FakeProc(stdout=reader)
    client.session_id = "s1"

    task = asyncio.ensure_future(client._read_stdout())
    gen = client.prompt("hi")
    try:
        await asyncio.wait_for(gen.__anext__(), timeout=2)  # "正在停止"提示
        with pytest.raises(acp_client.AcpError) as ei:
            await asyncio.wait_for(gen.__anext__(), timeout=2)
        assert "仍在收尾" in str(ei.value)
        assert client._orphan_rid == 1  # 隔离继续

        # 隔离期后才到的残文，照样进不了队列
        reader.feed_data(
            json.dumps(_notify("agent_message_chunk", "迟到的残文")).encode() + b"\n"
        )
        for _ in range(100):
            if not client._notifications.empty():
                break
            await asyncio.sleep(0.01)
        assert client._notifications.empty()
    finally:
        task.cancel()
        await gen.aclose()


# ---- 审批策略扩展方法（_approval）：方法名与载荷形状钉死 ----


async def test_get_and_set_approval_use_ext_method(monkeypatch):
    """get/set 都走 `_approval`（下划线前缀 = ACP ext method）。

    方法名要是哪天改了，agentd 的 ext_method 会当场 ValueError（未知扩展
    方法）—— 但那要等真机才能暴露，这里直接钉死字符串，改这边必挂测试。
    """
    client = AcpClient()
    client._proc = object()  # set_approval 要求子进程已在（不真用它）
    calls: list[tuple[str, dict]] = []

    async def fake_call(method, params):
        calls.append((method, params))
        return {"result": {"policy": params.get("policy") or "native"}}

    monkeypatch.setattr(client, "_call", fake_call)
    assert await client.get_approval() == "native"
    assert await client.set_approval("none") == "none"
    assert calls == [("_approval", {}), ("_approval", {"policy": "none"})]


async def test_get_lsp_uses_ext_method_and_parses_placeholder(monkeypatch):
    """get_lsp 走 `_lsp` 扩展方法，并把结果折成 {status, servers} 形状。

    返回值缺字段时兜底成「未接入」占位（agentd 当前无 LSP 集成）。
    """
    client = AcpClient()
    client._proc = object()
    calls: list[tuple[str, dict]] = []

    async def fake_call(method, params):
        calls.append((method, params))
        if method == "_lsp":
            return {"result": {"status": "未接入", "servers": []}}
        return {"result": {}}

    monkeypatch.setattr(client, "_call", fake_call)
    assert await client.get_lsp() == {"status": "未接入", "servers": []}
    assert calls == [("_lsp", {})]


async def test_get_lsp_fills_missing_fields(monkeypatch):
    """agentd 返回里缺 servers / status 时各自兜底，前端不会拿到 undefined。"""
    client = AcpClient()
    client._proc = object()

    async def fake_call(method, params):
        return {"result": {}}

    monkeypatch.setattr(client, "_call", fake_call)
    assert await client.get_lsp() == {"status": "未接入", "servers": []}


async def test_set_approval_requires_running_agent():
    """没起子进程就调 set：当场 AcpError，而不是发一帧永远没人回的请求。"""
    client = AcpClient()
    try:
        await client.set_approval("none")
    except acp_client.AcpError as exc:
        assert "尚未启动" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("应当抛 AcpError")
