"""把示例 MCP server 写进本机 mcp.json，好让 GUI 一分钟内能真跑起工具调用。

默认是**干跑**（只打印将要写入的内容，不动磁盘）：

    python scripts/install_demo_mcp.py
    python scripts/install_demo_mcp.py --write     # 真写（已有的会备份成 .bak）

为什么需要它：mcp.json 里 `command` 得写**绝对路径的解释器**，不能写 `python`
—— 这台机器上 PATH 里的 `python` 是微软商店的占位别名，一跑就报"未安装 Python"。
脚本替你填好 sys.executable，省掉这个坑。

★ 写进去的是"启动 GUI 时用的那个解释器"。所以要用装了 `mcp` 包的那个 venv 跑本脚本
  （例如 ./.venv/Scripts/python.exe）。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from forgeagent.gui.mcp_config import config_path  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
DEMO_SERVER = REPO / "examples" / "echo_mcp_server.py"


def build_config() -> dict:
    return {
        "mcpServers": {
            "demo": {
                "command": sys.executable,
                "args": [str(DEMO_SERVER)],
            }
        }
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="安装示例 MCP 配置")
    parser.add_argument("--write", action="store_true", help="真的写入（默认只打印）")
    parser.add_argument("--path", default=None, help="覆盖 mcp.json 位置")
    args = parser.parse_args()

    if not DEMO_SERVER.is_file():
        print(f"找不到示例 server：{DEMO_SERVER}")
        return 1

    target = Path(args.path) if args.path else config_path()
    text = json.dumps(build_config(), ensure_ascii=False, indent=2)
    print(f"目标文件：{target}")
    print(text)

    if not args.write:
        print("\n（干跑）加 --write 才会真的写。")
        return 0

    if target.is_file():
        backup = target.with_suffix(target.suffix + ".bak")
        shutil.copy2(target, backup)
        print(f"已备份原文件 → {backup}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text + "\n", encoding="utf-8")
    print(f"已写入 {target}")
    print("\n下一步：重启 GUI（forgeagent-gui），侧栏底部应显示「MCP · 1 个：demo」。")
    print("然后问它：「用 echo 工具回显 hello」—— 模型决定调工具时会弹出工具卡片。")
    print("注意：本地模型得真的会调工具才行；不确定就用 python scripts/mcp_e2e.py 验证链路。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
