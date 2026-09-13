"""Textual UI 层。

刻意只用 Textual 最经典、跨版本最稳的那几个 API：
compose / run_worker / query_one / on_input_submitted。
不追新语法，免得 Textual 一升级就碎。

一句话概括这层的职责：**渲染 AcpClient 产出的 Turn，别的什么都不管。**
协议、子进程、事件折叠全在 acp_client.py，这里一行都不碰。
"""

from __future__ import annotations

from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Footer, Header, Input, Static

from .acp_client import AcpClient, Turn


class ForgeAgentApp(App[None]):
    """最小 ACP 终端客户端。"""

    TITLE = "ForgeAgent"
    CSS = """
    #log-scroll {
        height: 1fr;
        border: round $boost;
        padding: 0 1;
    }
    #log { width: 100%; }
    #status {
        height: 1;
        background: $boost;
        color: $text-muted;
        padding: 0 1;
    }
    Input { dock: bottom; }
    """

    BINDINGS = [
        ("ctrl+c", "quit", "退出"),
        ("ctrl+l", "toggle_log", "agent 日志"),
    ]

    def __init__(self, command: list[str] | None = None, cwd: str | None = None) -> None:
        super().__init__()
        self._client = AcpClient(command=command, cwd=cwd)
        self._history: list[str] = []  # 已完成的轮次
        self._buf = ""  # 当前轮正在流式生成的内容
        self._show_log = False  # Ctrl+L 切换：对话 / agent stderr
        self._status = "启动中…"

    # ---- 布局 ----

    def compose(self) -> ComposeResult:
        yield Header()
        yield VerticalScroll(Static("", id="log"), id="log-scroll")
        yield Static(self._status, id="status")
        yield Input(placeholder="说点什么，回车发送（Ctrl+C 退出 / Ctrl+L 看日志）")
        yield Footer()

    async def on_mount(self) -> None:
        # 先把界面画出来，再去拉起 agent —— 否则启动那几秒是一片黑
        self._redraw()
        try:
            await self._client.start()
        except Exception as exc:  # 起不来要告诉用户，不能白屏
            self._status = f"连接失败：{exc}"
            self._history.append(f"[启动失败] {exc}")
            if self._client.stderr_lines:
                self._history.append("agent 日志末尾：")
                self._history.extend(self._client.stderr_lines[-10:])
            self._buf = ""
            self._redraw()
        else:
            self._status = f"已连接  会话 {self._client.session_id[:8]}"
            self._redraw()

    async def on_unmount(self) -> None:
        # 务必把子进程带走，否则每次退出都漏一个 python 进程
        await self._client.close()

    # ---- 交互 ----

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text:
            return
        event.input.value = ""

        self._history.append(f"› {text}")
        self._buf = ""
        self._redraw()
        # exclusive=True：同一时刻只跑一轮，新的会顶掉旧的
        self.run_worker(self._ask(text), exclusive=True)

    def action_toggle_log(self) -> None:
        self._show_log = not self._show_log
        self._redraw()

    # ---- 内部 ----

    async def _ask(self, text: str) -> None:
        """消费 prompt() 产出的 Turn，每次都整体重绘。"""
        try:
            async for turn in self._client.prompt(text):
                self._buf = self._format_turn(turn)
                self._status = self._format_status(turn)
                self._redraw()
                # 滚到底
                self.query_one("#log-scroll", VerticalScroll).scroll_end(animate=False)
        except Exception as exc:
            self._buf = f"[出错] {exc}"
            self._status = "出错"
            self._redraw()
        else:
            # 收尾：把这一轮沉淀进历史，清空流式缓冲
            if self._buf:
                self._history.append(self._buf)
            self._buf = ""
            self._redraw()

    def _format_turn(self, turn: Turn) -> str:
        if turn.error:
            return f"· [错误] {turn.error}"
        parts: list[str] = []
        if turn.thought:
            parts.append(f"· (思考) {turn.thought}")
        if turn.text:
            parts.append(f"· {turn.text}")
        if not parts:
            parts.append("· …")
        return "\n".join(parts)

    def _format_status(self, turn: Turn) -> str:
        tail = ""
        if self._client.stderr_lines:
            tail = f"   |   {self._client.stderr_lines[-1][:60]}"
        if turn.running:
            return f"生成中…{tail}"
        return f"stop={turn.stop_reason or '?'}{tail}"

    def _redraw(self) -> None:
        """整体重绘。

        流式输出时这里是全量重设文本而不是追加——简单、不闪烁、
        也不会因为追加逻辑写错导致重复。短对话下性能完全够。
        """
        if self._show_log:
            body = "\n".join(self._client.stderr_lines) or "(agent 暂无日志输出)"
            body = f"── agent 日志（Ctrl+L 返回）──\n{body}"
        else:
            chunks = list(self._history)
            if self._buf:
                chunks.append(self._buf)
            body = "\n\n".join(chunks) or "(还没有对话)"
        self.query_one("#log", Static).update(body)
        self.query_one("#status", Static).update(self._status)
