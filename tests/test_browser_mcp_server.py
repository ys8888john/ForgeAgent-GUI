"""examples/browser_mcp_server.py 的全链路测试（--stub 模式，不真开浏览器）。

覆盖：
- 工具清单（browser__open_url / browser__open_search）；
- open_url：合法 http/https 放行并返回将打开的 URL；裸域名/file:/javascript:
  与空白注入被拒；
- open_search：引擎白名单与模板（bing/google/duckduckgo）、空查询拒绝、
  未知引擎拒绝；
- --stub 语义：所有"打开"都只返回将打开的 URL（测试/CI 不拉浏览器）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("agentd", reason="需要 agentd 包（McpHub 在 agentd.kernel.mcp）")

from agentd.kernel.mcp import McpHub  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
AGENTD_VENV_PY = ROOT.parent / "Agentd" / ".venv" / "bin" / "python"

pytestmark = pytest.mark.skipif(
    not AGENTD_VENV_PY.is_file(),
    reason=f"需要 Agentd venv 的解释器才能起 MCP server 子进程：{AGENTD_VENV_PY}",
)


def _config() -> dict:
    return {
        "name": "browser",
        "command": str(AGENTD_VENV_PY),
        "args": [str(ROOT / "examples" / "browser_mcp_server.py"), "--stub"],
    }


async def test_browser_tools_roundtrip(tmp_path):
    async with McpHub([_config()], cwd=str(tmp_path)) as hub:
        tools = {item["function"]["name"] for item in hub.tool_schema()}
        assert tools == {"browser__open_url", "browser__open_search"}

        out = await hub.call("browser__open_url", json.dumps({"url": "https://example.com/page?a=1"}))
        assert out == "[stub] 将打开：https://example.com/page?a=1"

        out = await hub.call("browser__open_search", json.dumps({"query": "agent client protocol"}))
        assert out == "[stub] 将打开：https://www.bing.com/search?q=agent+client+protocol"


async def test_browser_url_validation(tmp_path):
    async with McpHub([_config()], cwd=str(tmp_path)) as hub:
        for bad in ("example.com/裸域名", "file:///etc/passwd", "javascript:alert(1)", "http://a b", " "):
            out = await hub.call("browser__open_url", json.dumps({"url": bad}))
            assert out.startswith("[错误]"), f"{bad!r} 应被拒: {out}"


async def test_open_search_engines_and_rejections(tmp_path):
    async with McpHub([_config()], cwd=str(tmp_path)) as hub:
        out = await hub.call(
            "browser__open_search", json.dumps({"query": "mcp server", "engine": "google"})
        )
        assert out == "[stub] 将打开：https://www.google.com/search?q=mcp+server"

        out = await hub.call(
            "browser__open_search", json.dumps({"query": "mcp server", "engine": "duckduckgo"})
        )
        assert "duckduckgo.com" in out

        out = await hub.call("browser__open_search", json.dumps({"query": "  "}))
        assert out.startswith("[错误]") and "搜索词" in out

        out = await hub.call(
            "browser__open_search", json.dumps({"query": "x", "engine": "baidu"})
        )
        assert out.startswith("[错误]") and "baidu" in out
