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
import re
import secrets
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import sys
import urllib.request
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .bridge import Bridge
from . import models as _models
from .mcp_config import config_path, load_mcp_servers, save_config_dict
from .mcp_presets import PRESETS, build_entries, merge_config, python_bin, repo_root, venv_dir
from .models import env_for, load_models, models_path, sanitize_profile, save_models
from .sessions import SessionsSource
from .spaces import SpaceManager

ASSETS = Path(__file__).parent / "assets"


def _wsl_capture(args: list[str], timeout: float = 10.0) -> str:
    """跑一条 wsl.exe 命令并拿回 stdout。

    两个坑都在这儿：
      1. wsl.exe 的输出是 UTF-16LE（`wsl -l -v` 尤其如此）。原先用 text=True
         让它按 locale 编码解，在中文 Windows 上直接抛 UnicodeDecodeError，
         再被 except 吞成「拿不到 IP」——host 于是静默退回 localhost，撞上
         本机 11434 上那条指向旧 IP 的 portproxy，表现为「连不上 Ollama」，
         而真因（WSL 没跑 / IP 变了）一个字都看不到。
      2. stderr 里混着乱码和告警，一律丢掉，只取 stdout。
    """
    proc = subprocess.run(["wsl.exe", *args], capture_output=True, timeout=timeout)
    raw = proc.stdout or b""
    if b"\x00" in raw[:400]:  # UTF-16LE 标志
        return raw.decode("utf-16-le", errors="replace").strip("\x00").strip()
    return raw.decode("utf-8", errors="replace").strip("\x00").strip()


def _wsl_distro() -> str | None:
    """默认发行版名（`wsl -l -q` 里带 `*` 的那行）。取不到就返回 None，
    后续用不带 -d 的 wsl.exe 兜底（它自己会用默认发行版）。"""
    try:
        listing = _wsl_capture(["-l", "-q"])
    except Exception:  # noqa: BLE001 - 探测失败按「没有默认发行版」处理
        return None
    for line in listing.splitlines():
        line = line.strip().lstrip("*").strip()
        if line:
            return line
    return None


def _wsl_running(distro: str | None) -> bool:
    """发行版是不是已经在跑。分不清 running/stopped 时保守返回 True ——
    宁可多跑一次 `hostname -I`（已运行的话是毫秒级），也别在它明明活着时
    误判成没跑、进而退回 localhost。"""
    try:
        listing = _wsl_capture(["-l", "-v"])
    except Exception:  # noqa: BLE001
        return True
    for line in listing.splitlines():
        # 按空白切成列再取第一列当发行版名。**不能**用 strip() 取整行 ——
        # '* Ubuntu    Stopped         2' 去掉星号后 strip 仍是
        # 'Ubuntu    Stopped         2'，跟 distro 永远比不相等，整行会被
        # 跳过、最后 return True：WSL 明明停着却报「在跑」。
        parts = line.replace("*", "").split()
        if not parts:
            continue
        if distro and parts[0] != distro:
            continue
        low = line.lower()
        if "stopped" in low or "stopping" in low:
            return False
        return True
    return True


def _wsl_ip(boot: bool = True) -> str | None:
    """WSL2 实例在 NAT 子网里分到的 IP。

    让 Windows 侧 agentd 直连这个 IP 去够 WSL 里的 Ollama —— 绕开 Windows↔WSL
    那套 loopback 转发。实测 WSL 的 loopback 转发只对「顶层进程」生效，
    agentd 作为子进程走不通；而直连 IP 走的是 vEthernet，稳。

    boot=True 时发行版没跑就把它拉起来（`wsl -e true` 即可触发启动）——
    Ollama 装在 WSL 里靠 systemd 随发行区启动，不拉起来就一定连不上。
    这是之前「提示让用户自己去确认 ollama serve 在跑」的根因：GUI 启动
    时 WSL 通常是 Stopped，探测直接返回 None，host 退回 localhost，然后
    去撞本机 11434 上那条指向旧 IP 的 portproxy，稳稳失败。
    """
    if os.name != "nt":
        return None
    distro = _wsl_distro()
    if not _wsl_running(distro):
        if not boot:
            return None
        try:
            # -e true 是最轻的「启动但不做事」；Ollama 由 WSL 内 systemd 拉起。
            subprocess.run(
                ["wsl.exe", *(["-d", distro] if distro else []), "-e", "true"],
                capture_output=True, timeout=45,
            )
        except Exception:  # noqa: BLE001 - 拉不起来就按「没有 IP」处理
            return None
    try:
        out = _wsl_capture(
            ["-d", distro, "-e", "hostname", "-I"] if distro else ["-e", "hostname", "-I"]
        )
    except Exception:  # noqa: BLE001
        return None
    for token in out.split():  # hostname -I 可能返回多个地址（v4 + v6）
        ip = token.strip()
        if ":" not in ip:  # 跳过 IPv6，Ollama 监听与路由都按 v4
            return ip
    return None


