"""GUI 冒烟测试：真起 agentd，真开窗口（可选），**走真实前端路径**发一句话。

三种模式：

    # 1) Qt 窗口（默认）：原生控件，不碰浏览器内核，最稳
    .\\.venv\\Scripts\\python.exe scripts\\gui_smoke.py --cwd D:/workspace/Agentd

    # 2) pywebview 窗口（HTML 界面）：机器上 WebView2 正常时才指望它过
    .\\.venv\\Scripts\\python.exe scripts\\gui_smoke.py --webview --cwd D:/workspace/Agentd

    # 3) 无窗口：只跑 本机 HTTP 服务 + bridge + agentd，不碰 GUI
    .\\.venv\\Scripts\\python.exe scripts\\gui_smoke.py --headless --cwd D:/workspace/Agentd

    --keep     跑完不关窗口，留着手动看
    --cwd      agentd 的工作目录

为什么窗口模式下不在 Python 侧代发消息：
    第一版就是直接调 api.send()，Python 全绿，界面上却一直显示「连不上后端」——
    Python 层通 ≠ 界面通。现在改成：Python 只下发一条命令，让**页面自己**
    走 submit() -> POST /api/send，再读界面自报的状态看气泡出没出来。

为什么不用 window.evaluate_js() 读 DOM：
    在 EdgeChromium 后端上它会死锁（pywebview 在 continuation 里 json.loads
    失败就不放信号量，semaphore.acquire() 永远等不到）。所以改成界面自己把
    DOM 摘要 POST /api/state 回来。

退出码 0 表示：服务能起 + 页面 JS 跑了 + 真的渲染出了回复。
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from forgeagent.gui.server import UiServer  # noqa: E402

QUESTION = "只回复八个字：GUI 打通了吗？"
HARD_TIMEOUT = 110  # 兜底：无论如何都要退出，不能把窗口挂死在用户桌面上


def _report(result: dict, mode: str) -> int:
    print("\n" + "=" * 52)
    start = result.get("start")
    if start is None:
        print("结果: agentd 没连上（server 都没起来）")
        return 1
    if not start.get("ok"):
        print(f"结果: agentd 连不上 -> {start.get('error')}")
        return 1
    if "error" in result:
        print(f"结果: 异常 -> {result['error']}")
        return 1

    ui = result.get("ui", {})
    count = ui.get("count") or {}
    bridge = ui.get("bridge", "")
    status = (ui.get("status") or "").strip()
    reply = (ui.get("assistant") or "").strip()
    err = (ui.get("error") or "").strip()
    headless = mode == "headless"

    print(f"模式:    { {'headless': '无窗口（headless）', 'qt': 'Qt 原生窗口', 'webview': 'pywebview HTML 窗口'}[mode] }")
    print(f"会话:    {start.get('session', '')[:14]}")
    if not headless:
        print(f"页面:    {bridge or '（未自报）'}")
        print(f"状态栏:  {status or '（空）'}")
        print(f"气泡:    {count or '（页面没汇报）'}")
        print(f"用户:    {(ui.get('user') or '')[:60]}")
    print(f"回复:    {reply[:200] or '（空）'}")

    if headless:
        ok = bool(reply) and not result.get("turn_error")
        print(f"结果:    {'OK — 服务 -> bridge -> agentd -> ollama 通' if ok else 'FAIL'}")
        print("=" * 52)
        return 0 if ok else 1

    if not ui:
        print("结果:    FAIL — 界面一次都没自报（前端压根没跑起来）")
        print("=" * 52)
        return 1
    if bridge != "ready":
        print(f"结果:    FAIL — 前端没连上本机服务（bridge={bridge or '空'}）")
        print("=" * 52)
        return 1
    if not count.get("u"):
        print("结果:    FAIL — 发送命令没走通（界面没有用户气泡）")
        print("=" * 52)
        return 1
    if err:
        print(f"结果:    FAIL — 界面上报了错误：{err[:200]}")
        print("=" * 52)
        return 1
    if not reply:
        print("结果:    FAIL — 没有渲染出回复气泡")
        print("=" * 52)
        return 1

    print(f"结果:    OK — 前端 -> 后端 -> ollama 全链路打通（{status}）")
    print("=" * 52)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="GUI 冒烟测试")
    parser.add_argument("--cwd", default=os.getcwd())
    parser.add_argument("--headless", action="store_true", help="不开窗口，只跑服务链路")
    parser.add_argument("--webview", action="store_true", help="用 pywebview(HTML) 而非 Tk")
    parser.add_argument("--keep", action="store_true", help="跑完不关窗口")
    args = parser.parse_args()

    mode = "headless" if args.headless else ("webview" if args.webview else "qt")
    server = UiServer(cwd=args.cwd).start()
    api = server.client()
    result: dict = {"start": {}, "ui": {}}

    def watchdog() -> None:
        time.sleep(HARD_TIMEOUT)
        print("\n[超时] 强制退出（窗口可能被留在桌面上了）")
        sys.stdout.flush()
        os._exit(1)

    threading.Thread(target=watchdog, daemon=True).start()

    def finish() -> int:
        code = _report(result, mode)
        sys.stdout.flush()
        os._exit(code)

    def wait_until(pred, deadline: float, tick: float = 0.2) -> dict:
        while time.time() < deadline:
            try:
                snap = api.get("/api/state").get("state") or {}
            except (urllib.error.URLError, OSError):
                snap = {}
            if snap and pred(snap):
                return snap
            time.sleep(tick)
        return snap

    # ---- 无窗口模式：直接驱动 HTTP 层 ----

    def run_headless() -> int:
        result["start"] = server.start_agent()
        if not result["start"].get("ok"):
            return _report(result, "headless")

        api.post("/api/send", {"text": QUESTION})
        text, turn_error = "", ""
        deadline = time.time() + HARD_TIMEOUT - 20
        while time.time() < deadline:
            for ev in api.get("/api/events?timeout=2").get("events", []):
                if ev.get("type") == "delta" and ev.get("role") == "assistant":
                    text += ev["text"]
                elif ev.get("type") == "delta" and ev.get("role") == "error":
                    text += "[错误] " + ev["text"]
                elif ev.get("type") == "done":
                    result["ui"] = {"assistant": text}
                    result["turn_error"] = ev.get("error") or ""
                    return _report(result, "headless")
        result["ui"] = {"assistant": text}
        return _report(result, "headless")

    if args.headless:
        return run_headless()

    # ---- Qt 窗口模式（默认）----

    def run_qt() -> int:
        from PySide6.QtWidgets import QApplication

        from forgeagent.gui.qt_ui import ChatWindow

        app = QApplication.instance() or QApplication([])
        win = ChatWindow(server, title="ForgeAgent 冒烟测试")
        win.show()

        result["start"] = server.start_agent()
        if not result["start"].get("ok"):
            return finish()

        def wait_qt(pred, deadline: float) -> dict:
            """不进 app.exec()，手动转 Qt 事件循环 —— 这样才能边等边检查。"""
            snap: dict = {}
            while time.time() < deadline:
                app.processEvents()
                try:
                    snap = api.get("/api/state").get("state") or {}
                except (urllib.error.URLError, OSError):
                    snap = {}
                if snap and pred(snap):
                    return snap
                time.sleep(0.05)
            return snap

        result["ui"] = wait_qt(lambda s: s.get("bridge") == "ready", time.time() + 25)
        if result["ui"].get("bridge") != "ready":
            return finish()

        # 让界面自己发（走 send() -> POST /api/send，不是 Python 代发）
        api.post("/api/command", {"action": "send", "text": QUESTION})

        result["ui"] = wait_qt(
            lambda s: (s.get("status") or "").startswith("stop=")
            or (s.get("error") or "").strip(),
            time.time() + HARD_TIMEOUT - 30,
        )
        if args.keep:
            app.exec()
            return 0
        return finish()

    if mode == "qt":
        return run_qt()

    # ---- pywebview（HTML）窗口模式 ----

    import webview

    window = webview.create_window(
        "ForgeAgent 冒烟测试",
        url=server.url,
        width=900,
        height=620,
    )

    def work() -> None:
        try:
            result["start"] = server.start_agent()
            if not result["start"].get("ok"):
                return finish()

            # 1) 等页面自报「连上了」 —— 证明 JS 真的跑起来了。
            #    窗口在后台/被遮挡时 WebView2 会推迟加载甚至挂起渲染进程，
            #    所以先显式把它显示出来；等不到就重载一次再等一轮。
            try:
                window.show()
            except Exception:  # noqa: BLE001
                pass

            result["ui"] = wait_until(lambda s: s.get("bridge") == "ready", time.time() + 20)
            if result["ui"].get("bridge") != "ready":
                print("[提示] 20 秒内页面没自报，重载一次再等")
                sys.stdout.flush()
                try:
                    window.load_url(server.url)
                except Exception as exc:  # noqa: BLE001
                    print(f"[提示] 重载失败: {exc}")
                result["ui"] = wait_until(
                    lambda s: s.get("bridge") == "ready", time.time() + 20
                )
            if result["ui"].get("bridge") != "ready":
                return finish()

            # 2) 让页面自己发（走 submit() -> POST /api/send，不是 Python 代发）
            api.post("/api/command", {"action": "send", "text": QUESTION})

            # 3) 等状态栏变成 stop=xxx，或页面冒出错误气泡
            result["ui"] = wait_until(
                lambda s: (s.get("status") or "").startswith("stop=")
                or (s.get("error") or "").strip(),
                time.time() + HARD_TIMEOUT - 30,
            )
            if args.keep:
                return
            return finish()
        except Exception as exc:  # noqa: BLE001 - 冒烟测试要结论，不是 traceback
            result["error"] = f"{type(exc).__name__}: {exc}"
        if args.keep:
            return
        finish()

    window.events.closed += server.stop
    webview.start(func=work, args=())

    return _report(result, mode) if result.get("start") else 1


if __name__ == "__main__":
    code = main()
    # pywebview / Edge Chromium 会留下后台线程，正常 return 时进程常常退不干净，
    # 退出码也就传不出来。这是无人值守脚本，直接 os._exit 保证退出码正确。
    sys.stdout.flush()
    os._exit(code)
