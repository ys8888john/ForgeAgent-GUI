"""本地 UI 服务：把 bridge 的能力暴露成 127.0.0.1 上一小撮 HTTP 接口。

为什么不用 pywebview 自带的 js_api：
    那条链路依赖 pywebview 在 NavigationCompleted 之后往页面注入 `window.pywebview`
    （edgechromium.py:389 -> util.inject_pywebview，分两步、在另一个线程里做）。
    实测在这台机器上极不稳定：注入脚本有时迟到几秒，有时根本不执行，
    界面就永远停在「不是通过 pywebview 打开的」；而 `window.evaluate_js()`
    在 EdgeChromium 后端上还会死锁（continuation 里 json.loads 抛异常就不放信号量）。
    换成 HTTP 之后，页面就是普通网页，JS 一律 fetch：
      - 不依赖脚本注入，三平台行为一致；
      - Python 侧不需要 evaluate_js；
      - 最关键：**能脱离 GUI 直接测**（见 tests/test_gui_server.py）。

安全边界（不是"把 ACP 搬到 web 上"）：
    ACP 本身仍然只在 stdio 上跑，agentd 一点没变。这个 HTTP 服务只是
    「本机窗口 <-> 本机 Python 进程」这一跳：
      - 只绑 127.0.0.1，端口由内核随机分配（port=0）；
      - 每个进程一次性 token，通过 URL fragment 交给页面（fragment 不会发给服务端）；
      - 每个请求校验 X-ForgeAgent-Token。
"""

from __future__ import annotations

import json
import os
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import sys
import urllib.request
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .bridge import Bridge
from .mcp_config import config_path, load_mcp_servers
from .sessions import SessionsSource

ASSETS = Path(__file__).parent / "assets"

_MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".json": "application/json; charset=utf-8",
}


class UiServer:
    """给窗口用的本地服务。

    bridge 可注入（单测塞个假 client 就能跑完整 HTTP 链路，不用真起 agentd）。
    """

    def __init__(
        self,
        bridge: Bridge | None = None,
        cwd: str | None = None,
        command: list[str] | None = None,
        host: str = "127.0.0.1",
        port: int = 0,
        sessions: SessionsSource | None = None,
        mcp_servers: list[dict] | None = None,
    ) -> None:
        # MCP server 配置：默认从 ~/.forgeagent/mcp.json 读（没有就是空），
        # 在 session/new 时交给 agentd。
        self.mcp_servers = load_mcp_servers() if mcp_servers is None else mcp_servers
        self.bridge = bridge if bridge is not None else Bridge(
            cwd=cwd, command=command, mcp_servers=self.mcp_servers
        )
        # 会话库只读视图：默认读 agentd 的 SQLite（~/.agentd/sessions.db）。
        # 测试可注入假的，完全不碰磁盘。
        self.sessions = sessions if sessions is not None else SessionsSource()
        self.token = secrets.token_hex(16)
        self._host = host
        self._httpd = ThreadingHTTPServer((host, port), _Handler)
        self._httpd.daemon_threads = True
        self._httpd.owner = self  # handler 里靠 self.server.owner 反查
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="forgeagent-ui", daemon=True
        )

    @property
    def port(self) -> int:
        return int(self._httpd.server_address[1])

    @property
    def url(self) -> str:
        """给 pywebview 的入口。token 走 fragment，不会出现在 HTTP 请求里。"""
        return f"http://{self._host}:{self.port}/#token={self.token}"

    def start(self) -> "UiServer":
        """开始监听（不连 agentd —— 那是 start_agent 的事）。"""
        if not self._thread.is_alive():
            self._thread.start()
        return self

    def start_agent(self) -> dict:
        """窗口出来之后在后台线程里连 agentd，别让启动那一秒白屏。"""
        return self.bridge.start()

    def client(self) -> "LocalClient":
        """进程内调用方（Tk 界面、冒烟测试）用的小 HTTP 客户端。"""
        return LocalClient(self)

    def stop(self) -> None:
        """关窗时调用：带走 agentd 子进程。"""
        self.bridge.close()
        try:
            self._httpd.shutdown()
            self._httpd.server_close()
        except Exception:  # noqa: BLE001 - 关闭路径不该抛
            pass


class LocalClient:
    """进程内访问 UiServer 的小客户端（只依赖标准库，不给项目加依赖）。"""

    def __init__(self, server: "UiServer") -> None:
        self._base = f"http://127.0.0.1:{server.port}"
        self._token = server.token

    def _req(self, method: str, path: str, body: dict | None = None) -> dict:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            self._base + path,
            data=data,
            method=method,
            headers={
                "X-ForgeAgent-Token": self._token,
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))

    def get(self, path: str) -> dict:
        return self._req("GET", path)

    def post(self, path: str, body: dict) -> dict:
        return self._req("POST", path, body)


