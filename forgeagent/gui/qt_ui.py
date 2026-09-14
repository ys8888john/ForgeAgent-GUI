"""Qt 版聊天窗口 —— 操作系统原生控件，不碰任何浏览器内核。

为什么在 HTML 那套（assets/index.html + pywebview）之外还要再来一个：
    pywebview 用的是系统 WebView，Windows 上就是 Edge WebView2 = Chromium。
    在显卡驱动 / Hyper-V 有问题的机器上，WebView2 的**浏览器进程会直接崩**，
    报 "The instance of CoreWebView2 is no longer valid because the browser
    process crashed" —— 窗口能开、HTML 能加载、JS 跑两下就再无动静。
    Qt Widgets 走的是系统原生控件，没有浏览器内核，不受影响。

两个前端吃的是同一个后端 UiServer（server.py），界面自报状态的协议也一致
（POST /api/state），所以 scripts/gui_smoke.py 能同时验两套。

线程模型：
    HTTP 长轮询一次要阻塞 2 秒，绝不能放在 Qt 主线程里。
    所以后台线程只负责取事件，通过 Signal 扔回主线程 —— Qt 自己会把
    跨线程的 signal 排成队列交给主线程处理，控件只在主线程被碰。
"""

from __future__ import annotations

import threading
import time

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QFont, QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QTextBrowser,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .server import UiServer

_ROLE = {
    "user": ("你", "#2563eb"),
    "assistant": ("助手", "#16a34a"),
    "thought": ("思考", "#6b7280"),
    "error": ("错误", "#b42318"),
}

# 跨平台等宽字体：Consolas 是 Windows 专有，非 Windows 上会退化成非等宽。
# 用 "Monospace" 家族名 + 显式等宽 style hint，Qt 在各平台都能落到真正的等宽字体
# （Windows→Consolas，Linux→DejaVu Sans Mono，macOS→Menlo/SF Mono）。
_MONO = QFont("Monospace", 10)
_MONO.setStyleHint(QFont.StyleHint.Monospace)


class _Signals(QObject):
    """后台线程 -> 主线程的事件通道。"""

    event = Signal(dict)


