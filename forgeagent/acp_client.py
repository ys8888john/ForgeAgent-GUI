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


class AcpError(RuntimeError):
    """agentd 返回 JSON-RPC error，或连接异常关闭。"""


# agentd 用 thought 通道送错误（ACP 的 stop_reason 只有五种，没有 error，
# 见 agentd/agentd/transports/acp_stdio.py 的 _STOP_REASON_MAP），
# 拿这个前缀标记。两个仓库各自定义同一个字面量——这里改了那边也要改，
# 否则最坏情况只是错误退化成普通思考文本显示，不会崩。
ERROR_MARK = "[错误]"


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


class AcpClient:
    """把 agentd 当子进程拉起来，用 ACP 跟它说话。"""

    def __init__(
        self,
        command: list[str] | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        # 默认用 sys.executable 而不是 "python"：
        # forgeagent 和 agentd 装在同一个 venv 里时，PATH 上的 python 未必是那一个。
        # 写成 "python" 的话，用 pip 装的 forgeagent 脚本一跑就"agent 起不来"，
        # 而报错只是 FileNotFoundError / ModuleNotFoundError，很难联想到是解释器错了。
        default_cmd = f'"{sys.executable}" -m agentd.server'
        raw = os.environ.get("FORGEAGENT_AGENT_CMD", default_cmd)
        self._command = command if command is not None else shlex.split(raw)
        self._cwd = cwd or os.environ.get("FORGEAGENT_CWD") or os.getcwd()
        self._env = env
        self._timeout = timeout

        self._proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task[None] | None = None
        self._stderr_reader: asyncio.Task[None] | None = None
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[dict]] = {}
        self._notifications: asyncio.Queue[dict] = asyncio.Queue()

        self.session_id = ""
        self.stderr_lines: list[str] = []

    # ---- 生命周期 ----

    async def start(self) -> None:
        """拉起 agentd 并完成握手。失败会抛 AcpError。"""
        self._proc = await asyncio.create_subprocess_exec(
            *self._command,
            cwd=self._cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,  # 必须捕获，见模块 docstring 第 2 条
            env=self._env,
        )
        self._reader = asyncio.create_task(self._read_stdout())
        self._stderr_reader = asyncio.create_task(self._read_stderr())

        await self._call(M_INIT, {"protocolVersion": PROTOCOL_VERSION})
        resp = await self._call(M_NEW, {"cwd": self._cwd, "mcpServers": []})
        self.session_id = resp["result"]["sessionId"]

    async def close(self) -> None:
        """收摊：停掉后台读取任务，终止子进程。"""
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
        """后台读 stdout：带 id 的认领成响应，不带 id 的当通知入队。"""
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
                    self.stderr_lines.append(f"[非 JSON stdout] {line[:200]!r}")
                    continue

                rid = msg.get("id")
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
            tool_or_plan = ("tool_call", "tool_call_update", "plan")
            if kind in tool_or_plan:
                # 内核目前不产出这些事件（acp_stdio.py 里明确没映射）。
                # 先记下来，等内核补齐工具调用时这里就是挂卡片的位置。
                turn.unknown.append(f"{kind}: {chunk[:80]}")
            else:
                turn.unknown.append(kind)