def _wsl_mirrored(config_path: str | os.PathLike | None = None) -> bool:
    """~/.wslconfig 里 [wsl2] 段的 networkingMode 是不是 mirrored。

    mirrored 下 WSL 与宿主**共用同一个网络栈**，WSL 里监听 0.0.0.0 的服务
    直接出现在宿主机的 0.0.0.0 上 —— 于是 localhost:11434 天然可连，而且
    那个地址**永不改变**（不再有 172.18.x.x 这种每次重启都可能变的 NAT 地址）。
    这是唯一真正意义上「把地址钉死」的办法。

    读不到 / 不是 mirrored 一律按 False（nat）处理，跟实际行为一致。
    config_path 只给测试用，生产走 %USERPROFILE%\\.wslconfig。
    """
    if os.name != "nt":
        return False
    try:
        if config_path is None:
            home = os.environ.get("USERPROFILE") or str(Path.home())
            config_path = Path(home) / ".wslconfig"
        text = Path(config_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    for chunk in re.split(r"(?m)^\s*\[", text):
        if not chunk.lower().startswith("wsl2]"):
            continue  # 别把 [experimental] 之类的段也算进来
        m = re.search(r"(?mi)^\s*networkingMode\s*=\s*(\S+)", chunk)
        if not m:
            return False
        return m.group(1).strip().strip('"').lower() == "mirrored"
    return False


class _WslKeepalive:
    """挂一个 WSL 客户端会话，防止发行区被空闲回收。

    WSL 2.7 起，发行区在**没有任何客户端连接**之后会被回收，而且这条
    行为已经不受配置控制：vmIdleTimeout 写 -1、0 都实测无效（wsl.conf 里
    那个键甚至直接报 Unknown key）。systemd 里的常驻服务也拦不住 ——
    判定看的是「有没有客户端挂着」，不是「有没有进程在跑」。

    实测（这台机器）：
        无 keepalive            -> 60 秒后 wsl -l -v 显示 Stopped，ollama 消失
        挂 wsl -e sleep infinity -> 3 分钟后仍 Running，/api/tags 一直 200

    对 GUI 来说这是硬需求：窗口开着不等于有人在说话。用户安静几分钟，
    WSL 就被回收了，下一次对话必然 All connection attempts failed。挂一个
    纯 sleep 的会话代价极低，换来整个会话期间 ollama 常驻。
    """

    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None

    def start(self) -> None:
        if os.name != "nt" or self._proc is not None:
            return
        # 测试里 UiServer 会被构造几十次，每次挂一个 wsl.exe 睡到天荒地老
        # 纯属泄漏。conftest.py 设这个变量关掉它。
        if os.environ.get("FORGEAGENT_NO_WSL_KEEPALIVE"):
            return
        distro = _wsl_distro()
        cmd = ["wsl.exe", *(["-d", distro] if distro else []), "-e", "sleep", "infinity"]
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=flags,
            )
        except Exception:  # noqa: BLE001 - 保活失败不该拦住 GUI 启动
            self._proc = None

    def stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001 - 退出路径不该抛
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass


