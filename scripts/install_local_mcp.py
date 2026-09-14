"""把本地 MCP server 一键装好并写进 mcp.json（P1）。

默认**干跑**：只打印「会做什么、会写成什么」，不动磁盘。真动手要显式加参数：

    # 1. 看有哪些预设（含每个预设的 pip 包 / 用途）
    python scripts/install_local_mcp.py --list

    # 2. 干跑：看计划 + 将要写进 mcp.json 的内容
    python scripts/install_local_mcp.py --servers time,fetch,git

    # 3. 真装（建专用 venv + pip install，需联网）+ 真写配置
    python scripts/install_local_mcp.py --servers time,fetch,git --install --write

    # 4. 验证链路（用 agentd 的 McpHub 真连一遍）
    python scripts/verify_local_mcp.py

    # 5. 重启 GUI，侧栏底部应显示「MCP · N 个：...」

为什么要单独搞一个 venv、还把 command 写成绝对路径：见 `forgeagent/gui/mcp_presets.py`
顶部的"设计取舍"。
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from forgeagent.gui.mcp_config import config_path  # noqa: E402
from forgeagent.gui.mcp_presets import (  # noqa: E402
    PRESETS,
    build_entries,
    find_git_dir,
    merge_config,
    python_bin,
    venv_dir,
)


def _read_json(path: Path) -> dict | None:
    """读已有 mcp.json；不存在/坏了返回 None（不报错，后面按新建处理）。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _print_list() -> None:
    print("可用预设：\n")
    for name, preset in PRESETS.items():
        extra = "（需要 PATH 上有 git）" if preset.needs_git else ""
        print(f"  {name:<7} pip 包 {preset.package:<20} {preset.summary}{extra}")
    print("\n说明：filesystem 故意没做 —— agentd 的原生工具（read_file/glob/grep/"
          "write_file/edit）已经覆盖了本地文件操作，再加一个 MCP server 只会多一轮审批。")
    print(f"专用 venv 目录：{venv_dir()}")


def _ensure_venv(target: Path, *, install: bool) -> Path | None:
    """确保专用 venv 存在，返回它的解释器路径；干跑且不存在时返回 None。"""
    py = python_bin(target)
    if py.is_file():
        print(f"[venv] 已存在：{target}")
        return py
    if not install:
        print(f"[venv] 不存在：{target}（干跑，不创建；加 --install 才会建）")
        return None
    print(f"[venv] 正在创建：{target}")
    subprocess.run([sys.executable, "-m", "venv", str(target)], check=True)
    if not py.is_file():
        raise SystemExit(f"创建 venv 后仍找不到解释器：{py}")
    print(f"[venv] 创建完成：{py}")
    return py


def _pip_install(
    py: Path, packages: list[str], *, install: bool, index_url: str | None = None
) -> None:
    if not packages:
        return
    index_args = ["-i", index_url] if index_url else []
    shown = " ".join([*index_args, *packages])
    if not install:
        print(f"[pip ] 将安装：{shown}（干跑，不执行）")
        return
    print(f"[pip ] 正在安装：{shown}")
    print("       （首次会拉 mcp / httpx / pydantic 等依赖，可能要一两分钟）")
    proc = subprocess.run([str(py), "-m", "pip", "install", *index_args, *packages])
    if proc.returncode != 0:
        raise SystemExit(
            f"pip install 失败（退出码 {proc.returncode}）。"
            "网络不稳时可以换源：--index-url https://pypi.tuna.tsinghua.edu.cn/simple"
        )
    print("[pip ] 安装完成")


def main() -> int:
    parser = argparse.ArgumentParser(description="安装本地 MCP server 并写入 mcp.json")
    parser.add_argument("--list", action="store_true", help="只列出可用预设")
    parser.add_argument(
        "--servers",
        default=",".join(PRESETS),
        help=f"要装的预设，逗号分隔（默认 {','.join(PRESETS)}）",
    )
    parser.add_argument("--venv", default=None, help="覆盖专用 venv 目录")
    parser.add_argument("--path", default=None, help="覆盖 mcp.json 位置")
    parser.add_argument("--install", action="store_true", help="真的建 venv + pip install")
    parser.add_argument(
        "--index-url",
        default=None,
        help="pip 源。默认用 pip 自己的配置；本机默认 PyPI 常被隧道 502，"
        "卡住时试 --index-url https://pypi.tuna.tsinghua.edu.cn/simple",
    )
    parser.add_argument("--write", action="store_true", help="真的写 mcp.json（默认只打印）")
    args = parser.parse_args()

    if args.list:
        _print_list()
        return 0

    names = [s.strip() for s in args.servers.split(",") if s.strip()]
    unknown = [n for n in names if n not in PRESETS]
    if unknown:
        print(f"未知预设：{', '.join(unknown)}")
        print(f"可用：{', '.join(PRESETS)}")
        return 2

    target_venv = Path(args.venv) if args.venv else venv_dir()
    target_cfg = Path(args.path) if args.path else config_path()
    packages = [PRESETS[n].package for n in names]

    print("将要做的事：")
    print(f"  预设      ：{', '.join(names)}")
    print(f"  venv 目录 ：{target_venv}")
    print(f"  配置文件  ：{target_cfg}")
    print()

    # git 预设需要 PATH 上有 git；这台机器上 git 不在 PATH（要手工拼）
    if any(PRESETS[n].needs_git for n in names):
        git_dir = find_git_dir()
        if git_dir is None:
            print("[警告] 选的预设里有需要 git 的，但没找到 git 可执行文件 ——")
            print("       写进 mcp.json 的 env.PATH 不会包含 git，该 server 起不来。")
        else:
            print(f"[ git ] 找到 git 目录，会拼进该 server 的 env.PATH：{git_dir}")
        print()

    py = _ensure_venv(target_venv, install=args.install)
    _pip_install(
        py or python_bin(target_venv),
        packages,
        install=args.install,
        index_url=args.index_url,
    )
    print()

    entries = build_entries(names, target_venv)
    existing = _read_json(target_cfg)
    merged = merge_config(existing, entries)

    if existing:
        kept = [k for k in (existing.get("mcpServers") or {}) if k not in entries]
        if kept:
            print(f"[配置] 会保留原有的其它 server：{', '.join(kept)}")
            print()

    text = json.dumps(merged, ensure_ascii=False, indent=2)
    print(f"mcp.json 内容（{target_cfg}）：")
    print(text)
    print()

    if not args.write:
        print("（干跑）加 --write 才会真的写；加 --install 才会真的装包。")
        return 0

    if not py or not py.is_file():
        print("[跳过] venv 还没建好就跑 --write 是不行的：先加 --install。")
        return 1

    if target_cfg.is_file():
        backup = target_cfg.with_suffix(target_cfg.suffix + ".bak")
        shutil.copy2(target_cfg, backup)
        print(f"已备份原文件 → {backup}")
    target_cfg.parent.mkdir(parents=True, exist_ok=True)
    target_cfg.write_text(text + "\n", encoding="utf-8")
    print(f"已写入 {target_cfg}")
    print()
    print("下一步：")
    print("  1) python scripts/verify_local_mcp.py       # 确认真的连得上、工具列得出")
    print("  2) 重启 GUI（forgeagent-gui），侧栏底部应显示 MCP 摘要")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
