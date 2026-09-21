"""入口：python -m forgeagent.gui（或装完后直接 forgeagent-gui）

前端主形态是 **web（Chromium）** —— 参考 WorkBuddy：WorkBuddy 本身就是个
Electron 应用，UI 是它自带 Chromium 渲染的 web 页面。所以我们把
forgeagent/gui/assets/index.html 当成唯一要打磨的产品界面，用 Chromium 渲染它。

一种窗口载体 + 一种无窗口：
    electron（默认）  Electron 壳包 index.html，自带 Chromium（带软件渲染兜底
                      swiftshader），绕开 Windows 上 Edge WebView2 的浏览器进程崩溃
                      （这台机器 GPU/driver 有坑）。最像 WorkBuddy 的跑法。
    serve             只起本机 UI 服务、不开窗口，把 URL 交给外部渲染
                      （浏览器 / IDE 预览面板 / 别的 Electron）。

    FORGEAGENT_GUI_MODE=electron forgeagent-gui    也能切。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


def _find_node_dir() -> str | None:
    """找 node 可执行文件所在目录，塞进子进程 PATH 让 electron.cmd 能调 `node`。

    electron.cmd 内部用 `node` 起 cli.js；本机 node 往往不在 PATH
    （只有 WorkBuddy 托管的全路径可用），先 which 一下，没有再兜底去找
    WorkBuddy 托管 node 的版本目录。
    """
    found = shutil.which("node")
    if found:
        return os.path.dirname(os.path.abspath(found))
    candidates = list(Path.home().glob(".workbuddy/binaries/node/versions/*/node.exe"))
    if candidates:
        return str(candidates[0].parent)
    return None


def _launch_electron(*, cwd: str | None) -> None:
    """拉起 electron/ 壳（自带 Chromium 渲染 index.html）。阻塞到窗口关闭。"""
    # main.js 用 path.resolve(__dirname, "..") 当 PROJECT_ROOT，所以 electron 壳
    # 应放在项目根（D:\workspace\ForgeAgent-GUI\electron）。这里优先项目根，
    # 也兼容旧布局 forgeagent/gui/electron。
    gui_dir = Path(__file__).resolve().parent  # .../forgeagent/gui
    project_root = gui_dir.parent.parent  # .../ForgeAgent-GUI
    candidates = [project_root / "electron", gui_dir / "electron"]
    electron_dir = next((p for p in candidates if p.is_dir()), None)
    if electron_dir is None:
        raise SystemExit(
            "找不到 Electron 壳目录：试过 " + " 和 ".join(str(p) for p in candidates)
        )

    # electron 包本体（含 Chromium 二进制）可能没装完整：npm install 的 postinstall
    # 要额外下载 Chromium，被网络挡住时只会留下 .bin 里的悬空 shim，跑起来报
    # "Cannot find module electron"。这里先点破，省得人去猜。
    electron_pkg = electron_dir / "node_modules" / "electron"
    if not electron_pkg.is_dir():
        npm = shutil.which("npm")
        npm_cmd = f'"{npm}" install' if npm else "npm install"
        raise SystemExit(
            f"Electron 包没装完整（缺 {electron_pkg}）。先装依赖：\n"
            f"  cd {electron_dir}\n"
            f"  {npm_cmd}\n"
            f"若 Chromium 下载被墙（GitHub 直连超时），用镜像重试：\n"
            f"  export ELECTRON_MIRROR=https://npmmirror.com/mirrors/electron/\n"
            f"  {npm_cmd}"
        )

    bin_name = "electron.cmd" if os.name == "nt" else "electron"
    local_bin = electron_dir / "node_modules" / ".bin" / bin_name

    env = dict(os.environ)
    # electron.cmd 内部用 `node` 起 cli.js；本机 node 往往不在 PATH（只有 WorkBuddy
    # 托管的全路径可用），先把 node 目录塞进 PATH，否则 electron.cmd 报
    # "node 不是内部或外部命令"。
    node_dir = _find_node_dir()
    if node_dir:
        env["PATH"] = node_dir + os.pathsep + env.get("PATH", "")
    else:
        print(
            "警告：PATH 里找不到 node，Electron 可能起不来（需先让 node 可用）。",
            file=sys.stderr,
        )

    # 把当前解释器交给 Electron 主进程，它再 spawn `python -m forgeagent.gui --mode serve`
    # （那个 venv 里装了 agentd；cwd 设到项目根，forgeagent 包才可导入）。
    env["FORGEAGENT_PYTHON"] = sys.executable
    if cwd:
        env["FORGEAGENT_CWD"] = cwd

    if local_bin.exists():
        cmd = [str(local_bin), str(electron_dir)]
    else:
        electron = shutil.which("electron")
        cmd = [electron, str(electron_dir)] if electron else ["npx", "electron", str(electron_dir)]

    # 阻塞到 Electron 退出（窗口关了才返回），顺便在退出时收掉 Python 后端。
    subprocess.run(cmd, cwd=str(electron_dir), env=env, check=False)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="forgeagent-gui", description="ForgeAgent 图形界面（独立窗口）"
    )
    parser.add_argument("--cwd", default=None, help="agentd 的工作目录，默认当前目录")
    parser.add_argument(
        "--mode",
        default=os.environ.get("FORGEAGENT_GUI_MODE", "electron"),
        choices=("electron", "serve"),
        help="前端载体：electron（默认，自带 Chromium 的独立窗口）/ serve（只起服务，交给浏览器等外部渲染）",
    )
    args = parser.parse_args()

    if args.mode == "serve":
        from .serve import run_serve

        run_serve(cwd=args.cwd)
        return

    # 默认 electron
    _launch_electron(cwd=args.cwd)


if __name__ == "__main__":
    main()
