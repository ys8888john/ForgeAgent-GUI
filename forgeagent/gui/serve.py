"""serve 形态：只起本机 UI 服务，不自己渲染任何窗口。

"谁来把界面画出来"和"后端怎么干活"本来是两件事。之前把它俩绑死在
pywebview 上，结果 WebView2 一崩，整个工具就跟着废了 —— 可从后端看它
一点毛病都没有。

拆开之后，同一个后端有两种载体：

    electron 模式 交给 Electron 壳开 Chromium 窗口（默认，自带浏览器、最像 WorkBuddy）
    serve 模式   什么都不画，只把 URL 交出去（本文件）

第三种的价值在于：**载体可以换，甚至可以换成一个我们控制不了的壳** ——
浏览器、Electron 宿主、IDE 的预览面板都行。它们崩了也不带走后端，
重开页面就能接回去；想换更好的壳（比如宿主自带的 Chromium）也不用改后端一行。

用法：
    forgeagent-gui --mode serve
    # stdout 会打一行 UI_READY <url>，把这个 url 交给任意 Web 载体打开即可
"""

from __future__ import annotations

import threading
import time

from .server import UiServer

_IDLE = 3600  # 主线程的休眠步长，没有实际意义，纯粹为了挂住进程


def run_serve(*, cwd: str | None = None, host: str = "127.0.0.1") -> None:
    """起服务 → 后台连 agentd → 打印 URL → 一直挂到被中断。"""
    server = UiServer(cwd=cwd, host=host).start()

    # 连 agentd 要握手（可能还要去 /api/tags 探模型），放到后台线程，
    # 别让"先把 URL 交出去"这件事被拖住。
    threading.Thread(target=server.start_agent, name="forgeagent-agent", daemon=True).start()

    # UI_READY 是给调用方解析用的锚点：一行一个 URL，前面那些日志都不算
    print(f"UI_READY {server.url}", flush=True)
    print("把上面的 URL 交给浏览器 / Electron / IDE 预览面板打开。Ctrl+C 收摊。", flush=True)

    try:
        while True:
            time.sleep(_IDLE)
    except KeyboardInterrupt:
        print("\n正在收摊…", flush=True)
        server.stop()
