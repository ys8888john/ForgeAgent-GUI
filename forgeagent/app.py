"""Textual UI 层。

刻意只用 Textual 最经典、跨版本最稳的那几个 API：
compose / run_worker / query_one / on_input_submitted。
不追新语法，免得 Textual 一升级就碎。

一句话概括这层的职责：**渲染 AcpClient 产出的 Turn，别的什么都不管。**
协议、子进程、事件折叠全在 acp_client.py，这里一行都不碰。

有个坑值得单独记一笔：**所有 Static 都关掉了 markup。**
Textual 的 Static 默认 markup=True，会把 `[xxx]` 当样式标签解析。
LLM 输出里方括号太常见了（`[DONE]`、数组、Markdown 链接），
实测 `[DONE]` 会被整段吞掉、界面上什么都不显示 —— 属于"不报错但悄悄丢字"，
比崩溃还难查。所以这里统一走 rich.text.Text 手动上色，不用 markup。
"""

from __future__ import annotations

from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Footer, Header, Input, Static

from .acp_client import AcpClient, Turn

# 历史条目：(角色, 内容)。角色决定渲染样式。
# 存成结构化而不是渲染好的字符串，是为了让样式可以在最后统一决定——
# 中途拼好字符串的话，想给某一行换颜色就得重新解析一遍。
ROLE_USER = "user"
ROLE_TEXT = "text"
ROLE_THOUGHT = "thought"
ROLE_ERROR = "error"
ROLE_LOG = "log"


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
        self._history: list[tuple[str, str]] = []  # 已完成的轮次
        self._turn: Turn | None = None  # 当前正在流式生成的轮次
        self._show_log = False  # Ctrl+L 切换：对话 / agent stderr
        self._status = "启动中…"

    # ---- 布局 ----

    def compose(self) -> ComposeResult:
        yield Header()
        yield VerticalScroll(
            Static("", id="log", markup=False), id="log-scroll"
        )
        yield Static(self._status, id="status", markup=False)
        yield Input(placeholder="说点什么，回车发送（Ctrl+C 退出 / Ctrl+L 看日志）")
        yield Footer()

    async def on_mount(self) -> None:
        # 先把界面画出来，再去拉起 agent —— 否则启动那几秒是一片黑
        self._redraw()
        try:
            await self._client.start()
        except Exception as exc:  # 起不来要告诉用户，不能白屏
            self._status = "连接失败"
            self._history.append((ROLE_ERROR, f"启动失败：{exc}"))
            if self._client.stderr_lines:
                self._history.append((ROLE_LOG, "agent 日志末尾："))
                for line in self._client.stderr_lines[-10:]:
                    self._history.append((ROLE_LOG, line))
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

        self._history.append((ROLE_USER, text))
        self._turn = None
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
                self._turn = turn
                self._status = self._format_status(turn)
                self._redraw()
                # 滚到底
                self.query_one("#log-scroll", VerticalScroll).scroll_end(animate=False)
        except Exception as exc:
            self._history.append((ROLE_ERROR, f"出错：{type(exc).__name__}: {exc}"))
            self._status = "出错"
            self._turn = None
            self._redraw()
        else:
            # 收尾：把这一轮沉淀进历史，清空流式缓冲
            self._commit_turn()
            self._redraw()

    def _commit_turn(self) -> None:
        """把当前流式的 Turn 拆成历史条目。

        拆开而不是整块存，是因为一轮里可能既有正文又有错误，
        混成一条就没法分别上色了。
        """
        turn = self._turn
        if turn is None:
            return
        if turn.error:
            self._history.append((ROLE_ERROR, turn.error))
        if turn.thought:
            self._history.append((ROLE_THOUGHT, turn.thought))
        if turn.text:
            self._history.append((ROLE_TEXT, turn.text))
        if not (turn.error or turn.thought or turn.text):
            # 什么都没有 —— 通常是 agent 静默失败了，得留个痕迹，
            # 否则用户只看到自己发了句话、对面毫无反应。
            self._history.append((ROLE_ERROR, "（无回复）"))
        self._turn = None

    def _format_status(self, turn: Turn) -> str:
        tail = ""
        if self._client.stderr_lines:
            tail = f"   |   {self._client.stderr_lines[-1][:60]}"
        if turn.running:
            return f"生成中…{tail}"
        if turn.error:
            return f"出错了（Ctrl+L 看日志）{tail}"
        return f"stop={turn.stop_reason or '?'}{tail}"

    # ---- 渲染 ----

    @staticmethod
    def _styled(role: str, content: str) -> Text:
        """按角色给一段内容上色。"""
        if role == ROLE_USER:
            return Text(f"› {content}", style="bold cyan")
        if role == ROLE_ERROR:
            return Text(f"✗ {content}", style="bold red")
        if role == ROLE_THOUGHT:
            return Text(f"· (思考) {content}", style="dim")
        if role == ROLE_LOG:
            return Text(f"  {content}", style="dim")
        # ROLE_TEXT：正文。走富文本路径，就不套颜色了，让终端默认色显示
        return Text(f"· {content}")

    def _render_turn(self, turn: Turn) -> Text:
        """正在流式生成的那一轮。"""
        out = Text()
        first = True
        for role, content in (
            (ROLE_ERROR, turn.error),
            (ROLE_THOUGHT, turn.thought),
            (ROLE_TEXT, turn.text),
        ):
            if not content:
                continue
            if not first:
                out.append("\n")
            first = False
            out.append_text(self._styled(role, content))
        if first:  # 一个都没渲染到
            out.append("· …")
        return out

    def _body(self) -> Text:
        """组装整个日志区的内容。"""
        if self._show_log:
            out = Text("── agent 日志（Ctrl+L 返回）──\n", style="bold")
            out.append("\n".join(self._client.stderr_lines) or "(agent 暂无日志输出)")
            return out

        out = Text()
        first = True
        for role, content in self._history:
            if not first:
                out.append("\n\n")
            first = False
            out.append_text(self._styled(role, content))
        if self._turn is not None:
            if not first:
                out.append("\n\n")
            out.append_text(self._render_turn(self._turn))
        if not out.plain:
            out.append("(还没有对话)")
        return out

    def _redraw(self) -> None:
        """整体重绘。

        流式输出时这里是全量重设文本而不是追加——简单、不闪烁、
        也不会因为追加逻辑写错导致重复。短对话下性能完全够。
        """
        self.query_one("#log", Static).update(self._body())
        self.query_one("#status", Static).update(Text(self._status))
