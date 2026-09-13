"""入口：`python -m forgeagent`

可用环境变量：
    FORGEAGENT_AGENT_CMD   拉起 agent 的命令，默认 "python -m agentd.server"
    FORGEAGENT_CWD         agent 的工作目录，默认当前目录
"""

from __future__ import annotations

from .app import ForgeAgentApp


def main() -> None:
    ForgeAgentApp().run()


if __name__ == "__main__":
    main()
