"""系统原生文件夹选择对话框（跨平台）。

GUI 运行在用户本机，新建空间时要「选一个目录作为工作区」。最贴近 WorkBuddy 体验的
做法是直接调系统原生对话框。后端是 Python HTTP 服务（Electron 壳和 serve 模式共用
同一份），所以对话框也由 Python 这侧调起 —— 不依赖 Electron 的 node 桥，serve 模式
同样可用；也不碰任何业务逻辑，纯粹把"用户选的路径"拿回来。

各平台实现：
  Windows  IFileOpenDialog（shell COM）+ FOS_PICKFOLDERS 真文件夹选取：
           现代资源管理器样式、当前目录本身可确认、OK 按钮写「选择文件夹」、
           没有文件名框。C# 互操作源码内嵌，脚本走 -EncodedCommand
           （utf-16 base64）传，彻底避开路径里的反斜杠 / 引号转义坑；
           初始目录通过环境变量 FORGE_PICK_INIT 喂进去，连路径里的引号都不用拼。
           回落链：IFileOpenDialog → FolderBrowserDialog（样式旧但功能正确）
           —— 只有「对话框坏了」才回落；用户取消就干净返回 None，不弹第二个框。
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

# IFileOpenDialog 的 C# 互操作（Add-Type -TypeDefinition 编译）。
# 为什么不用 OpenFileDialog(ValidateNames=false) 冒充文件夹选择器：
# 那是**文件**对话框 —— 进到空目录没有可选项，「打开」按钮一直灰着，
# 用户根本选不了当前目录（2026-10-17 实测：导航进空的 D:\workspace\test 后卡死）。
# FOS_PICKFOLDERS 才是真正的文件夹选取：当前目录本身可确认，OK 按钮写
# 「选择文件夹」，没有文件名框。shell COM 从 Vista 起就有，零外部依赖。
# 注意：源码里不要出现单引号（PowerShell here-string @'...'@ 的终止符是 '@），
# 中文走 -EncodedCommand（utf-16le base64）编码后传入，不受 ANSI 解码影响。
_CS_FOLDER_PICKER = r'''
using System;
using System.Runtime.InteropServices;

public static class ForgePicker
{
    // CLSID_FileOpenDialog
    [ComImport]
    [Guid("DC1C5A9C-E88A-4DDE-A5A1-60F82A20AEF7")]
    private class FileOpenDialog { }

    [ComImport]
    [Guid("43826D1E-E718-42EE-BC55-A1E261C37BFE")]
    [InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface IShellItem
    {
        void BindToHandler(IntPtr pbc, ref Guid bhid, ref Guid riid, out IntPtr ppv);
        void GetParent(out IShellItem ppsi);
        void GetDisplayName(uint sigdnName, out IntPtr ppszName);
        void GetAttributes(uint sfgaoMask, out uint psfgaoAttribs);
        void Compare(IShellItem psi, uint hint, out int piOrder);
    }

    // IFileDialog : IModalWindow —— 方法顺序即 vtable 顺序，错一个槽位就崩。
    // IID 以 winapi/winsafe/pinvoke 三处对账为准（2026-10-17：尾段是 5FC8，
    // 凭记忆写成 5900 会 QI 失败 E_NOINTERFACE，且报错只说是"转换失败"）
    [ComImport]
    [Guid("42F85136-DB7E-439C-85F1-E4075D135FC8")]
    [InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface IFileDialog
    {
        [PreserveSig] int Show(IntPtr parent);
        void SetFileTypes(uint cFileTypes, IntPtr rgFilterSpec);
        void SetFileTypeIndex(uint iFileType);
        void GetFileTypeIndex(out uint piFileType);
        void Advise(IntPtr pfde, out uint pdwCookie);
        void Unadvise(uint dwCookie);
        void SetOptions(uint fos);
        void GetOptions(out uint pfos);
        void SetDefaultFolder(IShellItem psi);
        void SetFolder(IShellItem psi);
        void GetFolder(out IShellItem ppsi);
        void GetCurrentSelection(out IShellItem ppsi);
        void SetFileName([MarshalAs(UnmanagedType.LPWStr)] string pszName);
        void GetFileName([MarshalAs(UnmanagedType.LPWStr)] out string pszName);
        void SetTitle([MarshalAs(UnmanagedType.LPWStr)] string pszTitle);
        void SetOkButtonLabel([MarshalAs(UnmanagedType.LPWStr)] string pszText);
        void SetFileNameLabel([MarshalAs(UnmanagedType.LPWStr)] string pszLabel);
        void GetResult(out IShellItem ppsi);
        void AddPlace(IShellItem psi, int fdap);
        void SetDefaultExtension([MarshalAs(UnmanagedType.LPWStr)] string pszDefaultExtension);
        void Close(int hr);
        void SetClientGuid(ref Guid guid);
        void ClearClientData();
        void SetFilter(IntPtr pFilter);
    }

    [DllImport("shell32.dll", CharSet = CharSet.Unicode, PreserveSig = false)]
    private static extern void SHCreateItemFromParsingName(
        [MarshalAs(UnmanagedType.LPWStr)] string pszPath,
        IntPtr pbc,
        ref Guid riid,
        out IShellItem ppv);

    private const uint FOS_PICKFOLDERS = 0x20;
    private const uint FOS_FORCEFILESYSTEM = 0x40;
    private const uint FOS_PATHMUSTEXIST = 0x800;
    private const uint SIGDN_FILESYSPATH = 0x80028000;

    private static IFileDialog NewDialog()
    {
        IFileDialog dlg = (IFileDialog)new FileOpenDialog();
        dlg.SetOptions(FOS_PICKFOLDERS | FOS_FORCEFILESYSTEM | FOS_PATHMUSTEXIST);
        dlg.SetTitle("选择空间目录");
        dlg.SetOkButtonLabel("选择文件夹");
        return dlg;
    }

    private static void SetInitialFolder(IFileDialog dlg, string initial)
    {
        if (string.IsNullOrEmpty(initial)) { return; }
        try
        {
            Guid iid = typeof(IShellItem).GUID;
            IShellItem folder;
            SHCreateItemFromParsingName(initial, IntPtr.Zero, ref iid, out folder);
            if (folder != null) { dlg.SetFolder(folder); }
        }
        catch { }  // 初始目录不存在/不可解析：不设，用默认位置
    }

    // 完整交互：返回选中的目录；取消返回 null；COM 异常向上抛（调用方回落）
    public static string Pick(string initial)
    {
        IFileDialog dlg = NewDialog();
        try
        {
            SetInitialFolder(dlg, initial);
            if (dlg.Show(IntPtr.Zero) != 0) { return null; }  // 取消 = 非 S_OK
            IShellItem item;
            dlg.GetResult(out item);
            IntPtr namePtr;
            item.GetDisplayName(SIGDN_FILESYSPATH, out namePtr);
            string path = Marshal.PtrToStringUni(namePtr);
            Marshal.FreeCoTaskMem(namePtr);
            return path;
        }
        finally
        {
            Marshal.ReleaseComObject(dlg);
        }
    }

    // 无头自检：把 Show 以外的每一步真走一遍（编译/COM/vtable/选项/初始目录），
    // 返回 "options=<值>;folder=<none|set|fail>"。tests/test_dialogs.py 靠它
    // 在不开界面的前提下验证互操作声明没写错。
    public static string Probe(string initial)
    {
        IFileDialog dlg = NewDialog();
        try
        {
            uint got;
            dlg.GetOptions(out got);
            string folder = "none";
            if (!string.IsNullOrEmpty(initial))
            {
                folder = "fail";
                try
                {
                    Guid iid = typeof(IShellItem).GUID;
                    IShellItem item;
                    SHCreateItemFromParsingName(initial, IntPtr.Zero, ref iid, out item);
                    if (item != null) { dlg.SetFolder(item); folder = "set"; }
                }
                catch { }
            }
            return "options=" + got.ToString() + ";folder=" + folder;
        }
        finally
        {
            Marshal.ReleaseComObject(dlg);
        }
    }
}
'''


def _win_pick(initial: str | None) -> str | None:
    # IFileOpenDialog + FOS_PICKFOLDERS 真文件夹选取；失败回落 FolderBrowserDialog
    # （功能正确、样式旧）。用户取消 = 干净 None，不触发回落、不弹第二个框。
    # InitialDirectory 走环境变量，零引号拼接。
    # [Console]::OutputEncoding 固定 UTF-8：选中的路径可能含非 ASCII（中文目录名），
    # 不显式设的话重定向 stdout 按本机 OEM 代码页（GBK）出字节，Python 侧按
    # UTF-8 解会乱码甚至崩读取线程（2026-10-17 实测踩到）。
    script = (
        "$ErrorActionPreference = 'Stop'\n"
        "[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)\n"
        "$picked = $null\n"
        "$failed = $null\n"
        "try {\n"
        "    Add-Type -TypeDefinition @'\n"
        + _CS_FOLDER_PICKER
        + "\n'@\n"
        "    $picked = [ForgePicker]::Pick($env:FORGE_PICK_INIT)\n"
        "} catch {\n"
        "    $failed = $_\n"
        "}\n"
        "if ($failed) {\n"
        "    Add-Type -AssemblyName System.Windows.Forms\n"
        "    $d = New-Object System.Windows.Forms.FolderBrowserDialog\n"
        "    $d.Description = '选择空间目录'\n"
        "    if ($env:FORGE_PICK_INIT) { $d.SelectedPath = $env:FORGE_PICK_INIT }\n"
        "    if ($d.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { $d.SelectedPath }\n"
        "} elseif ($picked) {\n"
        "    $picked\n"
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
        # 按字节收：powershell 出错时 stderr 按 GBK 写，text=True 会让读取线程
        # 崩（stderr 变 None、returncode 非零）。这里只用 returncode + stdout，
        # 自己按 UTF-8 解 stdout（脚本里已固定 OutputEncoding），坏字节替换不崩。
        proc = subprocess.run(
            cmd, capture_output=True, timeout=_DIALOG_TIMEOUT, env=env
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    out = (proc.stdout or b"").decode("utf-8", "replace").strip()
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
