"""GUI 桥接层 —— 纯 Python，不依赖 pywebview，可单测。

为什么需要这一层：
协议层 `AcpClient` 是 async 的（一次 prompt 会吐几十个流式事件），
而 pywebview 暴露给 JS 的 `js_api` 方法是同步的。两边节奏对不上，
中间必须有个缓冲把异步流摊平。

为什么用「长轮询拉模型」而不是 Python 主动推：
推要靠 `window.evaluate_js()`，在 EdgeChromium 后端上跨线程调用有已知的不稳定问题；
而 `js_api` 调用是 pywebview 自己派发到线程池里的，安全。
所以让 JS 端 `await` 一个「阻塞到有事件才返回」的 Python 方法 ——
既有推送的实时性，又完全不碰跨线程 UI 调用。
"""

from __future__ import annotations

import asyncio
import queue
import threading
from typing import Any

from ..acp_client import AcpClient, Turn

DEFAULT_POLL_TIMEOUT = 2.0  # JS 每次 await 的阻塞上限
_MAX_BATCH = 500  # 一次取走上限，防止生成极快时单批过大


class Bridge:
    """把 AcpClient 的异步事件流转成「JS 来取一批」的同步接口。

    事件都是普通 dict（直接 JSON 序列化给 JS），类型有：
        status  连接状态变化
        user    用户发出的消息（回声，让界面立刻有反馈）
        delta   流式增量，role ∈ assistant / thought / error
        tool    工具调用状态变化（含 id/title/kind/status/output）
        done    本轮结束，带 stop reason 和 error
    """

    def __init__(
        self,
        client: AcpClient | None = None,
        cwd: str | None = None,
        command: list[str] | None = None,
        mcp_servers: list[dict] | None = None,
    ) -> None:
        # client 可注入是为了单测：用一个假的 async client 就能测全部逻辑，
        # 不用真起 agentd 子进程。
        self._client = client if client is not None else AcpClient(
            command=command, cwd=cwd, mcp_servers=mcp_servers
        )
        self._events: queue.Queue[dict] = queue.Queue()
        self._commands: queue.Queue[dict] = queue.Queue()  # Python -> JS
        self._ui: dict = {}  # JS -> Python：界面自报的状态，外部可观测
        self._busy = False
        self._closed = threading.Event()

        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_loop, name="forgeagent-bridge", daemon=True
        )
        self._thread.start()

    # ---- 事件循环线程 ----

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _submit(self, coro: Any) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    def _emit(self, **event: Any) -> None:
        if not self._closed.is_set():
            self._events.put(event)

    # ---- 给 JS 用的接口 ----

    def start(self) -> dict:
        """拉起 agentd 并握手。同步阻塞（由 pywebview 派发到线程池，不卡界面）。"""
        self._emit(type="status", state="connecting", message="正在拉起 agentd…")
        try:
            self._submit(self._client.start()).result(timeout=30)
        except Exception as exc:  # noqa: BLE001 - 边界处统一转成状态事件
            msg = f"{type(exc).__name__}: {exc}"
            self._emit(
                type="status",
                state="error",
                message=msg,
                log=list(self._client.stderr_lines[-10:]),
            )
            return {"ok": False, "error": msg}

        self._emit(
            type="status",
            state="ready",
            message=f"已连接  会话 {self._client.session_id[:8]}",
        )
        return {"ok": True, "session": self._client.session_id}

    def new_session(self) -> dict:
        """开一个全新的 agentd 会话（对应 GUI 的「新对话」按钮）。

        走 ACP 的 session/new，拿到新 id 记到 client 上；后续 send 都用它。
        同步阻塞（由窗口层派发到线程池，不卡界面）。
        """
        if self._closed.is_set():
            return {"ok": False, "error": "已关闭"}
        try:
            sid = self._submit(self._client.new_session()).result(timeout=30)
        except Exception as exc:  # noqa: BLE001 - 边界处统一转成错误
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        self._busy = False
        self._emit(
            type="status",
            state="ready",
            message=f"新会话 {sid[:8]}",
        )
        return {"ok": True, "session": sid}

    def resume_session(self, session_id: str) -> dict:
        """续聊一个已有会话：**不**再开新会话，直接把 client 的 sessionId 换成旧的。

        为什么这样就够：agentd 内核在每次 handle() 开头都会从 SQLite 把整个历史
        load 进上下文（kernel/handle.py），所以只要 prompt 用的是旧 id，
        LLM 自然就接着上次的上下文聊——"续聊"在 agentd 侧本就免费，GUI 只需
        别去调 session/new、复用旧 id 即可。存在性校验在 server 层做（它握有
        会话库只读视图），这里只管切换。
        """
        if self._closed.is_set():
            return {"ok": False, "error": "已关闭"}
        if not session_id:
            return {"ok": False, "error": "缺少 session_id"}
        self._client.session_id = session_id
        self._busy = False
        self._emit(
            type="status",
            state="ready",
            message=f"已载入会话 {session_id[:8]}（可继续聊）",
        )
        return {"ok": True, "session": session_id}

    def send(self, text: str) -> dict:
        """发一轮。立即返回，真正的流式在后台线程跑。"""
        text = (text or "").strip()
        if not text:
            return {"ok": False, "error": "空消息"}
        if self._busy:
            return {"ok": False, "error": "上一轮还没结束"}

        self._busy = True
        self._emit(type="user", text=text)
        self._submit(self._stream(text))
        return {"ok": True}

    def next_events(self, timeout: float = DEFAULT_POLL_TIMEOUT) -> list[dict]:
        """阻塞到有事件可取，超时返回空列表。JS 端循环 await 这个方法。

        顺带一趟把待办命令捎回去（`{"type": "command", "cmd": ...}`）——
        省掉一次额外往返，也让「Python 让界面干点什么」走的是同一条安全通道。
        """
        if self._closed.is_set():
            return []
        out = self._drain_commands()
        if out:
            return out
        try:
            first = self._events.get(timeout=timeout)
        except queue.Empty:
            return []
        out = [first]
        while len(out) < _MAX_BATCH:
            try:
                out.append(self._events.get_nowait())
            except queue.Empty:
                break
        out.extend(self._drain_commands(_MAX_BATCH - len(out)))
        return out

    # ---- 反向通道：Python 让界面干活 / 界面自报状态 ----

    def push_command(self, cmd: dict) -> None:
        """让界面执行一个动作（如 {"action": "send", "text": "..."}）。

        不用 evaluate_js 推，原因同本文件开头：跨线程 UI 调用在 EdgeChromium
        上会死锁。命令排队，等 JS 下一次轮询取走。
        """
        if isinstance(cmd, dict):
            self._commands.put(cmd)

    def ui_report(self, state: dict) -> dict:
        """界面自报状态。外部（含冒烟测试）用 ui_state() 读。"""
        if isinstance(state, dict):
            self._ui.update(state)
        return {"ok": True}

    def ui_state(self) -> dict:
        return dict(self._ui)

    def _drain_commands(self, limit: int = _MAX_BATCH) -> list[dict]:
        out: list[dict] = []
        while len(out) < limit:
            try:
                out.append({"type": "command", "cmd": self._commands.get_nowait()})
            except queue.Empty:
                break
        return out

    def stderr_tail(self, n: int = 100) -> list[str]:
        """agentd 的日志（里面能看到自动选中了哪个模型）。"""
        return list(self._client.stderr_lines[-n:])

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        try:
            self._submit(self._client.close()).result(timeout=5)
        except Exception:  # noqa: BLE001 - 关闭路径上的失败不该抛出
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=3)

    # ---- 内部 ----

    async def _stream(self, text: str) -> None:
        """消费 prompt()，把增量拆成 delta 事件。

        只发新增的部分而不是每次发全量 —— 否则长回答的传输量是 O(n²)。
        """
        last = {"assistant": 0, "thought": 0, "error": 0}
        seen_tools: dict[str, tuple[str, str]] = {}
        turn: Turn | None = None
        try:
            async for t in self._client.prompt(text):
                turn = t
                for role, value in (
                    ("assistant", t.text),
                    ("thought", t.thought),
                    ("error", t.error),
                ):
                    if len(value) > last[role]:
                        self._emit(type="delta", role=role, text=value[last[role]:])
                        last[role] = len(value)
                # 工具卡片：状态有变就发一条（同一个 call_id 会被前端就地更新）
                for tool in t.tools:
                    snap = (tool.status, tool.output)
                    if seen_tools.get(tool.call_id) != snap:
                        seen_tools[tool.call_id] = snap
                        self._emit(
                            type="tool",
                            id=tool.call_id,
                            title=tool.title,
                            kind=tool.kind,
                            status=tool.status,
                            output=tool.output,
                        )
        except Exception as exc:  # noqa: BLE001 - 边界处统一转事件
            msg = f"{type(exc).__name__}: {exc}"
            self._emit(type="delta", role="error", text=msg)
            self._emit(type="done", stop="", error=msg)
            self._busy = False
            return

        self._emit(
            type="done",
            stop=(turn.stop_reason if turn else ""),
            error=(turn.error if turn else ""),
        )
        self._busy = False
