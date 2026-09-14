"""本地 MCP server 预设（P1）：装哪个 pip 包、怎么拼出 mcp.json 里的一条 server 条目。

为什么单独一个模块：装（`scripts/install_local_mcp.py`）、验（`scripts/verify_local_mcp.py`）、
测（`tests/test_mcp_presets.py`）三处都要用同一份定义，散开写必然会漂移。

设计取舍（都是这台机器上实测出来的，别照抄网上的写法）：

1. **只收纯 Python 的官方 server，用「专用 venv + 绝对路径解释器」启动。**
   网上常见的 `"command": "uvx", "args": ["mcp-server-time"]` 在这台机器上跑不了
   —— 没有 uv/uvx；`npx` 那条路也被沙箱安全策略拦死（`npm view` 直接 ACCESS_DENIED）。
   所以改成：装进一个专用 venv，`command` 写它的解释器绝对路径，`args` 写 `-m <模块>`。

2. **专用 venv（`~/.forgeagent/mcp-venv`）而不是项目 `.venv`。**
   `mcp-server-fetch` 会拖进 httpx / readabilipy / markdownify / protego 一串依赖，
   装进项目 venv 有和 GUI 自身依赖打架的风险（测试基线会飘）。放在 `~/.forgeagent/`
   下，和 `mcp.json` 同一个目录，互相不干扰。

3. **不提供 filesystem 预设。** agentd 的原生工具（read_file / glob / grep / write_file / edit）
   已经把本地文件操作覆盖了；再加一个 filesystem MCP server，只是让模型多一个选择、
   多一次审批往返，是负收益。
"""  # noqa: D400

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

# 专用 venv 的目录名，放在 ~/.forgeagent/ 下（与 mcp.json 同级）
DEFAULT_VENV_DIRNAME = "mcp-venv"

# probe_args 里可以写这个占位符，verify 脚本会替换成"当前工作目录"
CWD_PLACEHOLDER = "{cwd}"


@dataclass(frozen=True)
class Preset:
    """一个可一键安装的本地 MCP server。"""

    name: str
    """mcp.json 里的 server 名（也是工具名前缀，见 agentd 的 `{server}__{tool}`）。"""

    package: str
    """pip 包名。"""

    module: str
    """`python -m` 用的模块名。"""

    summary: str
    """一句话说明，给 `--list` 和 README 用。"""

    needs_git: bool = False
    """该 server 会 fork git 子进程，需要 PATH 上有 git。"""

    probe_tool: str | None = None
    """verify 脚本用来验证"真的能调用"的只读工具名；None = 只列工具不调用。"""

    probe_args: Mapping[str, Any] = field(default_factory=dict)
    """探针工具的参数；值里可以含 CWD_PLACEHOLDER。"""


PRESETS: dict[str, Preset] = {
    "time": Preset(
        name="time",
        package="mcp-server-time",
        module="mcp_server_time",
        summary="时间/时区换算、时间加减",
        probe_tool="get_current_time",
        probe_args={"timezone": "Asia/Shanghai"},
    ),
    "fetch": Preset(
        name="fetch",
        package="mcp-server-fetch",
        module="mcp_server_fetch",
        summary="抓取网页并转成 markdown",
        probe_tool="fetch",
        probe_args={"url": "https://example.com"},
    ),
    "git": Preset(
        name="git",
        package="mcp-server-git",
        module="mcp_server_git",
        summary="git 仓库查询（status/log/diff/show）",
        needs_git=True,
        probe_tool="git_status",
        probe_args={"repo_path": CWD_PLACEHOLDER},
    ),
}
"""预设表。key 是命令行里用的短名（`--servers time,fetch`）。"""


def config_root(home: str | Path | None = None) -> Path:
    """`~/.forgeagent`（和 mcp_config.config_path() 的默认目录保持一致）。"""
    return (Path(home) if home is not None else Path.home()) / ".forgeagent"


def venv_dir(home: str | Path | None = None) -> Path:
    """专用 venv 位置。"""
    return config_root(home) / DEFAULT_VENV_DIRNAME


def python_bin(venv: str | Path, *, platform: str | None = None) -> Path:
    """venv 里的解释器路径。Windows 是 Scripts/python.exe，POSIX 是 bin/python。"""
    plat = sys.platform if platform is None else platform
    sub = "Scripts/python.exe" if plat == "win32" else "bin/python"
    return Path(venv) / sub


