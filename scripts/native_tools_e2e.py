"""原生工具端到端验证：GUI 侧审批 → ACP → agentd 原生工具 → 结果 / 拒绝 → 界面 reducer。

为什么不用真模型：
    本机 Ollama 不一定在跑，就算在跑，小模型也不一定会按我们要的顺序吐 tool_calls。
    "原生工具通不通、审批拦不拦得住"不该赌在模型的工具调用能力上。
    所以给 agentd 塞 AGENTD_LLM_BACKEND=script + 一段脚本，
    链路上真实的代码路径一个都不跳过：

        agentd.kernel.tools.NativeToolbox        （真读写文件、真跑命令）
        agentd.kernel.modes.agent.AgentMode      （kind 映射 + 审批闸门）
        agentd.kernel.tools.needs_approval       （哪些工具要审批）
        agentd.transports.acp_stdio              （request_permission 的 SDK 帧）
        forgeagent.acp_client                    （解析 + 回帧 + reducer）
        forgeagent.gui.bridge                    （permission 事件）

    这一步是唯一能证明"两个仓库对审批帧的理解真的一致"的东西 ——
    单测两边各自打自己造的帧，字段名对不上也测不出来。

用法：
    python scripts/native_tools_e2e.py              # 允许：文件真被写出来
    python scripts/native_tools_e2e.py --deny       # 拒绝：文件绝不能存在

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

OK, BAD = "  [OK]", "  [FAIL]"
PROMPT = "读一下文件并写点东西"
TARGET = "notes.txt"


def section(title: str) -> None:
    print(f"\n{title}")
    print("-" * len(title))


def build_script() -> str:
    """agentd 侧的回放脚本：读+搜（不需审批）→ 写（需审批）→ 收尾正文。"""
    return json.dumps(
        [
            {
                "tool_calls": [
                    {"name": "read_file", "arguments": {"path": "seed.txt"}},
                    {"name": "glob", "arguments": {"pattern": "*.txt"}},
                ]
            },
            {
                "tool_calls": [
                    {
                        "name": "write_file",
                        "arguments": {"path": TARGET, "content": "agentd 写的\n"},
                    }
                ]
            },
            {"text": "都做完了"},
        ],
        ensure_ascii=False,
    )


async def run(agentd_repo: Path, allow: bool) -> int:
    with tempfile.TemporaryDirectory(prefix="forgeagent-native-e2e-") as td:
        work = Path(td)
        (work / "seed.txt").write_text("种子内容\n", encoding="utf-8")

        child_env = dict(os.environ)
        child_env["AGENTD_LLM_BACKEND"] = "script"
        child_env["AGENTD_SCRIPT_JSON"] = build_script()
        child_env["AGENTD_STORE"] = "memory"      # 别往用户真实的会话库里写测试数据
        child_env["AGENTD_DOTENV"] = "__nonexistent__"
        child_env["AGENTD_TOOLS"] = "native"      # 显式打开原生工具
        child_env["AGENTD_TOOLS_APPROVE"] = "native"

        # ---- 第 1 跳：拉起 agentd，session cwd 指到临时工作区 ----
        # 工具的相对路径是相对**会话 cwd**解析的，所以这里必须指到 work，
        # 而不是 agentd 仓库 —— 否则测试会去动仓库里的真文件。
        section("第 1 跳：拉起 agentd（cwd = 临时工作区）")
        client = AcpClient(
            command=[sys.executable, "-m", "agentd.server"],
            cwd=str(work),
            env=child_env,
        )
        try:
            await client.start()
        except Exception as exc:  # noqa: BLE001
            print(f"{BAD} 拉起 agentd 失败：{type(exc).__name__}: {exc}")
            for line in client.stderr_lines[-10:]:
                print(f"       | {line}")
            return 1
        print(f"{OK} 握手成功，会话 {client.session_id}")

        # ---- 第 2 跳：审批回调（模拟界面点按钮）----
        seen: list = []

        def on_permission(req):
            seen.append(req)
            pick = req.allow_ids[0] if (allow and req.allow_ids) else req.reject_id
            print(
                f"{OK} 收到审批请求 id={req.request_id} tool={req.title!r} "
                f"kind={req.kind!r} detail={req.detail!r}"
            )
            print(f"       选项={[o.get('optionId') for o in req.options]} → 选 {pick!r}")
            asyncio.ensure_future(client.answer_permission(req.request_id, pick))

        client.on_permission = on_permission

        # ---- 第 3 跳：跑一轮 ----
        section("第 2 跳：ACP session/prompt（工具循环 + 审批闸门）")
        turn = None
        try:
            async for t in client.prompt(PROMPT):
                turn = t
        except Exception as exc:  # noqa: BLE001
            print(f"{BAD} prompt 失败：{type(exc).__name__}: {exc}")
            for line in client.stderr_lines[-12:]:
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

        # ---- 第 4 跳：工具卡片（GUI reducer 看到的）----
        section("第 3 跳：工具卡片状态（GUI reducer）")
        for t in turn.tools:
            print(
                f"{OK} {t.title:<10} kind={t.kind:<7} status={t.status:<9} "
                f"output={t.output.splitlines()[0][:44] if t.output else ''!r}"
            )

        by_title = {t.title: t for t in turn.tools}
        expected = {"read_file": "read", "glob": "search", "write_file": "edit"}
        ok = True
        for title, kind in expected.items():
            t = by_title.get(title)
            if t is None:
                print(f"{BAD} 没有 {title} 的工具卡片")
                ok = False
                continue
            if t.kind != kind:
                print(f"{BAD} {title} 的 kind 应为 {kind!r}，实为 {t.kind!r}")
                ok = False
            if title == "write_file" and not allow:
                # 拒绝路径下这张卡片本来就该是 cancelled，在下面单独断言
                continue
            if t.status != "completed":
                print(f"{BAD} {title} 状态应为 completed，实为 {t.status!r}")
                ok = False

        # ---- 第 5 跳：审批与落盘 ----
        section("第 4 跳：审批闸门与真实副作用")
        if len(seen) != 1:
            print(f"{BAD} 应恰好收到 1 个审批请求（只有 write_file 需要），实为 {len(seen)}")
            ok = False
        elif seen[0].title != "write_file":
            print(f"{BAD} 审批对象应为 write_file，实为 {seen[0].title!r}")
            ok = False
        else:
            print(f"{OK} 只对 write_file 弹了审批（读/搜没有打扰用户）")

        written = (work / TARGET)
        if allow:
            if not written.is_file():
                print(f"{BAD} 允许之后 {TARGET} 应该存在 —— 工具没真的执行")
                ok = False
            elif written.read_text(encoding="utf-8") != "agentd 写的\n":
                print(f"{BAD} {TARGET} 内容不对：{written.read_text(encoding='utf-8')!r}")
                ok = False
            else:
                print(f"{OK} {TARGET} 已按预期写入：{written.read_text(encoding='utf-8')!r}")
            wf = by_title.get("write_file")
            if wf is not None and wf.status != "completed":
                print(f"{BAD} 允许后 write_file 状态应为 completed，实为 {wf.status!r}")
                ok = False
        else:
            if written.exists():
                print(f"{BAD} 拒绝之后 {TARGET} 绝不该存在 —— 审批被绕过了")
                ok = False
            else:
                print(f"{OK} {TARGET} 没有被创建（拒绝生效）")
            wf = by_title.get("write_file")
            if wf is None:
                print(f"{BAD} 缺少 write_file 卡片")
                ok = False
            else:
                # ACP 的 ToolCallStatus 里没有 cancelled，内核的 cancelled 会被折成
                # failed；客户端靠输出文案（DENY_MARK）还原成 cancelled。
                # 这里断言的就是这条还原链路 —— 它断了界面就会把"你点的拒绝"
                # 显示成红色"失败"。
                if wf.status != "cancelled":
                    print(f"{BAD} 拒绝后 write_file 状态应为 cancelled，实为 {wf.status!r}")
                    ok = False
                else:
                    print(f"{OK} 拒绝被还原成 cancelled（而不是吓人的 failed）")
                if "拒绝" not in wf.output:
                    print(f"{BAD} 拒绝的输出应回灌给模型，实为 {wf.output!r}")
                    ok = False

        section("第 5 跳：最终正文")
        print(f"{OK} 正文：{turn.text!r}")
        if not turn.text:
            print(f"{BAD} 正文为空")
            ok = False

        if ok:
            mode = "允许" if allow else "拒绝"
            print(f"\n全通：界面审批（{mode}）→ ACP → agentd 原生工具 → 副作用 → 界面 reducer")
        else:
            print("\n失败，agentd 日志末尾：")
            for line in logs[-15:]:
                print(f"       | {line}")
        return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="原生工具 + 审批端到端验证（不用真模型）")
    parser.add_argument(
        "--agentd-repo",
        default=os.getenv("AGENTD_REPO", r"D:\workspace\Agentd"),
        help="agentd 仓库路径（装进同一个 venv 时其实用不到，留着只为打印）",
    )
    parser.add_argument(
        "--deny",
        action="store_true",
        help="走拒绝路径：验证被拒的工具绝不产生副作用",
    )
    args = parser.parse_args()
    return asyncio.run(run(Path(args.agentd_repo), allow=not args.deny))


if __name__ == "__main__":
    raise SystemExit(main())
