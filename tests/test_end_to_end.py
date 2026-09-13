"""端到端测试：真起子进程，验证 spawn + 握手 + 流式 + 清理。

这是唯一能证明"客户端真的能跟 ACP agent 说上话"的测试——
单元测试测不了子进程通信，而子进程通信恰恰是最容易出错的地方。

不依赖 Textual，也不依赖真实 agentd：用 tests/fake_agent.py 顶替。
所以这份测试在任何装了 Python 的机器上都能跑。
"""

from __future__ import annotations

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
