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

from ..acp_client import AcpClient, PermissionRequest, Turn

DEFAULT_POLL_TIMEOUT = 2.0  # JS 每次 await 的阻塞上限
_MAX_BATCH = 500  # 一次取走上限，防止生成极快时单批过大


class Bridge:
    """把 AcpClient 的异步事件流转成「JS 来取一批」的同步接口。

    事件都是普通 dict（直接 JSON 序列化给 JS），类型有：
        status     连接状态变化
        user       用户发出的消息（回声，让界面立刻有反馈）
        delta      流式增量，role ∈ assistant / thought / error
        tool       工具调用状态变化（含 id/title/kind/status/output）
        permission agent 请求审批（带 id/tool/detail/options），界面必须回
        done       本轮结束，带 stop reason 和 error
    """

    def __init__(
        self,
        client: AcpClient | None = None,
        cwd: str | None = None,
        command: list[str] | None = None,
        mcp_servers: list[dict] | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        # client 可注入是为了单测：用一个假的 async client 就能测全部逻辑，
        # 不用真起 agentd 子进程。
        # env 是初始注入 agentd 子进程的变量组（GUI 的 active 模型 profile），
        # 只在"不注入 client"的默认路径上生效 —— 注入 client 的测试自己管 env。
        self._client = client if client is not None else AcpClient(
            command=command, cwd=cwd, mcp_servers=mcp_servers, env=env
        )
        self._events: queue.Queue[dict] = queue.Queue()
        self._commands: queue.Queue[dict] = queue.Queue()  # Python -> JS
        self._ui: dict = {}  # JS -> Python：界面自报的状态，外部可观测
        self._busy = False
        self._closed = threading.Event()

        # 审批请求来自协议层的读取协程（跑在本 bridge 的事件循环线程里），
        # 而 _events 是 queue.Queue（线程安全），所以直接塞进去是安全的。
        if hasattr(self._client, "on_permission"):
            self._client.on_permission = self._on_permission

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

    def restart(self, env: dict[str, str] | None = None) -> dict:
        """换环境变量重启 agentd 子进程（切换模型用），尽量保住当前会话 id。

        流程：关旧进程 → 给 client 换 env → 重新 start（握手 + 新 session）→
        如果之前有会话 id，把 client.session_id 切回去（agentd 从 SQLite 载入
        历史，模型换了上下文还在）。

        失败处理：不抛异常。返回 {"ok": False, "error": ...}，界面照常显示；
        旧 client 已经关了，用户重试即可（Bridge 层面没死锁风险）。
        """
        if self._closed.is_set():
            return {"ok": False, "error": "已关闭"}
        if self._busy:
            return {"ok": False, "error": "生成中，稍后再切换"}

        old_session = self._client.session_id or None
        # 先清空：如果 start 失败，至少别让一个已死进程的 session_id 被继续用
        self._client.session_id = ""

        try:
            self._submit(self._client.close()).result(timeout=10)
        except Exception:  # noqa: BLE001 - 关不掉就硬换；子进程是 daemon 关系
            pass
        set_env = getattr(self._client, "set_env", None)
        if callable(set_env):
            set_env(dict(env) if env else None)
        else:  # 旧/精简版 client：直接换字段（合并语义在 client 里）
            self._client._env = dict(env) if env else None

        self._emit(type="status", state="connecting", message="正在切换模型…")
        try:
            self._submit(self._client.start()).result(timeout=30)
        except Exception as exc:  # noqa: BLE001 - 边界处统一转状态事件
            msg = f"{type(exc).__name__}: {exc}"
            self._emit(
                type="status",
                state="error",
                message=msg,
                log=list(self._client.stderr_lines[-10:]),
            )
            return {"ok": False, "error": msg}

        # start() 会开新会话；把旧 id 切回去（存在的话）—— 历史在 SQLite 里，
        # 新进程照样能接着聊。切不回去（库被清了）就用新会话，不硬拗。
        target = old_session
        if target:
            self._client.session_id = target
            message = f"已切到新模型，继续会话 {target[:8]}"
        else:
            target = self._client.session_id
            message = f"已切到新模型，新会话 {target[:8]}"
        self._busy = False
        self._emit(type="session", session_id=target)
        self._emit(type="status", state="ready", message=message)
        return {"ok": True, "session": target, "model_env": bool(env)}

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

    # ---- 审批：agent -> 界面 -> agent 的往返 ----

    def _on_permission(self, req: PermissionRequest) -> None:
        """协议层收到审批请求 → 变成一条 permission 事件给界面。

        这里**不阻塞**：事件入队就返回，答案稍后由界面经 answer_permission 送回。
        协议层那边有个 Future 在等，两边谁都不卡住谁。
        """
        self._emit(
            type="permission",
            id=req.request_id,
            call_id=req.call_id,
            title=req.title,
            kind=req.kind,
            detail=req.detail,
            options=[
                {
                    "id": str(o.get("optionId") or ""),
                    "label": str(o.get("name") or o.get("optionId") or ""),
                    "kind": str(o.get("kind") or ""),
                }
                for o in req.options
            ],
        )

    def answer_permission(self, request_id: Any, option_id: str) -> dict:
        """界面点了某个选项；关掉弹窗/超时传空串（协议层会翻成 ACP 的 cancelled）。"""
        if self._closed.is_set():
            return {"ok": False, "error": "已关闭"}
        answer = getattr(self._client, "answer_permission", None)
        if not callable(answer):
            # 老/精简版 client 没有审批能力。给一句人话，别让它变成 AttributeError。
            return {"ok": False, "error": "当前客户端不支持审批"}
        try:
            rid = int(request_id)
        except (TypeError, ValueError):
            return {"ok": False, "error": "request_id 不合法"}
        try:
            ok = self._submit(answer(rid, str(option_id or ""))).result(timeout=5)
        except Exception as exc:  # noqa: BLE001 - 边界处统一转错误
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        # answered=False 表示这个请求已经不在了（超时/重复点击）—— 不是错误，但要说清楚
        return {"ok": True, "answered": bool(ok)}

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