class ChatWindow(QMainWindow):
    def __init__(self, server: UiServer, title: str = "ForgeAgent") -> None:
        super().__init__()
        self.server = server
        self.client = server.client()

        self._signals = _Signals()
        self._signals.event.connect(self._on_event)

        self._stop = threading.Event()
        self._busy = False
        self._cur_role: str | None = None
        self._cur_start = 0
        self._texts: dict[str, str] = {}
        self._status = "连接中…"

        self.setWindowTitle(title)
        self.resize(1000, 700)
        self._build()

        self._reader = threading.Thread(
            target=self._read_loop, name="forgeagent-qt-reader", daemon=True
        )
        self._reader.start()

        # 先自报一次「我起来了」，外部（冒烟测试）才知道前端真的跑起来了，
        # 而不是等第一个事件才冒泡 —— 否则连不上后端时外面一片漆黑，没法判断。
        self._report()

        self._log: QDialog | None = None

    # ---- 界面 ----

    def _build(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(6)

        head = QHBoxLayout()
        head.addWidget(QLabel("<b>ForgeAgent</b>"))
        self.status_label = QLabel(self._status)
        self.status_label.setStyleSheet("color:#6b7280")
        head.addWidget(self.status_label)
        head.addStretch(1)
        log_btn = QPushButton("agent 日志")
        log_btn.clicked.connect(self._toggle_log)
        head.addWidget(log_btn)
        layout.addLayout(head)

        self.view = QTextBrowser()
        self.view.setReadOnly(True)
        self.view.setOpenExternalLinks(True)
        layout.addWidget(self.view, 1)

        row = QHBoxLayout()
        self.input = QTextEdit()
        self.input.setFixedHeight(64)
        self.input.setPlaceholderText("说点什么…（Enter 发送，Shift+Enter 换行）")
        row.addWidget(self.input, 1)
        self.send_btn = QPushButton("发送")
        self.send_btn.setFixedWidth(80)
        self.send_btn.clicked.connect(self.send)
        row.addWidget(self.send_btn)
        layout.addLayout(row)

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt 的命名
        """Enter 发送、Shift+Enter 换行（焦点在输入框时）。"""
        if (
            self.input.hasFocus()
            and event.key() in (Qt.Key_Return, Qt.Key_Enter)
            and not (event.modifiers() & Qt.ShiftModifier)
        ):
            self.send()
            event.accept()
            return
        super().keyPressEvent(event)

    # ---- 发送 ----

    def send(self) -> None:
        if self._busy:
            return
        text = self.input.toPlainText().strip()
        if not text:
            return
        self.input.clear()
        try:
            self.client.post("/api/send", {"text": text})
        except Exception as exc:  # noqa: BLE001 - 界面上要看得见失败原因
            self._append("error", f"发送失败：{exc}")
            self._report()

    # ---- 后台取事件 ----

    def _read_loop(self) -> None:
        while not self._stop.is_set():
            try:
                batch = self.client.get("/api/events?timeout=2").get("events", [])
            except Exception:  # noqa: BLE001 - 服务还没起来 / 已关闭
                time.sleep(0.5)
                continue
            for ev in batch:
                self._signals.event.emit(ev)

    def _on_event(self, ev: dict) -> None:
        kind = ev.get("type")

        if kind == "status":
            self._status = ev.get("message") or ""
            self.status_label.setText(self._status)
            if ev.get("state") == "error":
                log = ev.get("log") or []
                self._append("error", self._status + ("\n" + "\n".join(log) if log else ""))
                self._report()
            return

        if kind == "user":
            self._busy = True
            self.send_btn.setEnabled(False)
            self._append("user", ev.get("text", ""))
            self._report()
            return

        if kind == "delta":
            self._append(ev.get("role", "assistant"), ev.get("text", ""))
            return

        if kind == "done":
            self._finish_block()
            self._busy = False
            self.send_btn.setEnabled(True)
            self._status = f"stop={ev.get('stop') or '?'}"
            self.status_label.setText(self._status)
            self._report()
            return

        if kind == "command":       # 让外部（冒烟测试）能驱动界面
            cmd = ev.get("cmd") or {}
            if cmd.get("action") == "send":
                self.input.setPlainText(cmd.get("text", ""))
                self.send()
            elif cmd.get("action") == "report":
                self._report()

    # ---- 渲染 ----

    def _append(self, role: str, text: str) -> None:
        if not text:
            return
        label, color = _ROLE.get(role, (role, "#16a34a"))

        cur = self.view.textCursor()
        cur.movePosition(QTextCursor.End)

        if role != self._cur_role:
            self._cur_role = role
            self._texts[role] = ""
            if self.view.document().characterCount() > 1:
                cur.insertBlock()
            self.view.setTextColor(color)
            cur.insertText(f"{label}\n")
            self._cur_start = cur.position()

        self._texts[role] = self._texts.get(role, "") + text
        self.view.setTextColor(color if role == "error" else Qt.black)
        cur.movePosition(QTextCursor.End)
        cur.insertText(text)
        self.view.setTextCursor(cur)
        self.view.ensureCursorVisible()

        if role == "user":
            self._last_user = self._texts[role]
        elif role == "assistant":
            self._last_assistant = self._texts[role]
        elif role == "error":
            self._last_error = self._texts[role]

    _last_user = ""
    _last_assistant = ""
    _last_error = ""

    def _finish_block(self) -> None:
        """一轮结束后把这一块按 Markdown 重排一遍（流式时是纯文本）。"""
        role = self._cur_role
        self._cur_role = None
        if role not in ("assistant", "thought", "error"):
            return
        text = self._texts.get(role, "")
        if not text.strip():
            return
        try:
            cur = QTextCursor(self.view.document())
            cur.setPosition(self._cur_start)
            cur.movePosition(QTextCursor.End, QTextCursor.KeepAnchor)
            cur.removeSelectedText()
            cur.insertMarkdown(text)
        except Exception:  # noqa: BLE001 - 渲染失败就保留纯文本，别把回复弄丢
            pass

    def text_of(self, role: str) -> str:
        return self._texts.get(role, "")

    # ---- 自报状态（和 HTML 前端同一套协议） ----

    def _report(self) -> None:
        snap = {
            "bridge": "ready",
            "status": self._status,
            "count": {
                "u": 1 if self._last_user else 0,
                "a": 1 if self._last_assistant else 0,
                "t": 0,
                "e": 1 if self._last_error else 0,
            },
            "user": self._last_user,
            "assistant": self._last_assistant,
            "error": self._last_error,
        }
        try:
            self.client.post("/api/state", snap)
        except Exception:  # noqa: BLE001 - 汇报失败不该影响主流程
            pass

    # ---- 日志面板 ----

    def _toggle_log(self) -> None:
        if self._log is not None:
            self._log.close()
            self._log = None
            return

        dlg = QDialog(self)
        dlg.setWindowTitle("agent 日志")
        dlg.resize(860, 500)
        box = QPlainTextEdit(dlg)
        box.setReadOnly(True)
        box.setFont(_MONO)
        box.setStyleSheet("background:#1f2124;color:#d6d8db")
        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(box)
        self._log = dlg
        dlg.finished.connect(lambda: setattr(self, "_log", None))

        timer = QTimer(dlg)

        def refresh() -> None:
            try:
                lines = self.client.get("/api/stderr?n=300").get("lines", [])
            except Exception as exc:  # noqa: BLE001
                lines = [f"读取失败：{exc}"]
            box.setPlainText("\n".join(lines) or "(agent 暂无日志输出)")

        timer.timeout.connect(refresh)
        timer.start(1500)
        refresh()
        dlg.show()

    # ---- 生命周期 ----

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt 的命名
        self._stop.set()
        super().closeEvent(event)


def launch_qt(
    *,
    title: str = "ForgeAgent",
    cwd: str | None = None,
    command: list[str] | None = None,
) -> None:
    """打开 Qt 窗口（阻塞到窗口关闭）。"""
    app = QApplication.instance() or QApplication([])

    server = UiServer(cwd=cwd, command=command).start()
    win = ChatWindow(server, title=title)
    win.show()

    # 窗口先出来，再连 agentd —— 否则启动那一秒是白屏
    threading.Thread(target=server.start_agent, name="forgeagent-boot", daemon=True).start()

    try:
        app.exec()
    finally:
        win._stop.set()  # noqa: SLF001 - 关闭路径，直接收尾
        server.stop()
