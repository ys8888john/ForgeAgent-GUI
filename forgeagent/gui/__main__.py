"""入口：python -m forgeagent.gui（或装完后直接 forgeagent-gui）

两套前端，吃同一个后端（server.py）：
    qt（默认）      Qt 原生控件，不碰浏览器内核，最稳。
    webview         HTML 界面（assets/index.html），更漂亮，但依赖系统 WebView。
                    Windows 上是 Edge WebView2 = Chromium；显卡驱动有问题的机器
                    上浏览器进程会崩，崩了就用回 qt。

    FORGEAGENT_GUI_MODE=webview forgeagent-gui    也能切。
"""

from __future__ import annotations

import argparse
import os


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="forgeagent-gui", description="ForgeAgent 图形界面（独立窗口）"
    )
    parser.add_argument("--cwd", default=None, help="agentd 的工作目录，默认当前目录")
    parser.add_argument("--width", type=int, default=1040)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument(
        "--mode",
        default=os.environ.get("FORGEAGENT_GUI_MODE", "qt"),
        choices=("qt", "webview"),
        help="前端：qt（默认，Qt 原生控件）/ webview（HTML 界面）",
    )
    parser.add_argument("--debug", action="store_true", help="打开 WebView 开发者工具")
    args = parser.parse_args()

    if args.mode == "qt":
        from .qt_ui import launch_qt

        launch_qt(cwd=args.cwd)
        return

    from .window import launch

    launch(cwd=args.cwd, width=args.width, height=args.height, debug=args.debug)


if __name__ == "__main__":
    main()
