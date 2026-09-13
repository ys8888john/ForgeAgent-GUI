"""GUI 冒烟测试：真开一个窗口，连真 agentd，发一句话，然后自动关掉。

为什么需要它：
    pytest 只能测桥接层（bridge.py），测不到「pywebview 在这台机器上到底能不能
    把窗口开出来」。而后者恰恰最容易出问题 —— WebView2 缺运行时、Linux 缺
    WebKitGTK、macOS 用了非 framework 版 Python，都会让它失败。

用法（会弹出一个窗口，跑完自动关闭，不用管它）：

    .\\.venv\\Scripts\\python.exe scripts\\gui_smoke.py          # Windows
    ./.venv/bin/python scripts/gui_smoke.py                     # Linux / macOS

    --keep    跑完不关窗口，留着手动看
    --cwd     agentd 的工作目录

退出码 0 表示窗口能开、链路能通。
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import webview  # noqa: E402

from forgeagent.gui.bridge import Bridge  # noqa: E402
from forgeagent.gui.window import ASSETS, Api  # noqa: E402

QUESTION = "只回复八个字：GUI 打通了吗？"
HARD_TIMEOUT = 100  # 兜底：无论如何都要退出，不能把窗口挂死在用户桌面上


def _report(result: dict) -> int:
    """打印结论并返回退出码。"""
    print("\n" + "=" * 46)
    if "start" not in result:
        print("结果: 窗口没跑起来（pywebview 启动失败）")
        return 1
    if not result["start"].get("ok"):
        print(f"结果: agentd 连不上 -> {result['start'].get('error')}")
        return 1
    if "error" in result:
        print(f"结果: 异常 -> {result['error']}")
        return 1

    done = result.get("done", {})
    text = (result.get("text") or "").strip()
    print(f"窗口:  已创建并正常关闭")
    print(f"会话:  {result['start'].get('session', '')[:14]}")
    print(f"回复:  {text[:200] or '（空）'}")
    print(f"stop:  {done.get('stop', '?')}")
    ok = bool(text) and not done.get("error")
    print(f"结果:  {'GUI 链路打通' if ok else '空回复或出错'}")
    print("=" * 46)
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="GUI 冒烟测试")
    parser.add_argument("--cwd", default=os.getcwd())
    parser.add_argument("--keep", action="store_true", help="跑完不关窗口")
    args = parser.parse_args()

    bridge = Bridge(cwd=args.cwd)
    api = Api(bridge)
    result: dict = {}

    window = webview.create_window(
        "ForgeAgent 冒烟测试",
        url=(ASSETS / "index.html").as_uri(),
        js_api=api,
        width=900,
        height=620,
    )

    def watchdog() -> None:
        time.sleep(HARD_TIMEOUT)
        print("\n[超时] 强制退出（窗口可能被留在桌面上了）")
        sys.stdout.flush()
        os._exit(1)

    threading.Thread(target=watchdog, daemon=True).start()

    def finish() -> None:
        """结束。--keep 时留给窗口；否则直接结束进程。

        刻意不走 window.destroy()：从工作线程调用它在 Edge Chromium 后端上
        不保证生效，一旦没关掉，webview.start() 就会一直阻塞，把窗口挂死在
        用户桌面上。直接结束进程最可靠 —— 窗口是子进程，跟着一起没了。
        """
        code = _report(result)
        sys.stdout.flush()
        os._exit(code)

    def work() -> None:
        try:
            result["start"] = api.start()
            if not result["start"].get("ok"):
                return finish()

            api.send(QUESTION)

            deadline = time.time() + HARD_TIMEOUT - 10
            text = ""
            while time.time() < deadline:
                for ev in api.next_events(2.0):
                    kind = ev.get("type")
                    role = ev.get("role")
                    if kind == "delta" and role == "assistant":
                        text += ev["text"]
                    elif kind == "delta" and role == "error":
                        text += "[错误] " + ev["text"]
                    elif kind == "done":
                        result["text"] = text
                        result["done"] = ev
                        if args.keep:
                            return  # 留在窗口里，等用户自己关
                        return finish()
        except Exception as exc:  # noqa: BLE001 - 冒烟测试要结论，不是 traceback
            result["error"] = f"{type(exc).__name__}: {exc}"
        if args.keep:
            return
        finish()

    window.events.closed += bridge.close
    webview.start(func=work, args=())

    # 走到这儿说明是用户关了窗口（--keep 模式）
    return _report(result) if result else 1


if __name__ == "__main__":
    code = main()
    # pywebview / Edge Chromium 会留下后台线程，正常 return 时进程常常退不干净，
    # 退出码也就传不出来。这是无人值守脚本，直接 os._exit 保证退出码正确。
    # （真正常驻的 GUI 走 window.py，由 pywebview 自己收尾。）
    sys.stdout.flush()
    os._exit(code)
