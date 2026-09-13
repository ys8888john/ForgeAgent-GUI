"""ForgeAgent —— 面向 ACP agent 的终端客户端。

分层是刻意的：

    acp_client.py   协议层，只依赖标准库 + 可选的 acp SDK
    app.py          UI 层，依赖 Textual

所以这里只导出协议层。**装不装 Textual 都能 `import forgeagent` 来用协议层**，
跑测试、写别的壳（比如将来换成 GUI）都不需要背 Textual 这个依赖。
"""

from .acp_client import AcpClient, AcpError, Turn

__all__ = ["AcpClient", "AcpError", "Turn"]
