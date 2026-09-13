"""端到端验证：前端 → 后端 → Ollama，三跳全通才算通。

为什么要有这个脚本：
    这三条链路任一小节出问题，症状都长得差不多——"没反应"或者"一片空白"。
    靠肉眼看 TUI 根本分不清是 Ollama 没起来、模型名不对、还是 ACP 方法名拼错了。
    所以这里一跳一跳验，坏在哪跳直接指出来。

用法：
    python scripts/e2e.py                # 全部走真实链路
    python scripts/e2e.py --model qwen3  # 指定模型（用来复现 404 场景）
    python scripts/e2e.py --skip-ui      # 只验后端两跳

退出码：0 全通，1 有跳失败。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

# 让 `python scripts/e2e.py` 能直接 import 到仓库里的 forgeagent
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from forgeagent.acp_client import AcpClient  # noqa: E402
from forgeagent.app import ForgeAgentApp  # noqa: E402

OK, BAD, SKIP = "  ✅", "  ❌", "  ⏭️ "
QUESTION = "只回复十个字以内：链路打通了吗？"


def section(title: str) -> None:
    print(f"\n{title}")
    print("-" * len(title))


# ---------------------------------------------------------------- 第 1 跳
async def hop_ollama(host: str) -> tuple[bool, list[str]]:
    """Ollama 活着吗？上面有哪些模型？"""
    section("第 1 跳：Ollama")
    url = f"{host}/api/tags"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
    except httpx.ConnectError as exc:
        print(f"{BAD} 连不上 {url}")
        print(f"       {exc}")
        print("       处理：确认 ollama serve 在跑。")
        print("       在 WSL2 里跑、从 Windows 访问的话，")
        print("       需要 %USERPROFILE%\\.wslconfig 里开 networkingMode=mirrored")
        return False, []
    except httpx.HTTPError as exc:
        print(f"{BAD} /api/tags 返回错误：{exc}")
        return False, []

    names = [
        (m.get("name") or m.get("model") or "")
        for m in (data.get("models") or [])
    ]
    names = [n for n in names if n]
    print(f"{OK} {url} 可达，共 {len(names)} 个模型")
    for n in names[:8]:
        print(f"       · {n}")
    if not names:
        print(f"{BAD} 一个模型都没有。先 ollama pull qwen3.5:9b")
        return False, []
    return True, names


# ---------------------------------------------------------------- 第 2 跳
async def hop_acp(cwd: str) -> tuple[bool, str]:
    """agentd 能走 ACP 说话吗？能拿到真实回复吗？"""
    section("第 2 跳：agentd（ACP over stdio）")
    client = AcpClient(cwd=cwd)
    try:
        await client.start()
    except Exception as exc:
        print(f"{BAD} 拉起 agentd 失败：{type(exc).__name__}: {exc}")
        if client.stderr_lines:
            print("       agent 日志末尾：")
            for line in client.stderr_lines[-10:]:
                print(f"       | {line}")
        print("       处理：确认 agentd 装在当前解释器里（pip install -e /path/to/Agentd）")
        return False, ""

    print(f"{OK} 握手成功，会话 {client.session_id}")

    turn = None
    try:
        async for t in client.prompt(QUESTION):
            turn = t
    except Exception as exc:
        print(f"{BAD} prompt 失败：{type(exc).__name__}: {exc}")
        return False, ""

    await client.close()

    if turn is None:
        print(f"{BAD} 一个事件都没收到")
        return False, ""
    if turn.error:
        print(f"{BAD} agentd 报错：{turn.error}")
        return False, ""
    if not turn.text.strip():
        print(f"{BAD} 回复是空的（stop={turn.stop_reason}）—— 典型的模型名不对")
        return False, ""

    print(f"{OK} 收到回复（stop={turn.stop_reason}）")
    print(f"       {turn.text.strip()[:200]}")
    return True, turn.text.strip()


# ---------------------------------------------------------------- 第 3 跳
async def hop_ui(cwd: str, timeout: float = 120.0) -> bool:
    """Textual 界面能把这一问一答真的显示出来吗？"""
    section("第 3 跳：Textual 界面")
    from textual.widgets import Input, Static

    app = ForgeAgentApp(cwd=cwd)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.pause()

        if not app._client.session_id:
            print(f"{BAD} 界面里 agent 没连上：{app._status}")
            for role, content in app._history:
                print(f"       [{role}] {content}")
            return False
        print(f"{OK} 界面已连上（{app._status}）")

        box = app.query_one(Input)
        box.value = QUESTION
        box.focus()
        await pilot.press("enter")

        waited = 0.0
        while waited < timeout and app._turn is not None:
            await pilot.pause(0.5)
            waited += 0.5
        # 提交后 _turn 会先被置 None 再被 worker 赋值，多等两拍确保 worker 已启动
        for _ in range(4):
            await pilot.pause(0.3)
        while waited < timeout and app._turn is not None:
            await pilot.pause(0.5)
            waited += 0.5

        shown = app.query_one("#log", Static).render()
        plain = shown.plain if hasattr(shown, "plain") else str(shown)

        if app._turn is not None:
            print(f"{BAD} 等了 {timeout}s 还没生成完")
            return False
        if QUESTION not in plain:
            print(f"{BAD} 界面上找不到刚发出去的提问")
            return False
        if "✗" in plain:
            print(f"{BAD} 界面上出现了错误条目：")
            for role, content in app._history:
                if role == "error":
                    print(f"       {content}")
            return False

        print(f"{OK} 界面上能看到提问和回复")
        for line in plain.splitlines()[-4:]:
            print(f"       {line}")
        return True


# ---------------------------------------------------------------- main
async def amain(args: argparse.Namespace) -> int:
    host = args.ollama_host
    if args.model:
        os.environ["AGENTD_OLLAMA_MODEL"] = args.model

    # None 表示"没跑到"（前跳失败或主动跳过），跟"跑了但失败"区分开
    results: list[tuple[str, bool | None]] = []

    ok_ollama, _ = await hop_ollama(host)
    results.append(("Ollama", ok_ollama))

    ok_acp = False
    if ok_ollama:
        ok_acp, _ = await hop_acp(args.cwd)
        results.append(("agentd（ACP）", ok_acp))
    else:
        # 模型服务都没起来，后面两跳跑也没意义，标成跳过而不是失败
        results.append(("agentd（ACP）", None))

    if args.skip_ui:
        results.append(("Textual 界面", None))
    elif ok_acp:
        results.append(("Textual 界面", await hop_ui(args.cwd)))
    else:
        results.append(("Textual 界面", None))

    section("结果")
    for name, ok in results:
        if ok is None:
            print(f"{SKIP} {name}：未执行")
            continue
        print(f"{OK if ok else BAD} {name}")
    return 0 if all(ok for _, ok in results if ok is not None) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="前端 → 后端 → Ollama 三跳验证")
    parser.add_argument("--ollama-host", default=os.getenv("AGENTD_OLLAMA_HOST", "http://localhost:11434"))
    parser.add_argument("--cwd", default=os.getcwd(), help="agentd 的工作目录")
    parser.add_argument("--model", default=None, help="显式指定模型（默认 auto 探测）")
    parser.add_argument("--skip-ui", action="store_true", help="跳过第 3 跳（界面）")
    args = parser.parse_args()
    return asyncio.run(amain(args))


if __name__ == "__main__":
    raise SystemExit(main())
