"""协议层单元测试 —— 不启子进程、不装 Textual 也能跑。

重点测 reducer：把 ACP 事件流折叠成 Turn 的正确性。
这块错了 UI 必然错，而且在流式输出里靠肉眼极难发现——所以必须钉死。

另外还测**反向请求**（agent -> client 的 session/request_permission）：
它是请求，不回帧对方就永久阻塞，所以"有没有回""回成什么形状"都必须钉。
"""

from __future__ import annotations

import asyncio
import json

from forgeagent.acp_client import ERROR_MARK, AcpClient, Turn, _collect_text


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
    # plan 这类内核还没产出的事件，先记下来，别静默吞掉
    client, turn = AcpClient(), Turn()
    client._apply_update(turn, _notify("plan", "步骤"))
    assert turn.unknown == ["plan"]


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
    assert req.allow_ids == ["allow_once", "allow_session"]
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
