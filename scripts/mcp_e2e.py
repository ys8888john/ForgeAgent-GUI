"""MCP 端到端验证：GUI 侧配置 → ACP → agentd → 真 stdio MCP server → 工具结果回来。

为什么不用真模型：
    本机 Ollama 不一定在跑，就算在跑，小模型也不一定会吐 tool_calls。
    "MCP 通不通"不该赌在模型的工具调用能力上。所以给 agentd 塞
    AGENTD_LLM_BACKEND=script + 一段脚本：第 1 步"举手"调 echo__echo、
    第 2 步写正文。整条链路上真实的代码路径一个都不跳过：

        forgeagent.gui.mcp_config.load_mcp_servers   （读 mcp.json → ACP 结构）
        forgeagent.acp_client.AcpClient              （session/new 带 mcpServers）
        agentd.transports.acp_stdio                  （mcp_servers → 内核）
        agentd.kernel.modes.agent.AgentMode          （工具循环）
        agentd.kernel.mcp.McpHub                     （真连 stdio MCP server）
        ToolCallStart/Done → acp.start_tool_call/update_tool_call
        forgeagent.acp_client 的 reducer            （折成 Turn.tools）

用法：
    python scripts/mcp_e2e.py
    python scripts/mcp_e2e.py --agentd-repo D:\\workspace\\Agentd

退出码：0 全通，1 有失败。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from forgeagent.acp_client import AcpClient  # noqa: E402
from forgeagent.gui.mcp_config import load_mcp_servers  # noqa: E402

OK, BAD = "  [OK]", "  [FAIL]"
PROMPT = "用 echo 工具回显：端到端"


def section(title: str) -> None:
    print(f"\n{title}")
    print("-" * len(title))


def build_mcp_json(tmp: Path, agentd_repo: Path) -> Path:
    """写一份 mcp.json，指向 Agentd 仓库里的测试 echo server。"""
    echo_server = agentd_repo / "tests" / "_echo_mcp_server.py"
    cfg = {
        "mcpServers": {
            "echo": {
                "command": sys.executable,
                "args": [str(echo_server)],
                "cwd": str(agentd_repo),
            }
        }
    }
    path = tmp / "mcp.json"
    path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def build_script() -> str:
    """agentd 侧的回放脚本：先要工具，再出正文。"""
    return json.dumps(
        [
            {"tool_calls": [{"name": "echo__echo", "arguments": {"text": "端到端"}}]},
            {"text": "工具说：echo: 端到端"},
        ],
        ensure_ascii=False,
    )


async def run(agentd_repo: Path) -> int:
    echo_server = agentd_repo / "tests" / "_echo_mcp_server.py"
    if not echo_server.is_file():
        print(f"{BAD} 找不到 echo MCP server：{echo_server}")
        return 1

    with tempfile.TemporaryDirectory(prefix="forgeagent-mcp-e2e-") as td:
        tmp = Path(td)
        mcp_json = build_mcp_json(tmp, agentd_repo)

        # ---- 第 1 跳：GUI 侧的配置读取 ----
        section("第 1 跳：读 mcp.json（GUI 侧）")
        servers = load_mcp_servers(mcp_json)
        if not servers or servers[0].get("name") != "echo":
            print(f"{BAD} load_mcp_servers 没解析出 echo：{servers}")
            return 1
        print(f"{OK} 解析出 {len(servers)} 个 server：{[s['name'] for s in servers]}")

        # 子进程环境：继承 + 覆盖成 script 后端
        child_env = dict(os.environ)
        child_env["AGENTD_LLM_BACKEND"] = "script"
        child_env["AGENTD_SCRIPT_JSON"] = build_script()
        child_env["AGENTD_STORE"] = "memory"  # 别往用户真实的会话库里写测试数据

        # ---- 第 2 跳：ACP → agentd → MCP → 回来 ----
        section("第 2 跳：ACP（session/new 带 mcpServers）→ agentd → MCP server")
        client = AcpClient(
            command=[sys.executable, "-m", "agentd.server"],
            cwd=str(agentd_repo),
            env=child_env,
            mcp_servers=servers,
        )
        try:
            await client.start()
        except Exception as exc:  # noqa: BLE001
            print(f"{BAD} 拉起 agentd 失败：{type(exc).__name__}: {exc}")
            for line in client.stderr_lines[-10:]:
                print(f"       | {line}")
            return 1
        print(f"{OK} 握手成功，会话 {client.session_id}")

        turn = None
        try:
            async for t in client.prompt(PROMPT):
                turn = t
        except Exception as exc:  # noqa: BLE001
            print(f"{BAD} prompt 失败：{type(exc).__name__}: {exc}")
            for line in client.stderr_lines[-10:]:
                print(f"       | {line}")
            await client.close()
            return 1

        logs = list(client.stderr_lines)
        await client.close()

        if turn is None:
            print(f"{BAD} 一个事件都没收到")
            return 1
        if turn.error:
            print(f"{BAD} agentd 报错：{turn.error}")
            return 1

        # ---- 第 3 跳：工具事件有没有被 reduce 出来 ----
        section("第 3 跳：工具卡片状态（GUI reducer）")
        if not turn.tools:
            print(f"{BAD} 没有一个工具调用事件。agentd 日志末尾：")
            for line in logs[-12:]:
                print(f"       | {line}")
            return 1

        ok = True
        for t in turn.tools:
            print(f"{OK} 工具 {t.call_id[:12]}  title={t.title!r} kind={t.kind!r} "
                  f"status={t.status!r} output={t.output!r}")
            if t.title != "echo":
                print(f"{BAD} 工具标题应为 server 侧原名 echo，实为 {t.title!r}")
                ok = False
            if t.status != "completed":
                print(f"{BAD} 工具状态应为 completed，实为 {t.status!r}")
                ok = False
            if t.output != "echo: 端到端":
                print(f"{BAD} 工具输出应来自真 MCP server（echo: 端到端），实为 {t.output!r}")
                ok = False
        if not ok:
            print("       agentd 日志末尾：")
            for line in logs[-15:]:
                print(f"       | {line}")

        section("第 4 跳：最终正文")
        if "echo: 端到端" not in turn.text:
            print(f"{BAD} 正文里看不到工具结果：{turn.text!r}")
            ok = False
        else:
            print(f"{OK} 正文：{turn.text!r}")
        if turn.stop_reason not in ("end_turn", ""):
            print(f"{BAD} 停止原因异常：{turn.stop_reason!r}")
            ok = False

        if ok:
            print("\n全通：mcp.json → ACP → agentd → MCP server → 工具事件 → 界面 reducer")
        return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="MCP 端到端验证（不用真模型）")
    parser.add_argument(
        "--agentd-repo",
        default=os.getenv("AGENTD_REPO", r"D:\workspace\Agentd"),
        help="agentd 仓库路径（要能找到 tests/_echo_mcp_server.py）",
    )
    args = parser.parse_args()
    return asyncio.run(run(Path(args.agentd_repo)))


if __name__ == "__main__":
    raise SystemExit(main())
