"""端到端测试：真起子进程，验证 spawn + 握手 + 流式 + 清理。

这是唯一能证明"客户端真的能跟 ACP agent 说上话"的测试——
单元测试测不了子进程通信，而子进程通信恰恰是最容易出错的地方。

不依赖 Textual，也不依赖真实 agentd：用 tests/fake_agent.py 顶替。
所以这份测试在任何装了 Python 的机器上都能跑。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from forgeagent.acp_client import AcpClient, AcpError

FAKE_AGENT = Path(__file__).parent / "fake_agent.py"


@pytest.fixture
async def client() -> AcpClient:
    c = AcpClient(command=[sys.executable, str(FAKE_AGENT)])
    await c.start()
    yield c
    await c.close()


async def test_handshake_returns_session_id(client: AcpClient):
    assert client.session_id == "test-session-0001"


async def test_prompt_streams_and_accumulates(client: AcpClient):
    turns = [turn async for turn in client.prompt("嗨")]
    assert turns, "至少要 yield 一次"
    # 同一个对象被就地更新，最后一次应为完整内容
    assert turns[-1].text == "你好世界"


async def test_thought_is_separated_from_message(client: AcpClient):
    turns = [turn async for turn in client.prompt("嗨")]
    assert turns[-1].thought == "我在想"


async def test_stop_reason_is_captured(client: AcpClient):
    turns = [turn async for turn in client.prompt("嗨")]
    assert turns[-1].stop_reason == "end_turn"
    assert turns[-1].running is False


async def test_stderr_is_captured_not_leaked(client: AcpClient):
    # 日志必须进缓冲，不能打到终端（Textual 会因此撕裂界面）
    assert any("fake-agent" in line for line in client.stderr_lines)


async def test_close_terminates_subprocess():
    c = AcpClient(command=[sys.executable, str(FAKE_AGENT)])
    await c.start()
    await c.close()
    assert c._proc is None


async def test_dead_agent_raises_acp_error():
    """agent 起不来时必须明确报错，不能卡在超时上。"""
    c = AcpClient(command=[sys.executable, "-c", "import sys; sys.exit(3)"])
    with pytest.raises(AcpError):
        await c.start()
    await c.close()


# ---- 反向请求：审批往返（agent 真的会停下来等我们回帧）----


async def test_permission_round_trip_allow(client: AcpClient):
    """整条往返：agent 发请求 → 客户端问"界面" → 界面答 → agent 拿到答案继续。

    假 agent 会把答案写进 thought 通道（`[perm] allow`），所以"客户端到底回了什么"
    是可断言的 —— 光看"没报错"证明不了任何事。
    """
    box: dict = {}

    def hook(req):
        box["req"] = req
        # 真实界面是用户点按钮；这里模拟"立刻点允许一次"
        asyncio.ensure_future(client.answer_permission(req.request_id, req.allow_ids[0]))

    client.on_permission = hook
    turns = [t async for t in client.prompt("请确认一下")]
    assert "[perm] allow" in turns[-1].thought

    req = box["req"]
    assert req.title == "run_command"
    assert req.kind == "execute"
    assert "echo hi" in req.detail
    assert req.call_id == "fake-call-1"
    assert {o["optionId"] for o in req.options} == {
        "allow_once",
        "allow_session",
        "reject",
    }


async def test_permission_round_trip_deny_when_ui_rejects(client: AcpClient):
    def hook(req):
        asyncio.ensure_future(client.answer_permission(req.request_id, "reject"))

    client.on_permission = hook
    turns = [t async for t in client.prompt("请确认一下")]
    assert "[perm] deny" in turns[-1].thought


async def test_permission_is_denied_when_no_ui_handler(client: AcpClient):
    """没挂处理器（等于没界面）时必须自动拒绝。

    这是整个审批机制的安全底线：宁可什么都不做，也不能在无人确认的情况下放行。
    """
    turns = [t async for t in client.prompt("请确认一下")]
    assert "[perm] deny" in turns[-1].thought
    # 而且这一轮本身要正常走完，不能因为审批被拒就整轮卡死
    assert turns[-1].text == "你好世界"