def find_git_dir() -> Path | None:
    """找一个含 git 可执行文件的目录，用来往 PATH 前面补。

    先看 PATH；这台机器上 git **不在** PATH 里（只在 PortableGit 目录），
    所以再兜底扫一遍 WorkBuddy 自带的 PortableGit。找不到返回 None
    —— 调用方应该打个警告，而不是编一个路径出来。
    """
    found = shutil.which("git")
    if found:
        return Path(found).parent

    base = Path.home() / ".workbuddy" / "binaries" / "PortableGit" / "versions"
    if base.is_dir():
        # 版本号目录按名字倒序，取最新的那个
        for version in sorted((p for p in base.iterdir() if p.is_dir()), reverse=True):
            for rel in ("cmd/git.exe", "bin/git.exe"):  # cmd/ 是官方布局，bin/ 兜底
                cand = version / rel
                if cand.is_file():
                    return cand.parent
    return None


def _dedupe(entries: list[str]) -> list[str]:
    """去空项 + 保序去重。"""
    seen: set[str] = set()
    out: list[str] = []
    for item in entries:
        if not item or item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def path_with_git(base_path: str | None = None) -> str:
    """把 git 目录拼到 PATH 最前面，顺手去重去空。

    去重不是洁癖：实测从某些 shell 继承下来的 PATH 里同一段会重复出现好几遍
    （本机 Bash shim 下 `safe-bin`、node 目录各出现两次），原样写进 mcp.json
    会得到一个又长又脏的值。
    """
    raw = os.environ.get("PATH", "") if base_path is None else base_path
    entries = _dedupe(raw.split(os.pathsep) if raw else [])
    git_dir = find_git_dir()
    if git_dir is not None and str(git_dir) not in entries:
        entries.insert(0, str(git_dir))
    return os.pathsep.join(entries)


def server_entry(
    preset: Preset,
    venv: str | Path,
    *,
    git_path: str | None = None,
) -> dict:
    """拼出 mcp.json 里的一条 server 配置。

    env 只在需要时给。**不要习惯性补 env**：MCP SDK 的 stdio 客户端是
    `get_default_environment() | (server.env or {})`，而 PATH / SystemRoot / USERPROFILE
    这些本来就在白名单里、会从 agentd 进程继承。只有「agentd 的 PATH 上没有 git」
    这种机器才需要在这里显式把 git 目录塞进去。
    """
    entry: dict[str, Any] = {
        "command": str(python_bin(venv)),
        "args": ["-m", preset.module],
    }
    if preset.needs_git:
        entry["env"] = {"PATH": path_with_git(git_path)}
    return entry


def build_entries(
    names: list[str] | tuple[str, ...],
    venv: str | Path,
    *,
    git_path: str | None = None,
) -> dict[str, dict]:
    """按短名列表生成 `{server名: 条目}`。未知短名抛 KeyError。"""
    out: dict[str, dict] = {}
    for name in names:
        preset = PRESETS.get(name)
        if preset is None:
            raise KeyError(name)
        out[preset.name] = server_entry(preset, venv, git_path=git_path)
    return out


def merge_config(
    existing: Mapping[str, Any] | None,
    entries: Mapping[str, Mapping[str, Any]],
) -> dict:
    """把新条目合进已有配置，保留别人写进去的 server 和其它顶层字段。

    刻意做「合并」而不是「覆盖」：用户可能已经手配了 echo demo 或公司的远程
    server，一键安装不该把它们抹掉 —— 同名的才覆盖。
    """
    config: dict[str, Any] = dict(existing) if isinstance(existing, Mapping) else {}
    raw_servers = config.get("mcpServers")
    servers: dict[str, Any] = dict(raw_servers) if isinstance(raw_servers, Mapping) else {}
    for name, entry in entries.items():
        servers[name] = dict(entry)
    config["mcpServers"] = servers
    return config


__all__ = [
    "CWD_PLACEHOLDER",
    "DEFAULT_VENV_DIRNAME",
    "PRESETS",
    "Preset",
    "build_entries",
    "config_root",
    "find_git_dir",
    "merge_config",
    "path_with_git",
    "python_bin",
    "server_entry",
    "venv_dir",
]