def _backend_prefix(backend: str | None) -> str:
    """后端名 -> 它那组 AGENTD_* 环境变量的前缀。

    与前端 index.html 里的 backendKeyPrefix 保持一致：mimo / zhipu /
    openai_compat / ollama 各自写不同的 MODEL / BASE_URL / API_KEY 键，
    agentd 按前缀取自己那份。
    """
    backend = (backend or "").strip()
    if backend == "mimo":
        return "AGENTD_MIMO_"
    if backend == "zhipu":
        return "AGENTD_ZHIPU_"
    if backend == "openai_compat":
        return "AGENTD_OPENAI_"
    return "AGENTD_OLLAMA_"


def _route_wsl_ollama(env: dict | None, profile_id: str | None) -> dict | None:
    """WSL 本地 Ollama 的 profile（id 含 'wsl' 且 host 是 loopback）在 Windows 上
    自动改写成 WSL 实例 IP，让 Windows 侧 agentd 直连、绕过不稳定的 loopback 转发。

    其它 profile（云端模型、真正本机 Ollama）原样返回，不受影响。

    两种模式：
      - nat（默认）：loopback 转发实测不生效，只能直连 WSL 的 NAT IP。IP 每次
        重启可能变，所以这里每次现取；取不到就保持原样，不硬塞坏地址。
      - mirrored：共享网络栈，localhost 直连且地址永久固定 —— 此时**不改写**，
        用户填什么 loopback 就用什么。
    """
    if not env or not profile_id or "wsl" not in str(profile_id).lower():
        return env
    if _wsl_mirrored():
        # 地址固定成 loopback、不改写；但 WSL 停着的时候 localhost 上并没有
        # 人在监听 —— 照样要把它拉起来，否则就是「地址完全正确、但连不上」。
        # 这里只取 _wsl_ip 的副作用（启动发行版），返回值用不上。
        _wsl_ip(boot=True)
        return env
    h = env.get("AGENTD_OLLAMA_HOST", "")
    if "127.0.0.1" not in h and "localhost" not in h:
        return env
    ip = _wsl_ip()
    if not ip:
        return env
    env = dict(env)
    env["AGENTD_OLLAMA_HOST"] = f"http://{ip}:11434"
    return env


def _ollama_models(host: str, timeout: float = 3.0) -> list[str]:
    """GET {host}/api/tags 取模型名列表。失败一律返回 []，不给上层抛。"""
    import urllib.request

    url = f"{host.rstrip('/')}/api/tags"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception:  # noqa: BLE001 - 连不上/超时/返回非 JSON，统统当「不可用」
        return []
    models = data.get("models") if isinstance(data, dict) else None
    if not isinstance(models, list):
        return []
    return [str(m.get("name")) for m in models if isinstance(m, dict) and m.get("name")]


def _probe_ollama_host(host: str) -> dict:
    """探测一个用户填进来的任意地址（模型配置弹窗的「测试连接」用）。

    只收 http/https —— 这个端点会按参数去连任意地址，限制协议是最低限度
    的自我约束（这是本机工具，不对外，但没必要给自己留 file:// 之类的口子）。
    """
    host = (host or "").strip()
    if not host:
        return {"ok": False, "error": "地址为空"}
    if not host.startswith(("http://", "https://")):
        return {"ok": False, "error": "地址要以 http:// 或 https:// 开头"}
    models = _ollama_models(host, timeout=5.0)
    if not models:
        return {"ok": False, "host": host, "error": "连不上或该地址不是 Ollama（/api/tags 无响应）"}
    return {"ok": True, "host": host, "models": models}


def resolve_ollama_host(env: dict | None = None) -> str:
    """当前生效的 Ollama 地址：profile 的 AGENTD_OLLAMA_HOST > 进程环境变量 >
    agentd 自己的默认值。三者跟 Agentd/boot.py:111 的优先级一致。

    兜底默认写 127.0.0.1 而不是 localhost —— 这是被实测数据逼出来的：mirrored
    模式下 WSL 的 IPv6 回环 ::1 是不通的，而 Windows 上 localhost 会解析成
    ::1,127.0.0.1（IPv6 优先），Python 的 urllib/httpx 未必回退到 IPv4。
    实测同一时刻 localhost 只有 1/10 成功率，127.0.0.1 是 10/10。
    """
    if env is None:
        active = load_models().get("active")
        env = _route_wsl_ollama(env_for(active), active)
    return (
        (env or {}).get("AGENTD_OLLAMA_HOST")
        or os.environ.get("AGENTD_OLLAMA_HOST")
        or "http://127.0.0.1:11434"
    )