class _Handler(BaseHTTPRequestHandler):
    # 刻意用 HTTP/1.0（默认值）：一个请求一条连接，不带 keep-alive。
    # 试过 HTTP/1.1 长连接，在 WebView2 上连接复用会偶发卡住 —— 页面第一个请求
    # 能出去，后面就再没有请求了。本机 loopback、每秒也就几个请求，
    # 换掉 keep-alive 完全不亏，换来的是确定性。
    protocol_version = "HTTP/1.0"
    # FORGEAGENT_UI_DEBUG=1 时把每个请求打到 stderr —— 排查「页面到底请求了什么」
    debug = os.environ.get("FORGEAGENT_UI_DEBUG", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )

    def log_message(self, fmt: str, *args) -> None:  # 默认别刷屏
        if self.debug:
            sys.stderr.write(f"[ui] {self.command} {self.path} -> {fmt % args}\n")
            sys.stderr.flush()

    # ---- 小工具 ----

    @property
    def _owner(self) -> UiServer:
        return self.server.owner  # type: ignore[attr-defined]

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(
            code,
            json.dumps(obj, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
        )

    def _fail(self, code: int, msg: str) -> None:
        self._json({"ok": False, "error": msg}, code)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            raw = json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:  # noqa: BLE001
            return {}
        return raw if isinstance(raw, dict) else {}

    def _authed(self) -> bool:
        return self.headers.get("X-ForgeAgent-Token") == self._owner.token

    # ---- 路由 ----

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 的命名
        u = urlparse(self.path)
        path, q = u.path, parse_qs(u.query)

        if path in ("/", "/index.html"):
            return self._file("index.html")

        if path.startswith("/api/"):
            if not self._authed():
                return self._fail(401, "token 不对")
            return self._api_get(path, q)

        if path.startswith("/assets/"):
            return self._file(path[len("/assets/"):])

        self._fail(404, f"没有这个路径: {path}")

    def do_POST(self) -> None:  # noqa: N802
        u = urlparse(self.path)
        if not u.path.startswith("/api/"):
            return self._fail(404, f"没有这个路径: {u.path}")
        if not self._authed():
            return self._fail(401, "token 不对")

        body = self._body()
        bridge = self._owner.bridge

        if u.path == "/api/send":
            text = str(body.get("text") or "")
            return self._json(bridge.send(text))

        if u.path == "/api/state":
            bridge.ui_report(body.get("state") if isinstance(body.get("state"), dict) else body)
            return self._json({"ok": True})

        if u.path == "/api/command":
            bridge.push_command(body if isinstance(body, dict) else {})
            return self._json({"ok": True})

        # ---- 审批应答 ----
        # agent 发来的 session/request_permission 是个**请求**，不回对方就永久阻塞
        # （agentd 侧是 await conn.request_permission）。所以这个接口不是可选的装饰，
        # 是协议闭环的一半。option_id 为空串表示"用户没选"（关掉弹窗/超时）。
        if u.path == "/api/permission":
            return self._json(
                bridge.answer_permission(body.get("id"), str(body.get("option_id") or ""))
            )

        # ---- 会话：新对话 / 续聊 ----
        # 续聊前先确认 id 真在库里（拿着只读视图查），再让 bridge 切换 sessionId；
        # 这样即使前端传个瞎编的 id，也不会悄悄把后续消息写进一个幽灵会话。
        if u.path == "/api/session/resume":
            sid = str(body.get("session_id") or "")
            if not sid:
                return self._fail(400, "缺少 session_id")
            if not self._owner.sessions.exists(sid):
                return self._fail(404, f"没有这个会话: {sid}")
            return self._json(bridge.resume_session(sid))

        if u.path == "/api/session/new":
            return self._json(bridge.new_session())

        self._fail(404, f"没有这个接口: {u.path}")

    def _api_get(self, path: str, q: dict) -> None:
        bridge = self._owner.bridge

        if path == "/api/hello":
            return self._json({"ok": True, "token_ok": True})

        # 本机 MCP 配置摘要（侧栏/状态区显示连了几个 server）
        if path == "/api/mcp":
            servers = self._owner.mcp_servers
            return self._json(
                {
                    "ok": True,
                    "path": str(config_path()),
                    "count": len(servers),
                    "servers": [s.get("name") for s in servers],
                }
            )

        # ---- 会话侧栏 / 续聊 ----
        # /api/sessions        列出已有会话（侧栏用）
        # /api/session/<id>    某会话完整历史（点开看 / 续聊前先画出来）
        # 注意：/api/session/new 和 /api/session/resume 是 POST，不在这里处理。
        if path == "/api/sessions":
            return self._json({"ok": True, "sessions": self._owner.sessions.list_meta()})

        if path.startswith("/api/session/"):
            sid = path[len("/api/session/"):]
            if not sid:
                return self._fail(400, "缺少 session_id")
            hist = self._owner.sessions.get_history(sid)
            if hist is None:
                return self._fail(404, f"没有这个会话: {sid}")
            return self._json({"ok": True, "history": hist})

        if path == "/api/events":
            try:
                timeout = float((q.get("timeout") or ["2.0"])[0])
            except ValueError:
                timeout = 2.0
            return self._json({"ok": True, "events": bridge.next_events(timeout)})

        if path == "/api/stderr":
            try:
                n = int((q.get("n") or ["100"])[0])
            except ValueError:
                n = 100
            return self._json({"ok": True, "lines": bridge.stderr_tail(n)})

        if path == "/api/state":
            return self._json({"ok": True, "state": bridge.ui_state()})

        self._fail(404, f"没有这个接口: {path}")

    def _file(self, rel: str) -> None:
        rel = (rel or "").strip("/") or "index.html"
        target = (ASSETS / rel).resolve()
        root = ASSETS.resolve()
        # 防目录穿越
        if target != root and root not in target.parents:
            return self._fail(403, "越界了")
        if not target.is_file():
            return self._fail(404, f"没有这个文件: {rel}")

        body = target.read_bytes()
        if target.suffix.lower() == ".html":
            # 把本次启动的 token 直接写进页面。
            # 不靠 URL fragment —— pywebview 传 URL 时 fragment 不一定保得住。
            body = body.replace(b"__FORGEAGENT_TOKEN__", self._owner.token.encode("ascii"))
        self._send(
            200,
            body,
            _MIME.get(target.suffix.lower(), "application/octet-stream"),
        )
