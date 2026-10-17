"""dialogs._win_pick 的对话框选型 + 接线回归。

2026-10-17 用户实测：OpenFileDialog(ValidateNames=false) 冒充文件夹选择器，
导航进空目录（D:\\workspace\\test，0 项）后没有可选项，「打开」按钮一直灰着，
选不了当前目录。修复是换成 IFileOpenDialog + FOS_PICKFOLDERS（真文件夹选取）。

三个层次各钉一层：
  1. 选型：编码后的 PowerShell 里必须出现 FOS_PICKFOLDERS，且不出现
     ValidateNames（文件对话框冒充文件夹的签名）—— bug 的签名就是"用文件对话框"；
  2. 接线：-EncodedCommand 通道、初始目录走环境变量、stdout 剥离、
     非零退出码/超时/OSError 一律 None（调用方退化成手动填路径）；
  3. 互操作：真跑一遍 powershell，用 C# 侧的无头 Probe（不开界面）
     验证 COM/vtable/选项/初始目录都没写错 —— 这是唯一能在 CI 里
     验证 shell COM 互操作的办法。
"""

from __future__ import annotations

import base64
import os
import subprocess
import sys

import pytest

from forgeagent.gui import dialogs


def _decoded_script() -> str:
    """把 _win_pick 实际会执行的 -EncodedCommand 解回 PowerShell 源码。"""
    captured: dict = {}

    def fake_run(cmd, **kwargs):  # noqa: ANN001 - 只抓参数，不执行
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

    real_run = subprocess.run
    subprocess.run = fake_run  # type: ignore[assignment]
    try:
        dialogs._win_pick("D:\\ws")
    finally:
        subprocess.run = real_run  # type: ignore[assignment]
    cmd = captured["cmd"]
    idx = cmd.index("-EncodedCommand")
    return base64.b64decode(cmd[idx + 1]).decode("utf-16-le")


def test_win_pick_script_is_a_real_folder_picker():
    """选型回归：FOS_PICKFOLDERS 在场，文件对话框冒充 hack 不在场。"""
    script = _decoded_script()
    assert "FOS_PICKFOLDERS" in script          # 真文件夹选取
    assert "SHCreateItemFromParsingName" in script  # 初始目录支持还在
    assert "选择文件夹" in script                 # OK 按钮标签
    # 文件对话框冒充文件夹的签名（bug 本体）：这几个一出现就是倒退
    assert "ValidateNames" not in script
    assert "Split-Path" not in script
    # 回落链还在：FolderBrowserDialog（功能正确的旧样式）
    assert "FolderBrowserDialog" in script
    # 取消 vs 坏掉要分开：只有 catch 到异常才回落，取消不弹第二个框
    assert "$failed" in script and "elseif ($picked)" in script


def test_win_pick_wiring(monkeypatch):
    """接线：命令形状、环境变量传初始目录、返回值剥离、失败一律 None。"""
    seen: dict = {}

    def fake_run(cmd, **kwargs):  # noqa: ANN001
        seen["cmd"] = cmd
        seen["kwargs"] = kwargs
        return subprocess.CompletedProcess(cmd, 0, stdout=b"D:\\ws\\test\n", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)

    # 正常：stdout 剥干净返回
    assert dialogs._win_pick("D:\\ws") == "D:\\ws\\test"
    cmd = seen["cmd"]
    assert cmd[1] == "-NoProfile" and cmd[3] == "-EncodedCommand"
    # 初始目录走环境变量（路径里的引号/反斜杠零拼接）
    assert seen["kwargs"]["env"]["FORGE_PICK_INIT"] == "D:\\ws"

    # 没给初始目录：不塞环境变量
    dialogs._win_pick(None)
    assert "FORGE_PICK_INIT" not in seen["kwargs"]["env"]

    # 非零退出码（对话框环境没有 powershell 等）→ None
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, stdout=b"", stderr=b"x"),
    )
    assert dialogs._win_pick(None) is None

    # 超时（用户把对话框晾着超过 300s）→ None
    def raise_timeout(cmd, **kwargs):  # noqa: ANN001
        raise subprocess.TimeoutExpired(cmd, 300)

    monkeypatch.setattr(subprocess, "run", raise_timeout)
    assert dialogs._win_pick(None) is None

    # powershell 不存在 → None（不抛给调用方）
    def raise_oserror(cmd, **kwargs):  # noqa: ANN001
        raise OSError("not found")

    monkeypatch.setattr(subprocess, "run", raise_oserror)
    assert dialogs._win_pick(None) is None


@pytest.mark.skipif(sys.platform != "win32", reason="Windows shell COM 专测")
def test_probe_verifies_interop_headlessly(tmp_path):
    """无头自检：真编译 C# + 建 COM 对象 + 选项/初始目录 round-trip。

    这是唯一能在不开界面的前提下验证 IFileDialog 互操作声明的办法 ——
    vtable 顺序写错只会在 Show/GetResult 崩，编译期发现不了，所以
    C# 侧提供一个 Probe（Show 以外全真走）。options 期望
    0x20|0x40|0x800 = 2144（PICKFOLDERS|FORCEFILESYSTEM|PATHMUSTEXIST）。

    按字节收输出：powershell 出错时 stderr 按 GBK 写，text=True 会让
    subprocess 的读取线程崩（stderr 变 None，2026-10-17 实测）。
    """
    workdir = tmp_path / "space"
    workdir.mkdir()
    script = (
        "$ErrorActionPreference = 'Stop'\n"
        "[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)\n"
        "Add-Type -TypeDefinition @'\n"
        + dialogs._CS_FOLDER_PICKER
        + "\n'@\n"
        "[ForgePicker]::Probe($env:FORGE_PICK_INIT)\n"
    )

    def run_probe(init: str) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env["FORGE_PICK_INIT"] = init
        return subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-EncodedCommand",
                base64.b64encode(script.encode("utf-16-le")).decode("ascii"),
            ],
            capture_output=True,
            timeout=120,
            env=env,
        )

    proc = run_probe(str(workdir))
    err = (proc.stderr or b"").decode("gbk", "replace")
    assert proc.returncode == 0, f"powershell 失败：{err[-800:]}"
    out = (proc.stdout or b"").decode("utf-8", "replace").strip()
    # shell 的 FileOpenDialog 对象会自带 FOS_FILEMUSTEXIST(0x1000)，
    # SetOptions 后仍在（实测 6240 = 0x1860）—— 按位校验三个关键位在就行，
    # 别钉精确值；PICKFOLDERS 位在 = 真的是文件夹选取模式
    options = int(out.split("options=")[1].split(";")[0])
    assert options & 0x20, out   # FOS_PICKFOLDERS
    assert options & 0x40, out   # FOS_FORCEFILESYSTEM
    assert options & 0x800, out  # FOS_PATHMUSTEXIST
    assert "folder=set" in out, out          # 初始目录 SHCreateItemFromParsingName 成功
    # 初始目录不存在时不许崩：Probe 自己吞掉，回落 "fail"
    proc2 = run_probe(str(tmp_path / "no_such_dir"))
    err2 = (proc2.stderr or b"").decode("gbk", "replace")
    assert proc2.returncode == 0, f"坏初始目录也崩了：{err2[-800:]}"
    assert "folder=fail" in (proc2.stdout or b"").decode("utf-8", "replace").strip()
