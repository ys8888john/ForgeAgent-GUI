"""系统原生文件夹选择对话框（跨平台）。

GUI 运行在用户本机，新建空间时要「选一个目录作为工作区」。最贴近 WorkBuddy 体验的
做法是直接调系统原生对话框。后端是 Python HTTP 服务（Electron 壳和 serve 模式共用
同一份），所以对话框也由 Python 这侧调起 —— 不依赖 Electron 的 node 桥，serve 模式
同样可用；也不碰任何业务逻辑，纯粹把"用户选的路径"拿回来。

各平台实现：
  Windows  powershell 的 OpenFileDialog（ValidateNames=false）当文件夹选择器用，
           比老 FolderBrowserDialog 更接近资源管理器样式；脚本走 -EncodedCommand
           （utf-16 base64）传，彻底避开路径里的反斜杠 / 引号转义坑；初始目录通过
           环境变量 FORGE_PICK_INIT 喂进去，连路径里的引号都不用拼。
  macOS    osascript 的 choose folder（返回 POSIX path）。
  Linux   zenity --file-selection --directory，退路 kdialog。

取消、或环境里根本没有可用的对话框（比如无显示的服务端）时一律返回 None —— 调用方
退化为「手动填路径」，不抛异常。
"""

from __future__ import annotations

import base64
import os
import platform
import subprocess

_DIALOG_TIMEOUT = 300  # 秒；用户可能把对话框晾着，给足时间


def _win_pick(initial: str | None) -> str | None:
    # OpenFileDialog 当文件夹选择器：ValidateNames=false 时选中的"文件"其实是目录本身，
    # ShowDialog 返回 OK 后用 Split-Path 取回目录。InitialDirectory 走环境变量，零引号拼接。
    script = (
        "Add-Type -AssemblyName System.Windows.Forms\n"
        "$d = New-Object System.Windows.Forms.OpenFileDialog\n"
        "$d.Title = '选择空间目录'\n"
        "$d.ValidateNames = $false\n"
        "$d.CheckFileExists = $false\n"
        "$d.CheckPathExists = $true\n"
        "if ($env:FORGE_PICK_INIT) { $d.InitialDirectory = $env:FORGE_PICK_INIT }\n"
        "if ($d.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {\n"
        "    Split-Path $d.FileName\n"
        "}\n"
    )
    cmd = [
        "powershell",
        "-NoProfile",
        "-NonInteractive",
        "-EncodedCommand",
        base64.b64encode(script.encode("utf-16-le")).decode("ascii"),
    ]
    env = dict(os.environ)
    init = os.path.expanduser(initial) if initial else None
    if init:
        env["FORGE_PICK_INIT"] = init
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=_DIALOG_TIMEOUT, env=env
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    out = (proc.stdout or "").strip()
    return out or None


def _mac_pick(initial: str | None) -> str | None:
    initial = os.path.expanduser(initial) if initial else None
    # default location 只接受存在的目录引用；不存在就退到 /，避免出现 POSIX file 报错
    if initial and os.path.isdir(initial):
        default = f' default location (POSIX file "{initial}")'
    else:
        default = ""
    script = (
        'try\n'
        f'  set chosen to POSIX path of (choose folder with prompt "选择空间目录"{default})\n'
        "  return chosen\n"
        "end try\n"
    )
    try:
        proc = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=_DIALOG_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    out = (proc.stdout or "").strip()
    return out or None


def _linux_pick(initial: str | None) -> str | None:
    initial = os.path.expanduser(initial) if initial else os.path.expanduser("~")
    try:
        proc = subprocess.run(
            ["zenity", "--file-selection", "--directory", f"--filename={initial}/"],
            capture_output=True,
            text=True,
            timeout=_DIALOG_TIMEOUT,
        )
        if proc.returncode == 0:
            out = (proc.stdout or "").strip()
            if out:
                return out
        # 退路：kdialog
        proc2 = subprocess.run(
            ["kdialog", "--getexistingdirectory", initial],
            capture_output=True,
            text=True,
            timeout=_DIALOG_TIMEOUT,
        )
        if proc2.returncode == 0:
            out = (proc2.stdout or "").strip()
            if out:
                return out
    except (OSError, subprocess.SubprocessError):
        return None
    return None


def pick_directory(initial: str | None = None) -> str | None:
    """打开系统原生文件夹选择对话框，返回选中的绝对路径；取消/无对话框可用返回 None。"""
    name = platform.system()
    if name == "Windows":
        return _win_pick(initial)
    if name == "Darwin":
        return _mac_pick(initial)
    return _linux_pick(initial)
