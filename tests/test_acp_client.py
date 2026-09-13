"""协议层单元测试 —— 不启子进程、不装 Textual 也能跑。

重点测 reducer：把 ACP 事件流折叠成 Turn 的正确性。
这块错了 UI 必然错，而且在流式输出里靠肉眼极难发现——所以必须钉死。
"""

from __future__ import annotations

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
    # 工具调用/计划这类事件内核还没产出，先记下来，别静默吞掉
    client, turn = AcpClient(), Turn()
    client._apply_update(turn, _notify("tool_call", "ls -la"))
    assert turn.unknown
    assert "tool_call" in turn.unknown[0]


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
