"""pywebview 窗口层。

只有这个文件 import webview —— server / bridge 层因此能脱离 GUI 单独测试。

职责很薄：起一个本机 UI 服务（server.py），把它的 URL 交给 pywebview 开个窗口，
关窗时把 agentd 子进程带走。页面跟 Python 之间走 HTTP，**不用 pywebview 的
js_api** —— 原因见 server.py 模块注释。

跨平台说明（pywebview 三平台用的都是系统自带 WebView，不打包浏览器）：
    Windows  Edge WebView2。Win11 自带；Win10 需要装 Evergreen Runtime。
    macOS    WKWebKit，系统自带，无需额外安装。
    Linux    WebKitGTK + PyGObject，**最常见的问题就是这俩没装**，见 _LINUX_HINT。
"""

from __future__ import annotations

import os
import sys

from .server import UiServer

_LINUX_HINT = """\
Linux 上启动 GUI 失败，通常是缺 WebKitGTK / PyGObject。按发行版装：

  Debian/Ubuntu:  sudo apt install python3-gi python3-gi-cairo gir1.2-webkit2-4.1
  Fedora:         sudo dnf install python3-gobject webkit2gtk4.1
  Arch:           sudo pacman -S python-gobject webkit2gtk-4.1

装完仍失败的话，试试换 GTK 后端：FORGEAGENT_GUI=gtk forgeagent-gui
"""

_MACOS_HINT = """\
macOS 上启动 GUI 失败。若是从虚拟环境运行，确认用的是 framework 版 Python
（python.org 官方安装包或 brew 的 python），不要用精简版。
"""

_WINDOWS_HINT = """\
Windows 上启动 GUI 失败，通常是缺 Edge WebView2 Runtime。
Win11 自带；Win10 请装 Evergreen Bootstrapper：
https://developer.microsoft.com/microsoft-edge/webview2/
"""


def _platform_hint() -> str:
    if sys.platform == "linux":
        return _LINUX_HINT
    if sys.platform == "darwin":
        return _MACOS_HINT
    return _WINDOWS_HINT


def launch(
    *,
    title: str = "ForgeAgent",
    width: int = 1040,
    height: int = 720,
    cwd: str | None = None,
    command: list[str] | None = None,
    debug: bool = False,
) -> None:
    """打开一个独立的 GUI 窗口（阻塞到窗口关闭）。"""
    try:
        import webview
    except ImportError as exc:
        raise SystemExit("没装 pywebview。装一下：pip install -e '.[gui]'") from exc

    server = UiServer(cwd=cwd, command=command).start()
    print(f"[forgeagent] 本机 UI 服务: http://127.0.0.1:{server.port}/")

    window = webview.create_window(
        title,
        url=server.url,
        width=width,
        height=height,
        min_size=(640, 420),
    )

    # 关窗务必带走 agentd 子进程，否则会漏一个 python 进程在后台
    window.events.closed += server.stop

    gui = os.environ.get("FORGEAGENT_GUI") or None  # 允许强制指定后端：qt / gtk / cef
    try:
        # 窗口先出来，再去连 agentd —— 否则启动那一秒是白屏
        webview.start(func=server.start_agent, args=(), debug=debug, gui=gui)
    except Exception as exc:  # noqa: BLE001 - 启动失败要给人话，不是 traceback
        server.stop()
        raise SystemExit(f"GUI 启动失败：{type(exc).__name__}: {exc}\n\n{_platform_hint()}")
