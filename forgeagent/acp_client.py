"""ACP 客户端 —— 协议层，与 UI 完全无关。

三个设计决定，都是踩过坑才定的：

1. **手写 JSON-RPC，不用 SDK 的高层 helper。**
   官方 SDK 的 `spawn_agent_process` 签名跨版本变过（factory 风格 vs 直接传实例），
   靠不住。这里跟 agentd 的 tests/test_acp.py 保持同一套约定：自己拼帧、自己读帧。

2. **stderr 必须捕获，不能让它打到终端。**
   agentd 的日志全部走 stderr（因为 stdout 要保持纯 JSON-RPC）。一旦 Textual 接管
   终端，这些日志直接打出来会把界面撕成碎片。所以 stderr 走管道，存进环形缓冲，
   UI 想看再看。

3. **必须有 reducer（Turn）。**
   ACP 的 session/update 是流式通知——一句话可能被拆成几十个 chunk 陆续到达，
   工具调用还有 start/update/done 三态。UI 拿事件流直接渲染，必然重复、乱序、闪烁。
   正确做法是先 reduce 成一个稳定状态，UI 只渲染这个状态。
   这个思路是从 Panda 的 README 学来的，是这类客户端的关键设计。

4. **必须实现反向请求（session/request_permission）。**
   ACP 不是"客户端只发命令"的单向协议：agent 会反过来请求客户端
   （审批、读文件、开终端）。这些是**请求**，每个都必须回一帧，不回对方就永久
   阻塞。所以 `_read_stdout` 按"有没有 method 字段"分流，而不是按 id ——
   两个方向各自编号，id 必然撞车。见 `_handle_incoming` 的注释。

SDK 是可选的：装了就用它的方法名常量表，没装就退回硬编码字面量，功能不受影响。
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

try:  # 可选依赖：只用来取方法名常量
    from acp import AGENT_METHODS, CLIENT_METHODS, PROTOCOL_VERSION
except Exception:  # 没装 SDK 也能跑
    AGENT_METHODS = None
    CLIENT_METHODS = None
    PROTOCOL_VERSION = 1


DEFAULT_TIMEOUT = 300.0  # 一轮生成可能很长，给足
STDERR_LIMIT = 500  # 环形缓冲上限，防止长时间跑把内存吃满
PERMISSION_TIMEOUT = 300.0  # 审批等这么久还没人答就按拒绝回帧（见 PermissionRequest）


class AcpError(RuntimeError):
    """agentd 返回 JSON-RPC error，或连接异常关闭。"""


# agentd 用 thought 通道送错误（ACP 的 stop_reason 只有五种，没有 error，
# 见 agentd/agentd/transports/acp_stdio.py 的 _STOP_REASON_MAP），
# 拿这个前缀标记。两个仓库各自定义同一个字面量——这里改了那边也要改，
# 否则最坏情况只是错误退化成普通思考文本显示，不会崩。
ERROR_MARK = "[错误]"

# agentd 在"用户拒绝了这次工具调用"时回灌给模型的固定开头
# （见 agentd/kernel/modes/agent.py 的 `用户拒绝执行`）。
#
# 为什么需要靠文案识别：ACP 的 ToolCallStatus 只有 pending/in_progress/
# completed/failed，**没有 cancelled**。内核的 cancelled 到了协议层被迫折成
# failed —— 于是"用户主动拒绝"和"工具真的炸了"在协议上长得一模一样。
# 我们在这里把它还原回来，界面才能显示成"已取消"（虚线灰边）而不是红色失败：
# 拒绝是用户的选择，不是故障，不该吓人。
# 两个仓库各自定义同一个字面量，同 ERROR_MARK 的约定。
DENY_MARK = "[错误] 用户拒绝"


@dataclass
class PermissionRequest:
    """agent 发来的审批请求。

    **它是请求不是通知**：JSON-RPC 意义上的请求，带 id，必须回一帧
    （见 answer_permission）。agentd 那边是 `await conn.request_permission(...)`，
    我们不回，它就永远卡在那一行 —— 整轮对话静默死住，两边日志都不报错。
    这也是为什么 _read_stdout 必须靠"有没有 method 字段"区分方向，
    而不能靠 id：双方各自从 1 开始编号，**id 必然撞车**。
    """

    request_id: int          # JSON-RPC id，回帧时原样带回
    session_id: str
    call_id: str             # toolCall.toolCallId，和工具卡片能对上
    title: str               # 工具短名，如 run_command
    kind: str                # read / edit / search / execute / other
    detail: str              # 人类可读的一行摘要（命令、文件路径…）
    options: list[dict]      # [{optionId, name, kind}]，由 agent 给，原样呈现

    @property
    def allow_ids(self) -> list[str]:
        return [str(o.get("optionId") or "") for o in self.options
                if str(o.get("kind") or "").startswith("allow")]

    @property
    def reject_id(self) -> str:
        for o in self.options:
            if str(o.get("kind") or "").startswith("reject"):
                return str(o.get("optionId") or "")
        return ""


@dataclass
class ToolCallState:
    """一次工具调用的归并状态（start / update 折叠到同一个对象）。"""

    call_id: str
    title: str = ""
    # ACP ToolKind：read / edit / delete / move / search / execute /
    # think / fetch / switch_mode / other。前端只对前几类给专门图标，
    # 认不出来的原样显示即可 —— 别在客户端做白名单，内核加新 kind 不该让界面瞎。
    kind: str = ""
    status: str = ""    # pending / in_progress / completed / failed
    output: str = ""


@dataclass
class Turn:
    """一轮对话 reduce 出来的稳定状态。

    prompt() 会反复 yield 同一个 Turn 实例（就地更新），UI 拿到后整体重绘即可，
    不需要自己做增量拼接。
    """

    user: str = ""
    text: str = ""
    thought: str = ""  # 思考通道，ACP 里客户端通常暗色渲染
    stop_reason: str = ""
    error: str = ""  # 从 thought 通道里识别出来的错误，见 ERROR_MARK
    unknown: list[str] = field(default_factory=list)  # 未识别的事件类型，调试用
    tools: list[ToolCallState] = field(default_factory=list)  # 本轮的工具调用（按出现顺序）
    tool_map: dict[str, ToolCallState] = field(default_factory=dict)  # call_id -> 状态

    @property
    def running(self) -> bool:
        return not self.stop_reason and not self.error


def _collect_text(node: Any) -> str:
    """从任意嵌套结构里捞出所有 text 字段。

    刻意不依赖 SDK 的 model 形状：SDK 版本之间嵌套层级变过，而"找所有 text"
    这个语义是稳的。宁可宽容，不要因为字段挪了位置就丢字。
    """
    out: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "text" and isinstance(value, str):
                out.append(value)
            else:
                out.append(_collect_text(value))
    elif isinstance(node, (list, tuple)):
        for value in node:
            out.append(_collect_text(value))
    return "".join(out)


def _method(table: Any, keys: tuple[str, ...], fallback: str) -> str:
    """从 SDK 常量表取 wire 方法名，取不到就用字面量。"""
    if isinstance(table, dict):
        for key in keys:
            value = table.get(key)
            if isinstance(value, str):
                return value
    elif table is not None:
        for key in keys:
            value = getattr(table, key, None)
            if isinstance(value, str):
                return value
    return fallback


# SDK 常量表的 key 是 python 风格全名（session_new / session_prompt / session_cancel）。
# 曾经写成 "new_session" / "prompt" 导致 KeyError —— 所以这里正名优先、
# 旧写法兜底、最后还有字面量，三层保险。
M_INIT = _method(AGENT_METHODS, ("initialize",), "initialize")
M_NEW = _method(AGENT_METHODS, ("session_new", "new_session"), "session/new")
M_PROMPT = _method(AGENT_METHODS, ("session_prompt", "prompt"), "session/prompt")
M_CANCEL = _method(AGENT_METHODS, ("session_cancel", "cancel"), "session/cancel")
M_UPDATE = _method(CLIENT_METHODS, ("session_update",), "session/update")
# agent -> client 的审批请求。这个是**反方向**的方法（agent 发起、客户端应答），
# 但它同样登记在 CLIENT_METHODS 里（"客户端要实现的那些方法"）。
M_PERMISSION = _method(
    CLIENT_METHODS, ("session_request_permission",), "session/request_permission"
)


class AcpClient:
    """把 agentd 当子进程拉起来，用 ACP 跟它说话。"""

    def __init__(
        self,
        command: list[str] | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        mcp_servers: list[dict] | None = None,
    ) -> None:
        # 默认用 sys.executable 而不是 "python"：
        # forgeagent 和 agentd 装在同一个 venv 里时，PATH 上的 python 未必是那一个。
        # 写成 "python" 的话，用 pip 装的 forgeagent 脚本一跑就"agent 起不来"，
        # 而报错只是 FileNotFoundError / ModuleNotFoundError，很难联想到是解释器错了。
        if command is not None:
            self._command = command
        elif raw := os.environ.get("FORGEAGENT_AGENT_CMD"):
            # posix 模式要显式指定：shlex 默认按 POSIX 规则转义，而 Windows 路径
            # 里的反斜杠在 POSIX 规则下是转义符（虽然只在 $ ` " \ 换行 前生效，
            # 但依赖这个细节太脆）。Linux/macOS 用 posix=True，Windows 用 False。
            self._command = shlex.split(raw, posix=(os.sep == "/"))
        else:
            # 默认值直接给 list，不走「拼字符串再切分」这道手续 ——
            # 跨平台时那道手续是主要的踩坑来源（引号、反斜杠、空格路径）。
            self._command = [sys.executable, "-m", "agentd.server"]
        self._cwd = cwd or os.environ.get("FORGEAGENT_CWD") or os.getcwd()
        self._env = env
        self._timeout = timeout
        # 本会话要接入的 MCP server（ACP 格式），session/new 时带过去
        self._mcp_servers = list(mcp_servers or [])

        self._proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task[None] | None = None
        self._stderr_reader: asyncio.Task[None] | None = None
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[dict]] = {}
        self._notifications: asyncio.Queue[dict] = asyncio.Queue()

        # 待审批的请求：request_id -> Future[optionId]。
        # 由 _read_stdout 填、answer_permission 解、_settle_permission 回帧。
        self._permissions: dict[int, asyncio.Future[str]] = {}
        self._perm_tasks: set[asyncio.Task[Any]] = set()
        self._permission_timeout = PERMISSION_TIMEOUT
        # 谁来处理审批请求。bridge 会把它设成"往 UI 事件队列里塞一条 permission 事件"。
        # 不设 = 没人可问 → 一律按拒绝回帧（fail-closed，绝不能默默放行）。
        self.on_permission: Any = None

        self.session_id = ""
        self.stderr_lines: list[str] = []

    # ---- 生命周期 ----

    async def start(self) -> None:
        """拉起 agentd 并完成握手。失败会抛 AcpError。"""
        # 子进程环境：默认继承父进程的，但强制 stdout/stderr 走 UTF-8。
        # 否则在非 UTF-8 本机 locale（比如 Windows 上 PYTHONIOENCODING=cp936 /
        # 中文 GBK）时，agentd 打出的中文日志是 GBK 字节，而我们这边按 utf-8
        # + replace 解码会全变成 U+FFFD，stderr 面板里中文全乱。这一台恰好是
        # UTF-8 所以之前没暴露，跨平台必须显式钉死。
        child_env = dict(os.environ if self._env is None else self._env)
        child_env["PYTHONIOENCODING"] = "utf-8"
        self._proc = await asyncio.create_subprocess_exec(
            *self._command,
            cwd=self._cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,  # 必须捕获，见模块 docstring 第 2 条
            env=child_env,
        )
        self._reader = asyncio.create_task(self._read_stdout())
        self._stderr_reader = asyncio.create_task(self._read_stderr())

        await self._call(M_INIT, {"protocolVersion": PROTOCOL_VERSION})
        await self.new_session()

    async def new_session(self) -> str:
        """开一个全新的 agentd 会话，把拿到的 sessionId 记下来。

        单独抽出来是因为 GUI 的「新对话」按钮也要开新会话，但那一步发生在
        initialize 握手之后、用户点了按钮才触发，不能和 start() 绑死。
        """
        # 切会话前先把旧会话挂着的审批结掉：那个弹窗已经属于上一个会话，
        # 用户不会再点，不结的话 agentd 里那个 await 永远不返回。
        stale = await self.cancel_pending_permissions()
        if stale:
            self._log(f"[审批] 切会话，作废 {stale} 个未决审批")
        resp = await self._call(M_NEW, {"cwd": self._cwd, "mcpServers": self._mcp_servers})
        self.session_id = resp["result"]["sessionId"]
        return self.session_id

    async def close(self) -> None:
        """收摊：停掉后台读取任务，终止子进程。"""
        # 先结掉待审批的，再停读线程 —— 否则那些 _settle_permission 任务
        # 会连着 Future 一起被丢掉，永远发不出回帧（虽然进程马上就没了）。
        await self.cancel_pending_permissions()
        for task in list(self._perm_tasks):
            task.cancel()
        self._perm_tasks.clear()

        for task in (self._reader, self._stderr_reader):
            if task is not None:
                task.cancel()
        self._reader = self._stderr_reader = None

        if self._proc is not None and self._proc.returncode is None:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                self._proc.kill()
        self._proc = None

    # ---- 核心 ----

    async def prompt(self, text: str) -> AsyncIterator[Turn]:
        """发一轮，持续 yield 就地更新的 Turn。

        用 async generator 是因为 UI 要在生成过程中实时重绘；yield 出去的是
        同一个对象，UI 直接整体重绘即可，不用管增量。
        """
        turn = Turn(user=text)
        task = asyncio.ensure_future(
            self._call(
                M_PROMPT,
                {
                    "sessionId": self.session_id,
                    "prompt": [{"type": "text", "text": text}],
                },
            )
        )
        notify_task: asyncio.Task[dict] | None = None
        try:
            while True:
                if notify_task is None:
                    notify_task = asyncio.ensure_future(self._notifications.get())
                # 同时等"来通知了"和"这轮结束了"，谁先到处理谁
                await asyncio.wait(
                    (task, notify_task), return_when=asyncio.FIRST_COMPLETED
                )
                if notify_task.done():
                    self._apply_update(turn, notify_task.result())
                    notify_task = None
                    yield turn
                if task.done():
                    break

            # 响应到了，把队列里剩余的通知吃掉，避免污染下一轮
            while not self._notifications.empty():
                self._apply_update(turn, self._notifications.get_nowait())

            resp = task.result()
            turn.stop_reason = str(resp.get("result", {}).get("stopReason", ""))
            yield turn

        except asyncio.CancelledError:
            await self.cancel()
            raise
        finally:
            if notify_task is not None and not notify_task.done():
                notify_task.cancel()
            if not task.done():
                task.cancel()

    async def cancel(self) -> None:
        """通知 agentd 中断当前轮次（通知，不期待响应）。"""
        if not self.session_id or self._proc is None:
            return
        await self._call(M_CANCEL, {"sessionId": self.session_id})

    # ---- 审批 ----

    async def answer_permission(self, request_id: int, option_id: str) -> bool:
        """回答一个审批请求。返回 False 表示这个请求已经不在了（超时/重复点击）。

        只负责"解开等在那儿的 Future"，真正回帧是 _settle_permission 干的 ——
        这样"_read_stdout 永远不阻塞"这条性质不会被破坏。
        """
        fut = self._permissions.get(request_id)
        if fut is None or fut.done():
            return False
        fut.set_result(str(option_id or ""))
        return True

    async def cancel_pending_permissions(self) -> int:
        """把所有还挂着的审批按"取消"结掉。

        为什么必须有：会话被切走 / 窗口关了的时候，界面上那个弹窗再也不会有人点，
        agentd 却还在 await。不主动结掉，那个子进程就带着一个永不完成的请求
        一直挂着。返回结掉的数量。
        """
        count = 0
        for fut in list(self._permissions.values()):
            if not fut.done():
                fut.set_result("")  # 空串 = 没选任何选项 → 回 cancelled
                count += 1
        return count

    async def _handle_incoming(self, request_id: int, method: str, params: dict) -> None:
        """处理 agent 发来的**请求**（必须回一帧，不回就是对方永久阻塞）。

        只认 session/request_permission；其余一律回 -32601。
        这条"必须回帧"的纪律比具体实现重要：我们还没实现的协议扩展
        （终端、fs/read_text_file 等）如果静默丢掉，agentd 那边就是一个
        再也醒不过来的 await。
        """
        if method != M_PERMISSION:
            self._log(f"[agent 请求] 未实现 {method}，已回 -32601")
            await self._respond_error(request_id, -32601, f"客户端未实现 {method}")
            return

        tool_call = params.get("toolCall") or params.get("tool_call") or {}
        raw_input = tool_call.get("rawInput") or tool_call.get("raw_input") or {}
        detail = ""
        if isinstance(raw_input, dict):
            detail = str(raw_input.get("detail") or "")
        elif raw_input:
            detail = str(raw_input)

        req = PermissionRequest(
            request_id=request_id,
            session_id=str(params.get("sessionId") or params.get("session_id") or ""),
            call_id=str(tool_call.get("toolCallId") or tool_call.get("tool_call_id") or ""),
            title=str(tool_call.get("title") or ""),
            kind=str(tool_call.get("kind") or "other"),
            detail=detail,
            options=[o for o in (params.get("options") or []) if isinstance(o, dict)],
        )

        fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._permissions[request_id] = fut

        hook = self.on_permission
        if hook is None:
            # 没人可问。绝不能默默放行 —— 那样"审批"就成了摆设。
            self._log(f"[审批] 没有处理器，按拒绝处理：{req.title}")
            fut.set_result("")
        else:
            try:
                hook(req)
            except Exception as exc:  # noqa: BLE001 - 钩子坏了也按拒绝走
                self._log(f"[审批] 处理器异常，按拒绝处理：{exc}")
                fut.set_result("")

        # 不在这里 await —— 那会把 _read_stdout 一起卡住，
        # 后面 agent 再发什么通知（比如工具卡片在转圈）就都读不到了。
        task = asyncio.ensure_future(self._settle_permission(req, fut))
        self._perm_tasks.add(task)
        task.add_done_callback(self._perm_tasks.discard)

    async def _settle_permission(
        self, req: PermissionRequest, fut: asyncio.Future[str]
    ) -> None:
        """等用户选择（或超时），然后把结果回给 agent。"""
        try:
            option_id = await asyncio.wait_for(fut, timeout=self._permission_timeout)
        except asyncio.TimeoutError:
            self._log(f"[审批] {self._permission_timeout:g}s 无人应答，按拒绝处理：{req.title}")
            option_id = ""
        except asyncio.CancelledError:
            option_id = ""
            raise
        finally:
            self._permissions.pop(req.request_id, None)

        if not option_id:
            # 空 = 用户没选（关掉了弹窗 / 超时）：ACP 的"没选任何选项"就是 cancelled。
            # agentd 侧把 cancelled 和 reject 都当拒绝，这里只是把语义表达准确。
            result: dict = {"outcome": {"outcome": "cancelled"}}
        else:
            # 注意：选了"拒绝"那个**选项**，按 ACP 语义是 selected（我们确实给了它一个
            # optionId），而不是 cancelled。cancelled 专指"压根没选"。
            result = {"outcome": {"outcome": "selected", "optionId": option_id}}
        self._log(f"[审批] {req.title} -> {option_id or 'cancelled'}")
        await self._respond(req.request_id, result)

    async def _respond(self, request_id: int, result: Any) -> None:
        await self._write_frame({"jsonrpc": "2.0", "id": request_id, "result": result})

    async def _respond_error(self, request_id: int, code: int, message: str) -> None:
        await self._write_frame(
            {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
        )

    async def _write_frame(self, frame: dict) -> None:
        if self._proc is None or self._proc.stdin is None:
            return
        try:
            self._proc.stdin.write(json.dumps(frame).encode("utf-8") + b"\n")
            await self._proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, AttributeError):
            # agent 已经没了，回帧失败没什么可补救的
            pass

    # ---- 内部 ----

    async def _call(self, method: str, params: dict) -> dict:
        """发一个请求并等它的响应。中途到达的通知会被塞进队列，不丢。"""
        if self._proc is None or self._proc.stdin is None:
            raise AcpError("agent 进程尚未启动")

        self._next_id += 1
        rid = self._next_id
        fut: asyncio.Future[dict] = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut

        frame = json.dumps(
            {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
        )
        self._proc.stdin.write(frame.encode("utf-8") + b"\n")
        await self._proc.stdin.drain()

        try:
            msg = await asyncio.wait_for(fut, timeout=self._timeout)
        except asyncio.TimeoutError as exc:
            self._pending.pop(rid, None)
            raise AcpError(f"{method} 超时（{self._timeout}s）") from exc

        if "error" in msg:
            err = msg["error"]
            raise AcpError(
                f"{err.get('message', '未知错误')} (code={err.get('code')})"
            )
        return msg

    async def _read_stdout(self) -> None:
        """后台读 stdout：按**有没有 method 字段**分流，不是按 id。

        踩过的坑（一定要记住）：JSON-RPC 里两个方向各自编号，我们发出去的
        `session/prompt` 可能是 id=3，agent 发来的审批请求也可能是 id=3。
        所以"带 id 的就是给我的响应"是错的 —— 这么写会把审批请求当成
        prompt 的响应，然后 prompt 提前结束、审批永远没人回、agentd 永久挂住。
        正确的判据是：有 method = 对方发起的（带 id 是请求、不带 id 是通知），
        没有 method = 对方给我的响应。
        """
        assert self._proc is not None and self._proc.stdout is not None
        try:
            while True:
                line = await self._proc.stdout.readline()
                if not line:
                    break  # stdout 关闭 = 进程没了
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    # agent 在 stdout 上打了非 JSON —— 协议违规，但别崩，记下来
                    self._log(f"[非 JSON stdout] {line[:200]!r}")
                    continue

                rid = msg.get("id")
                method = msg.get("method")

                if isinstance(method, str):
                    # 对方发起的东西
                    if rid is None:
                        await self._notifications.put(msg)  # 通知：入队给 reducer
                    else:
                        await self._handle_incoming(rid, method, msg.get("params") or {})
                    continue

                fut = self._pending.pop(rid, None) if rid is not None else None
                if fut is not None and not fut.done():
                    fut.set_result(msg)
                else:
                    await self._notifications.put(msg)
        except asyncio.CancelledError:
            return
        finally:
            # 进程没了就让所有等待者立刻失败，别傻等超时
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(AcpError("agent 进程已退出"))
            self._pending.clear()
            # 待审批的也一并结掉：没人会再点那个弹窗了
            for perm in list(self._permissions.values()):
                if not perm.done():
                    perm.set_result("")

    async def _read_stderr(self) -> None:
        """后台读 stderr，存进环形缓冲供 UI 查看。"""
        assert self._proc is not None and self._proc.stderr is not None
        try:
            while True:
                line = await self._proc.stderr.readline()
                if not line:
                    break
                self._log(line.decode("utf-8", "replace").rstrip())
        except asyncio.CancelledError:
            return

    def _log(self, text: str) -> None:
        self.stderr_lines.append(text)
        if len(self.stderr_lines) > STDERR_LIMIT:
            del self.stderr_lines[: len(self.stderr_lines) - STDERR_LIMIT]

    def _apply_update(self, turn: Turn, msg: dict) -> None:
        """把一个 session/update 通知折叠进 Turn —— 这就是 reducer。"""
        params = msg.get("params", {})
        update = params.get("update", params)
        kind = str(update.get("sessionUpdate", ""))

        # 工具调用：即使没有文本也要处理（start 事件常常只有 id/title/status）
        if kind in ("tool_call", "tool_call_update"):
            self._apply_tool(turn, update)
            return

        chunk = _collect_text(update)
        if not chunk:
            return

        if kind == "agent_thought_chunk":
            # 错误是借 thought 通道过来的（见 ERROR_MARK）。识别出来单独放，
            # 这样 UI 能用红色渲染，而不是混在"思考"里让人以为模型在自言自语。
            # turn.error 非空后再来的内容继续当错误正文——错误可能被拆成多个 chunk。
            if chunk.startswith(ERROR_MARK) or turn.error:
                body = chunk[len(ERROR_MARK):].lstrip() if chunk.startswith(ERROR_MARK) else chunk
                turn.error += body
            else:
                turn.thought += chunk
        elif kind in ("agent_message_chunk", ""):
            # 空 kind 兜底：SDK 各版本字段名有出入，宁可当正文也别丢字
            turn.text += chunk
        elif kind == "user_message_chunk":
            pass  # 自己说的话不重复渲染
        else:
            turn.unknown.append(kind)

    def _apply_tool(self, turn: Turn, update: dict) -> None:
        """把 tool_call / tool_call_update 折叠进 Turn.tools（按 call_id 归并）。"""
        call_id = str(update.get("toolCallId") or update.get("tool_call_id") or "")
        if not call_id:
            return
        state = turn.tool_map.get(call_id)
        if state is None:
            state = ToolCallState(call_id=call_id)
            turn.tools.append(state)
            turn.tool_map[call_id] = state
        if update.get("title"):
            state.title = str(update["title"])
        if update.get("kind"):
            state.kind = str(update["kind"])
        if update.get("status"):
            state.status = str(update["status"])
        raw = update.get("rawOutput", update.get("raw_output"))
        if raw is not None:
            state.output = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
        else:
            text = _collect_text(update.get("content") or [])
            if text:
                state.output = text

        # 把"用户拒绝"从 failed 里救回来（见 DENY_MARK 的注释）：
        # 协议层没有 cancelled 这个状态，只能靠输出文案区分。
        if state.status == "failed" and DENY_MARK in state.output:
            state.status = "cancelled"
