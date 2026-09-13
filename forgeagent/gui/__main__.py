"""入口：python -m forgeagent.gui（或装完后直接 forgeagent-gui）"""

from __future__ import annotations

import argparse

from .window import launch


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="forgeagent-gui", description="ForgeAgent 图形界面（独立窗口）"
    )
    parser.add_argument("--cwd", default=None, help="agentd 的工作目录，默认当前目录")
    parser.add_argument("--width", type=int, default=1040)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument("--debug", action="store_true", help="打开 WebView 开发者工具")
    args = parser.parse_args()

    launch(cwd=args.cwd, width=args.width, height=args.height, debug=args.debug)


if __name__ == "__main__":
    main()
