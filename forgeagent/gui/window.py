"""pywebview 窗口层。

只有这个文件 import webview —— bridge 层因此能脱离 GUI 单独测试。

跨平台说明（pywebview 三平台用的都是系统自带 WebView，不打包浏览器）：
    Windows  Edge WebView2。Win11 自带；Win10 需要装 Evergreen Runtime。
    macOS    WKWebKit，系统自带，无需额外安装。
    Linux    WebKitGTK + PyGObject，**最常见的问题就是这俩没装**，见 _LINUX_HINT。
"""

from __future__ import annotations

import sys
from pathlib import Path

from .bridge import Bridge

ASSETS = Path(__file__).parent / "assets"

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


class Api:
    """暴露给 JS 的接口，方法名直接就是 JS 里的 pywebview.api.xxx。

    刻意薄：只做转发，不含任何业务逻辑 —— 业务逻辑在 bridge.py 里，可单测。
    """

    def __init__(self, bridge: Bridge) -> None:
        self._bridge = bridge

    def start(self) -> dict:
        return self._bridge.start()

    def send(self, text: str) -> dict:
        return self._bridge.send(text)

    def next_events(self, timeout: float = 2.0) -> list[dict]:
        return self._bridge.next_events(timeout)

    def stderr_tail(self, n: int = 100) -> list[str]:
        return self._bridge.stderr_tail(n)


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
    """打开一个独立的 GUI 窗口。"""
    try:
        import webview
    except ImportError as exc:
        raise SystemExit(
            "没装 pywebview。装一下：pip install -e '.[gui]'"
        ) from exc

    import os

    bridge = Bridge(cwd=cwd, command=command)
    api = Api(bridge)

    # Path.as_uri() 三平台都能生成合法 file:// URL（Windows 上是 file:///D:/...）
    url = (ASSETS / "index.html").as_uri()
    window = webview.create_window(
        title,
        url=url,
        js_api=api,
        width=width,
        height=height,
        min_size=(640, 420),
    )

    def _bootstrap() -> None:
        # 窗口已经出来了再连 agentd —— 否则启动那 1 秒是白屏
        api.start()

    def _on_closed() -> None:
        # 务必带走 agentd 子进程，否则关窗后会漏一个 python 进程
        bridge.close()

    window.events.closed += _on_closed

    gui = os.environ.get("FORGEAGENT_GUI") or None  # 允许强制指定后端：qt / gtk / cef
    try:
        webview.start(func=_bootstrap, args=(), debug=debug, gui=gui)
    except Exception as exc:  # noqa: BLE001 - 启动失败要给人话，不是 traceback
        bridge.close()
        raise SystemExit(f"GUI 启动失败：{type(exc).__name__}: {exc}\n\n{_platform_hint()}")
