r"""开一个「能看见工具调用」的 GUI demo —— 不需要 Ollama，也不需要会调工具的模型。

为什么能不需要模型：
    启动前给后端设 `AGENTD_LLM_BACKEND=script`（agentd 的脚本回放后端），
    让"模型"按剧本走：第 1 步要调 `demo__echo`，第 2 步把结果写进正文。
    MCP server 是真的（examples/echo_mcp_server.py），工具卡片上的输出是它真的返回的。
    Electron 壳把整个环境变量透传给 Python 后端（electron/main.js 的 `env: process.env`），
    所以这里设的变量能一路到 agentd。

用法（用装了 mcp 的那个 venv 跑）：
    .\.venv\Scripts\python.exe scripts\demo_mcp_gui.py              # 开 Electron 窗口
    .\.venv\Scripts\python.exe scripts\demo_mcp_gui.py --serve      # 只起服务，打印 URL
    .\.venv\Scripts\python.exe scripts\demo_mcp_gui.py --model ollama   # 用真模型（需 ollama serve）

界面上试这句（script 后端不看输入内容，发什么都行）：
    用 echo 工具回显 hello

想同时验链路（不开窗口、退出码可判）：python scripts/mcp_e2e.py
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEMO_SERVER = REPO / "examples" / "echo_mcp_server.py"

# 剧本：先举手要调工具，再出正文。用户发什么内容都不影响这两步。
DEMO_SCRIPT = [
    {"tool_calls": [{"name": "demo__echo", "arguments": {"text": "你好，MCP"}}]},
    {"text": "工具返回：echo: 你好，MCP"},
]


def write_mcp_json(tmp: Path) -> Path:
    """写一份临时 mcp.json（不动你 ~/.forgeagent 里的真配置）。"""
    cfg = {
        "mcpServers": {
            "demo": {
                "command": sys.executable,          # 绝对路径：PATH 上的 python 是商店占位别名
                "args": [str(DEMO_SERVER)],
            }
        }
    }
    path = tmp / "mcp.json"
    path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="MCP GUI demo（默认不依赖任何模型）")
    parser.add_argument("--serve", action="store_true", help="只起本机服务并打印 URL，不开窗口")
    parser.add_argument(
        "--model",
        choices=("script", "ollama", "openai_compat"),
        default="script",
        help="后端 LLM：script=回放剧本（默认，不需要模型服务）；其它走真模型",
    )
    args = parser.parse_args()

    if not DEMO_SERVER.is_file():
        print(f"找不到示例 MCP server：{DEMO_SERVER}")
        return 1

    env = dict(os.environ)
    env["AGENTD_STORE"] = "memory"  # 别把 demo 的对话写进你的真实会话库

    with tempfile.TemporaryDirectory(prefix="forgeagent-demo-") as td:
        env["FORGEAGENT_MCP_CONFIG"] = str(write_mcp_json(Path(td)))

        if args.model == "script":
            env["AGENTD_LLM_BACKEND"] = "script"
            env["AGENTD_SCRIPT_JSON"] = json.dumps(DEMO_SCRIPT, ensure_ascii=False)
        else:
            env["AGENTD_LLM_BACKEND"] = args.model  # ollama / openai_compat 走真模型

        cmd = [sys.executable, "-m", "forgeagent.gui"]
        if args.serve:
            cmd += ["--mode", "serve"]

        print("=" * 66)
        print("MCP demo")
        print(f"  MCP 配置   {env['FORGEAGENT_MCP_CONFIG']}  (server: demo)")
        print(f"  LLM 后端   {env['AGENTD_LLM_BACKEND']}"
              + ("（脚本回放：第1步调工具，第2步出正文）" if args.model == "script" else ""))
        print("  会话存储   memory（不写你的 ~/.agentd）")
        print(f"  启动命令   {' '.join(cmd)}")
        if args.serve:
            # 说明里刻意不写 "UI_READY " 这个字面量（带空格），
            # 免得别人用 `"UI_READY " in line` 这种朴素判断误抓到这行说明。
            print("  说明        下面会打印一行 UI_READY+URL，用浏览器打开它即可")
        else:
            print("  说明        会弹一个 ForgeAgent 窗口；关掉窗口即结束")
        print("=" * 66)
        print("窗口里发这句试试：  用 echo 工具回显 hello")
        print("（应看到一张工具卡片：tool=echo / execute / 完成后输出 echo: 你好，MCP）")
        print("  注意卡片标题是 server 侧原名 echo；demo__echo 只是喂给模型的名字。")
        print("=" * 66, flush=True)

        try:
            return subprocess.call(cmd, cwd=str(REPO), env=env)
        except KeyboardInterrupt:
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
