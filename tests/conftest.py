"""pytest 全局配置。

目前只做一件事：关掉 WSL 保活会话。

UiServer 在 active profile 是 WSL 的 ollama 时会挂一个 `wsl -e sleep
infinity`，好让发行区不被空闲回收（见 server.py 的 _WslKeepalive）。
单测里 UiServer 会被构造几十次，每次留一个睡到天荒地老的 wsl.exe —— 进程
泄漏是一方面，更要命的是它会把开发机上真实的 WSL 状态搅进去，让本来纯粹
的单元测试变得依赖环境。这里直接从源头关掉。
"""

import os

os.environ.setdefault("FORGEAGENT_NO_WSL_KEEPALIVE", "1")