def ollama_status(env: dict | None = None) -> dict:
    """当前生效的 Ollama host 通不通、上面有什么模型。

    给前端一个能主动问的入口 —— 过去只有 agentd 抛 httpx 的
    "All connection attempts failed"，用户看不到到底连的是哪个地址。

    超时给 8s 而不是 _ollama_models 的默认 3s：走 vEthernet 进 WSL 的第一
    个请求常要 3~4s（WSL 刚起时 ollama 还在预热），按 3s 判会误报「连不上」，
    而用户看到的恰恰就是这类误报 —— 健康检查宁可慢，也不能给假-negative。
    """
    host = resolve_ollama_host(env)
    models = _ollama_models(host, timeout=8.0)
    return {
        "ok": bool(models),
        "host": host,
        "models": models,
        "wsl_ip": _wsl_ip(boot=False) if os.name == "nt" else None,
    }


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
        spaces: SpaceManager | None = None,
    ) -> None:
        # MCP server 配置：默认从 ~/.agentd/gui/mcp.json 读（没有就是空），
        # 在 session/new 时交给 agentd。
        self.mcp_servers = load_mcp_servers() if mcp_servers is None else mcp_servers
        # **启动时应用 active 模型 profile**：models.json 里记着"当前用哪个
        # provider"（含 AGENTD_* 环境变量组）。早期做法是把这些变量直接塞进
        # agentd 子进程的启动环境，靠"启动环境变量最优先"生效；现在改成写进
        # **热配置文件**（agentd 每轮都读），好处是切换时**不用重启子进程**——
        # 会话历史、工具循环、已加载的 MCP 全都不用重建。这条链和
        # /api/model/select 的热切换写的是同一个文件。
        active = load_models().get("active")
        self.models_env = _route_wsl_ollama(env_for(active), active)
        # 告诉 agentd 去哪读热配置（GUI 自己管理的 ~/.agentd/gui/hotenv.json）。
        # 设到本进程环境里，这样每次 spawn 子进程（含 MCP 改动触发的重启）
        # 都会自动带上，不会在重启后丢掉。
        self.hotenv_path = _models.hotenv_path()
        os.environ["AGENTD_HOTENV"] = str(self.hotenv_path)
        # 启动时先把 active profile 的 env 写进热文件（active 为空则写空，
        # 让 agentd 回落到自己的 .env）。agentd 首轮对话就读它。
        if self.models_env:
            _models.write_hotenv(self.models_env)
        else:
            _models.clear_hotenv()
        # Ollama 跑在 WSL 里时挂个保活会话（判据跟 _route_wsl_ollama 一致：
        # profile id 含 wsl）。必须在 agentd 起来之前挂上，否则握手探模型
        # 时 WSL 可能正被回收，探完 /api/tags 拿不到就退回默认模型。
        self._wsl_keepalive = _WslKeepalive()
        if active and "wsl" in str(active).lower():
            self._wsl_keepalive.start()
        # ---- 空间（Space）：参考 WorkBuddy 的「空间」----
        # 每个空间是一个具名工作目录；在某个空间下开会话，会话就自动绑定到它的
        # 目录（cwd），agentd 的原生工具以该目录为根。多空间互不串门。
        #
        # cwd 优先级：
        #   1) 显式传了 cwd（用户 --cwd / FORGEAGENT_CWD）→ 把它落地成一个空间并激活
        #      （找不到就按目录名建一个）。这样「forgeagent-gui --cwd /p/project」
        #      就等于「打开名为 project 的空间」。
        #   2) 没传 → 用持久化的 active 空间目录（首次运行 = ~/.agentd/spaces/default）。
        # 注意：Electron 壳只有在用户显式给了 --cwd 时才会往后端传 cwd（main.js 改过），
        # 所以正常双击启动走的是第 2 条，即空间系统说了算。
        self.spaces = spaces if spaces is not None else SpaceManager()
        if cwd:
            self.spaces.ensure_space_for_path(cwd)
        self.current_space_name = self.spaces.active_name
        self.current_space_dir = self.spaces.active_path()
        # 不再把 profile env 注入子进程：agentd 改从热文件读（见上）。
        self.bridge = bridge if bridge is not None else Bridge(
            cwd=self.current_space_dir, command=command, mcp_servers=self.mcp_servers, env=None
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
        """关窗时调用：带走 agentd 子进程。

        shutdown() 只能在 serve_forever 跑着的时候调 —— 没,start 过的 server
        （构造完直接停，比如测试/工具脚本）调 shutdown 会**永久挂死**：
        它等的是一个从未开始、也就永远不会退出的循环。is_alive 判一下；
        server_close（关 socket）则总是安全的。
        """
        self.bridge.close()
        self._wsl_keepalive.stop()
        try:
            if self._thread.is_alive():
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

        # ---- 停止：把正在生成的一轮按用户意愿收掉 ----
        # 只递通知不等待；agentd 在下一个流式/工具边界收尾，done 事件会带来
        # stop_reason="cancelled"，前端借此把运行中的工具卡片收成「已取消」。
        if u.path == "/api/cancel":
            return self._json(bridge.cancel())

        # ---- 会话：新对话 / 续聊 ----
        # 续聊前先确认 id 真在库里（拿着只读视图查），再让 bridge 切换 sessionId；
        # 这样即使前端传个瞎编的 id，也不会悄悄把后续消息写进一个幽灵会话。
        if u.path == "/api/session/resume":
            sid = str(body.get("session_id") or "")
            if not sid:
                return self._fail(400, "缺少 session_id")
            if not self._owner.sessions.exists(sid):
                return self._fail(404, f"没有这个会话: {sid}")
            # 还原该会话归属的空间：按 session_spaces.json 查出原空间目录，
            # 切到那个空间（同时改 active，让 header 高亮跟着变），再把 cwd 带进
            # load_session —— 续聊出来的工具 cwd 就落回正确的目录。
            owner = self._owner
            space_name = owner.spaces.space_of(sid)
            if space_name:
                sp = owner.spaces.resolve_path(space_name)
                if sp:
                    try:
                        owner.spaces.set_active(space_name)
                    except ValueError:
                        pass
                    owner.current_space_name = space_name
                    owner.current_space_dir = sp
                    return self._json(bridge.resume_session(sid, cwd=sp))
            return self._json(bridge.resume_session(sid))

        if u.path == "/api/session/new":
            r = bridge.new_session()
            if r.get("ok"):
                owner = self._owner
                sid = r.get("session")
                if sid:
                    owner.spaces.bind(sid, owner.current_space_name)
                r["space"] = {
                    "name": owner.current_space_name,
                    "path": owner.current_space_dir,
                }
            return self._json(r)

        # ---- 空间（Space）：新建 / 切换 ----
        # 参考 WorkBuddy 的「空间」：每个空间一个具名工作目录，会话归属空间后
        # 自动把 cwd 绑到那个目录。新建完自动进入（切 active + 改后续会话 cwd）。
        if u.path == "/api/space/new":
            name = str(body.get("name") or "").strip()
            path = body.get("path")  # 可选：用户显式指定目录；留空则放 ~/.agentd/spaces/<name>
            path = str(path).strip() if isinstance(path, str) else None
            if not name:
                return self._fail(400, "缺少空间名")
            try:
                created = self._owner.spaces.add_space(name, path=path)
            except (ValueError, OSError) as exc:
                return self._fail(400, str(exc))
            owner = self._owner
            owner.spaces.set_active(created["name"])
            owner.current_space_name = created["name"]
            owner.current_space_dir = created["path"]
            bridge.set_cwd(created["path"])
            return self._json(
                {
                    "ok": True,
                    "space": created,
                    "active": created["name"],
                    "spaces": owner.spaces.list_spaces(),
                }
            )

        if u.path == "/api/space/switch":
            name = str(body.get("name") or "").strip()
            sp = self._owner.spaces.resolve_path(name)
            if sp is None:
                return self._fail(404, f"没有这个空间: {name}")
            owner = self._owner
            owner.spaces.set_active(name)
            owner.current_space_name = name
            owner.current_space_dir = sp
            bridge.set_cwd(sp)
            return self._json(
                {
                    "ok": True,
                    "active": name,
                    "path": sp,
                    "spaces": owner.spaces.list_spaces(),
                }
            )

        # ---- 模式切换：转 bridge（session/set_mode，下一轮 prompt 生效）----
        if u.path == "/api/mode":
            return self._json(bridge.set_mode(str(body.get("mode_id") or "")))

        # ---- 会话"删除"：侧栏隐藏（库写方在 agentd，GUI 只维护本地名单）----
        if u.path == "/api/session/hide":
            sid = str(body.get("session_id") or "")
            if not sid:
                return self._fail(400, "缺少 session_id")
            sessions = getattr(self._owner, "sessions", None)
            if sessions is None or not hasattr(sessions, "hide"):
                return self._fail(501, "当前会话源不支持隐藏（测试假件未实现）")
            sessions.hide(sid)
            if bridge._client.session_id == sid:  # noqa: SLF001 - 同包内共知成员
                bridge.new_session()  # 隐藏的是当前会话 → 顺手开个新的，不留悬空引用
            return self._json({"ok": True, "session": sid})

        # ---- 会话重命名：同隐藏一样走本地名单（alias 优先于自动标题）----
        if u.path == "/api/session/rename":
            sid = str(body.get("session_id") or "")
            title = str(body.get("title") or "").strip()
            if not sid:
                return self._fail(400, "缺少 session_id")
            sessions = getattr(self._owner, "sessions", None)
            if sessions is None or not hasattr(sessions, "rename"):
                return self._fail(501, "当前会话源不支持重命名（测试假件未实现）")
            final = sessions.rename(sid, title)
            return self._json({"ok": True, "session": sid, "title": final})

        # ---- 模型：自定义 profile 管理 / 切换 ----
        # profile = 一组注入 agentd 的 AGENTD_* 环境变量（存
        # ~/.agentd/gui/models.json）。切换 = 把选中 profile 的 env 写进
        # **热配置文件**（~/.agentd/gui/hotenv.json），运行中 agentd 下一轮
        # 对话即生效，**不重启子进程**（会话历史 / 工具循环 / MCP 全保留）。
        # ---- MCP 配置原文保存（管理弹窗）----
        # 校验到"能安全写回"即可：字段级校验是 agentd 的 MCP SDK 的职责，
        # 这里只保证能解析、顶层形状对 —— README 有"字段缺失被静默清空"的坑。
        if u.path == "/api/mcp/raw":
            text = body.get("text")
            if not isinstance(text, str):
                return self._fail(400, "text 必须是字符串")
            try:
                data = json.loads(text)
            except ValueError as exc:
                return self._fail(400, f"JSON 解析失败：{exc}")
            if not isinstance(data, dict):
                return self._fail(400, "顶层必须是 JSON 对象")
            if "mcpServers" in data and not isinstance(data["mcpServers"], dict):
                return self._fail(400, "mcpServers 必须是对象")
            p = config_path()
            try:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
            except OSError as exc:
                return self._fail(500, f"写 {p} 失败：{exc}")
            # 生效走同一套 restart：mcpServers 属于 session/new，新进程才用新声明
            servers = load_mcp_servers(p)
            self._owner.mcp_servers = servers
            restarted = bridge.restart(mcp_servers=servers)
            return self._json({"ok": restarted.get("ok", False), "count": len(servers), "detail": restarted})

        # ---- 常用工具一键添加（MCP 预设）----
        # bundled（自研）直接写配置；pip 型只在"专用 venv 里已经装好包"时放行
        # —— 包安装交给 scripts/install_local_mcp.py（沙箱/离线实测过 pip 出网
        # 不可控，不在 HTTP 请求里现装）。
        if u.path == "/api/mcp/presets/add":
            name = str(body.get("name") or "").strip()
            preset = PRESETS.get(name)
            if preset is None:
                return self._fail(404, f"没有这个预设: {name}")
            if preset.bundled_script:
                root = repo_root()
                if not (root / preset.bundled_script).is_file():
                    return self._fail(500, "仓库脚本缺失，请重新拉取 ForgeAgent-GUI")
                venv = str(Path(sys.executable).parent.parent)
            else:
                py = python_bin(venv_dir())
                if not py.is_file():
                    return self._fail(
                        400,
                        "专用 venv 还没建：先运行 scripts/install_local_mcp.py --install --write",
                    )
                probe = subprocess.run(
                    [str(py), "-c", f"import {preset.module}"],
                    capture_output=True,
                    timeout=15,
                )
                if probe.returncode != 0:
                    return self._fail(
                        400,
                        f"{preset.package} 还没装进专用 venv："
                        "运行 scripts/install_local_mcp.py --install --write"
                        "（离线机器加 --index-url https://pypi.tuna.tsinghua.edu.cn/simple）",
                    )
                venv = str(venv_dir())
            entry_root = repo_root()
            entries = build_entries([name], venv, root=entry_root)
            p = config_path()
            try:
                existing = json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}
            except ValueError:
                existing = {}
            save_config_dict(merge_config(existing if isinstance(existing, dict) else {}, entries), p)
            servers = load_mcp_servers(p)
            self._owner.mcp_servers = servers
            restarted = bridge.restart(mcp_servers=servers)
            return self._json({"ok": restarted.get("ok", False), "count": len(servers), "detail": restarted})

        if u.path == "/api/models":
            data = body.get("data") if isinstance(body.get("data"), dict) else {}
            profiles = [sanitize_profile(p) for p in data.get("profiles") or [] if isinstance(p, dict)]
            try:
                saved = save_models({"active": data.get("active"), "profiles": profiles})
            except ValueError as exc:
                return self._fail(400, str(exc))
            return self._json({"ok": True, "data": self._models_masked(saved)})

        if u.path == "/api/model/select":
            pid = str(body.get("id") or "")
            model = str(body.get("model") or "").strip()  # 同一 provider 下选中的具体模型
            data = load_models()
            if pid and not any(p.get("id") == pid for p in data["profiles"]):
                return self._fail(404, f"没有这个模型配置: {pid}")
            save_models({**data, "active": pid or None})

            # 热切换：把选中的 env 写进热配置文件，agentd 下一轮即用，不重启。
            env = env_for(pid or None)
            if model and env:
                # 同一 provider 下在多个模型间切换：只改 MODEL，其余（key/base/backend）沿用
                prefix = _backend_prefix(env.get("AGENTD_LLM_BACKEND"))
                env = dict(env)
                env[prefix + "MODEL"] = model
            routed = _route_wsl_ollama(env, pid or None) if env else {}
            if routed:
                _models.write_hotenv(routed)
            else:
                # 切回「默认（agentd .env / Ollama）」：删掉热文件，agentd 回落到自己配置
                _models.clear_hotenv()
            # 切到 WSL 的 Ollama 时确保 WSL 没被回收；若已启动过则 no-op
            if pid and "wsl" in str(pid).lower():
                self._wsl_keepalive.start()
            return self._json(
                {"ok": True, "hot_swapped": bool(routed), "id": pid or None, "model": model or None}
            )

        self._fail(404, f"没有这个接口: {u.path}")

    def _api_get(self, path: str, q: dict) -> None:
        bridge = self._owner.bridge

        if path == "/api/hello":
            return self._json({"ok": True, "token_ok": True})

        # ---- 自定义模型 profile ----
        if path == "/api/models":
            return self._json(self._models_payload())

        # Ollama 连通性自检：主动问一次，比等 agentd 抛 httpx 错误清楚得多。
        # 刻意不传 self.models_env —— 那是启动时算的，WSL IP 漂移后就过期了；
        # 这里重新解析才能反映「此刻」连不连得上。
        if path == "/api/ollama/status":
            # 带 host= 时探测那个地址（未保存的表单值），不带就报当前生效的
            wanted = (q.get("host") or [""])[0]
            if wanted:
                return self._json(_probe_ollama_host(wanted))
            return self._json(ollama_status())

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

        if path == "/api/mcp/raw":
            # 给「MCP 管理」弹窗用的原文视图：mcp.json 里有很多我们不拥有的
            # 字段（不同 server 各有各的参数），表单化反而丢信息 —— 直接暴露
            # 原始 JSON，编辑体验对齐 Claude Desktop 的"打开配置文件"。
            p = config_path()
            try:
                raw = p.read_text(encoding="utf-8") if p.is_file() else ""
            except OSError:
                raw = ""
            return self._json({"ok": True, "path": str(p), "text": raw})

        if path == "/api/mcp/presets":
            names = {s.get("name") for s in self._owner.mcp_servers}
            return self._json(
                {
                    "ok": True,
                    "presets": [
                        {
                            "name": item.name,
                            "summary": item.summary,
                            "package": item.package,
                            "bundled": item.bundled_script is not None,
                            "in_config": item.name in names,
                        }
                        for item in PRESETS.values()
                    ],
                }
            )

        # ---- 自定义模型 profile ----
        # GET  /api/models           列表（env 里的 key 原样给，value 脱敏显示）
        # POST /api/models           整份保存（来自管理弹窗）
        if path == "/api/models":
            return self._json(self._models_payload())

        # 当前激活的模型信息（状态栏显示用）
        if path == "/api/model/active":
            data = load_models()
            pid = data.get("active")
            profile = next((p for p in data["profiles"] if p.get("id") == pid), None)
            model = ""
            if profile:
                prefix = _backend_prefix(profile.get("env", {}).get("AGENTD_LLM_BACKEND"))
                model = profile.get("env", {}).get(prefix + "MODEL") or ""
            return self._json(
                {
                    "ok": True,
                    "id": pid,
                    "name": (profile or {}).get("name") if profile else None,
                    "model": model,
                    "has_profile": bool(profile),
                }
            )

        # ---- 会话侧栏 / 续聊 ----
        # /api/sessions        列出已有会话（侧栏用）
        # /api/session/<id>    某会话完整历史（点开看 / 续聊前先画出来）
        # 注意：/api/session/new 和 /api/session/resume 是 POST，不在这里处理。
        if path == "/api/sessions":
            return self._json({"ok": True, "sessions": self._owner.sessions.list_meta()})

        # ---- 空间（Space）：当前激活空间 + 全部空间列表（header 下拉框用）----
        if path == "/api/spaces":
            return self._json(
                {
                    "ok": True,
                    "active": self._owner.current_space_name,
                    "path": self._owner.current_space_dir,
                    "spaces": self._owner.spaces.list_spaces(),
                }
            )

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

    # ---- 模型 profile 的读/写辅助 ----

    @staticmethod
    def _models_masked(data: dict) -> dict:
        """列表给前端时脱敏：只脱「密钥语义」的值（键名含 KEY）。

        MODEL / BASE_URL **不脱敏** —— 它们本来就不敏感，而且编辑表单要回填：
        回填一个掩码串，用户一保存就把掩码写进了真配置。API Key 则相反：
        永远脱敏 + 永不回填，编辑时留空由后端 merge 语义保留旧值。
        """
        profiles = []
        for p in data.get("profiles") or []:
            env = {}
            for k, v in (p.get("env") or {}).items():
                v = str(v)
                if "KEY" in str(k).upper() and len(v) > 6:
                    v = f"{v[:3]}…{v[-3:]}"
                env[k] = v
            profiles.append({**p, "env": env})
        return {"ok": True, "path": str(models_path()), "active": data.get("active"), "profiles": profiles}

    def _models_payload(self) -> dict:
        return self._models_masked(load_models())

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
