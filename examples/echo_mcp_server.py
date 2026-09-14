"""示例 MCP server（stdio）：给 ForgeAgent 试用 MCP 用。

刻意不依赖 agentd，只用官方 `mcp` 包 —— 把它配进 mcp.json 之后，
模型就能调下面这两个工具。

    pip install mcp
    python examples/echo_mcp_server.py     # 手动跑没输出是正常的（等 stdio 输入）
"""

from __future__ import annotations

import datetime as _dt

from mcp.server.mcpserver import MCPServer

server = MCPServer("demo")


@server.tool()
def echo(text: str) -> str:
    """原样回显输入文本。"""
    return f"echo: {text}"


@server.tool()
def now(timezone: str = "local") -> str:
    """返回当前时间（ISO 8601）。timezone 目前只认 local。"""
    return _dt.datetime.now().isoformat(timespec="seconds")


if __name__ == "__main__":
    server.run("stdio")
