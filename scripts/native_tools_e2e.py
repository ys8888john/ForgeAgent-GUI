"""原生工具端到端验证：GUI 侧审批 → ACP → agentd 原生工具 → 结果 / 拒绝 → 界面 reducer。

为什么不用真模型：
    本机 Ollama 不一定在跑，就算在跑，小模型也不一定会按我们要的顺序吐 tool_calls。
    "原生工具通不通、审批拦不拦得住"不该赌在模型的工具调用能力上。
    所以给 agentd 塞 AGENTD_LLM_BACKEND=script + 一段脚本，
    链路上真实的代码路径一个都不跳过：

        agentd.kernel.tools.NativeToolbox        （真读写文件、真跑命令、真发 HTTP）
        agentd.kernel.modes.agent.AgentMode      （kind 映射 + 审批闸门）
        agentd.kernel.tools.needs_approval       （哪些工具要审批）
        agentd.transports.acp_stdio              （request_permission 的 SDK 帧）
        forgeagent.acp_client                    （解析 + 回帧 + reducer）
        forgeagent.gui.bridge                    （permission 事件）

    这一步是唯一能证明"两个仓库对审批帧的理解真的一致"的东西 ——
    单测两边各自打自己造的帧，字段名对不上也测不出来。

两个场景：
    files   读 + 搜（都免审批）→ 写（弹审批）→ 允许则真落盘 / 拒绝则绝不能落盘
    web     web_search → web_fetch（都免审批）

    ⚠️ web 场景**不出网**：脚本在自己进程里起一个假的搜索/网页后端（真 socket、
    真 HTML 响应），再用 AGENTD_SEARCH_ENDPOINT 把 agentd 的搜索后端指过去。
    这样才能"离线可复现"地验跨仓库链路。"Bing 的页面结构现在还认得出来吗"
    是另一个问题，由 agentd 自己的 tests/test_web_tools_live.py 负责（那个要出网）。

用法：
    python scripts/native_tools_e2e.py                    # files 场景，允许写入
    python scripts/native_tools_e2e.py --deny             # files 场景，拒绝写入
    python scripts/native_tools_e2e.py --scenario web     # web 场景（离线可跑）

退出码：0 全通，1 有失败。
"""

from __future__ import annotations

import argparse
import asyncio
import http.server
import json
import os
import sys
import tempfile
import threading
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from forgeagent.acp_client import AcpClient  # noqa: E402

OK, BAD = "  [OK]", "  [FAIL]"
FILES_PROMPT = "读一下文件并写点东西"
WEB_PROMPT = "搜一下再打开一个页面"
TARGET = "notes.txt"


def section(title: str) -> None:
    print(f"\n{title}")
    print("-" * len(title))


# --------------------------------------------------------------------------
# 假的搜索 / 网页后端（只在 web 场景用）
#
# 刻意起真 HTTP 服务而不是 mock httpx：web_search / web_fetch 里的 AsyncClient
# 会走完整的 socket + header + 流式读路径，mock 掉就把要验的那段跳过了。
# 返回的 HTML 抄的是真 Bing 的结构（<li class="b_algo"> / 块内首个 h2>a / 首个 p），
# 所以解析器的选择器也能被这一段覆盖到。
# --------------------------------------------------------------------------

_FAKE_BING_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>假搜索结果</title></head>
<body>
<span class="sb_count">约 42 条结果</span>
<li class="b_algo"><h2><a href="https://example.com/first">第一条标题（假）</a></h2>
  <p>第一条摘要，来自本地假后端。</p></li>
<li class="b_algo"><h2><a href="https://example.com/second">第二条标题（假）</a></h2>
  <p>第二条摘要，不该被返回 —— 脚本只要了 2 条里的前 2 条。</p></li>
<li class="b_algo"><h2><a href="https://example.com/third">第三条标题（假）</a></h2>
  <p>第三条摘要，必须被 count 截掉。</p></li>
</body></html>"""

_FAKE_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>示例页</title>
<style>body{color:red}</style>
<script>var 脚本里的字 = "不该出现在正文里";</script>
</head>
<body><h1>示例标题</h1>
<p>段落一 &lt;实体&gt; 已还原。</p>
<p>段落二。</p></body></html>"""


class _FakeWebHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 —— BaseHTTPRequestHandler 规定的名字
        path = urllib.parse.urlparse(self.path).path
        if path == "/search":
            body, ctype = _FAKE_BING_PAGE, "text/html; charset=utf-8"
        elif path == "/page":
            body, ctype = _FAKE_PAGE, "text/html; charset=utf-8"
        else:
            self.send_error(404, "fake backend: 只有 /search 和 /page")
            return
        raw = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args: object) -> None:
        """默认实现往 stderr 打访问日志，会淹没 e2e 输出；静音。"""


class FakeWeb:
    """本地假后端，绑 127.0.0.1 的随机空闲端口。"""

    def __init__(self) -> None:
        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FakeWebHandler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    def __enter__(self) -> "FakeWeb":
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()


# --------------------------------------------------------------------------
# 两个场景的回放脚本
# --------------------------------------------------------------------------


def build_files_script() -> str:
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


def build_web_script(page_url: str) -> str:
    """联网场景：搜一次（count=2，验上限生效）→ 抓一个本地页面。"""
    return json.dumps(
        [
            {
                "tool_calls": [
                    {"name": "web_search", "arguments": {"query": "原生工具", "count": 2}}
                ]
            },
            {"tool_calls": [{"name": "web_fetch", "arguments": {"url": page_url}}]},
            {"text": "联网也通了"},
        ],
        ensure_ascii=False,
    )


# --------------------------------------------------------------------------
# 公共：拉起 agentd + 跑一轮
# --------------------------------------------------------------------------


async def _run_turn(work: Path, script: str, extra_env: dict, prompt: str, allow: bool = False):
    """拉起 agentd、跑一轮、收事件，然后把子进程关掉。

    返回 (turn, logs, seen)：turn 为 None 表示链路在握手/启动阶段就断了。
    seen 是收到的审批请求列表（web 场景必须为空 —— 联网工具是只读的）。
    """
    child_env = dict(os.environ)
    child_env["AGENTD_LLM_BACKEND"] = "script"
    child_env["AGENTD_SCRIPT_JSON"] = script
    child_env["AGENTD_STORE"] = "memory"      # 别往用户真实的会话库里写测试数据
    child_env["AGENTD_DOTENV"] = "__nonexistent__"
    child_env["AGENTD_TOOLS"] = "native"      # 显式打开原生工具（联网工具在这个档里）
    child_env["AGENTD_TOOLS_APPROVE"] = "native"
    child_env.update(extra_env)

    # 工具的相对路径是相对**会话 cwd**解析的，所以 cwd 必须指到临时工作区，
    # 而不是 agentd 仓库 —— 否则测试会去动仓库里的真文件。
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
        return None, None, []

    print(f"{OK} 握手成功，会话 {client.session_id}")

    # 审批回调：模拟界面点按钮。allow=False 时一律点拒绝。
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

    turn = None
    try:
        async for t in client.prompt(prompt):
            turn = t
    except Exception as exc:  # noqa: BLE001
        print(f"{BAD} prompt 失败：{type(exc).__name__}: {exc}")
        for line in client.stderr_lines[-12:]:
            print(f"       | {line}")
        await client.close()
        return None, None, seen

    logs = list(client.stderr_lines)   # 关掉之前先把日志抓下来
    await client.close()
    return turn, logs, seen


def _print_cards(tools) -> None:
    for t in tools:
        first = t.output.splitlines()[0][:44] if t.output else ""
        print(
            f"{OK} {t.title:<10} kind={t.kind:<7} status={t.status:<9} output={first!r}"
        )


# --------------------------------------------------------------------------
# 场景一：文件读写 + 审批
# --------------------------------------------------------------------------


async def run_files(agentd_repo: Path, allow: bool) -> int:
    with tempfile.TemporaryDirectory(prefix="forgeagent-native-e2e-") as td:
        work = Path(td)
        (work / "seed.txt").write_text("种子内容\n", encoding="utf-8")

        section("第 1 跳：拉起 agentd（cwd = 临时工作区）")
        turn, logs, seen = await _run_turn(
            work, build_files_script(), {}, FILES_PROMPT, allow=allow
        )
        if turn is None:
            return 1

        if turn.error:
            print(f"{BAD} agentd 报错：{turn.error}")
            return 1

        section("第 2 跳：工具卡片状态（GUI reducer）")
        _print_cards(turn.tools)

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

        section("第 3 跳：审批闸门与真实副作用")
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

        section("第 4 跳：最终正文")
        print(f"{OK} 正文：{turn.text!r}")
        if not turn.text:
            print(f"{BAD} 正文为空")
            ok = False

        if ok:
            mode = "允许" if allow else "拒绝"
            print(f"\n全通：界面审批（{mode}）→ ACP → agentd 原生工具 → 副作用 → 界面 reducer")
        else:
            print("\n失败，agentd 日志末尾：")
            for line in (logs or [])[-15:]:
                print(f"       | {line}")
        return 0 if ok else 1


# --------------------------------------------------------------------------
# 场景二：联网工具（离线：本地假后端）
# --------------------------------------------------------------------------


async def run_web(agentd_repo: Path) -> int:
    with FakeWeb() as fake, tempfile.TemporaryDirectory(
        prefix="forgeagent-web-e2e-"
    ) as td:
        work = Path(td)
        page_url = f"http://127.0.0.1:{fake.port}/page"
        extra_env = {
            # 把搜索后端指到本地假 Bing —— 全链路离线、可复现
            "AGENTD_SEARCH_ENDPOINT": f"http://127.0.0.1:{fake.port}/search",
            # agentd 子进程会继承 HTTP_PROXY（本机会把所有出网导给本地代理），
            # 不排除环回的话，连 http://127.0.0.1 的假后端也会被代理掉。
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
        }

        section(f"第 1 跳：拉起 agentd（假后端 127.0.0.1:{fake.port}）")
        turn, logs, seen = await _run_turn(
            work, build_web_script(page_url), extra_env, WEB_PROMPT
        )
        if turn is None:
            return 1

        if turn.error:
            print(f"{BAD} agentd 报错：{turn.error}")
            return 1

        section("第 2 跳：工具卡片状态（GUI reducer）")
        _print_cards(turn.tools)

        by_title = {t.title: t for t in turn.tools}
        ok = True

        # kind 必须是 ACP 合法值：客户端不认就原样显示，卡片会变成 "search"/"fetch"
        # 这种英文裸词而不是「搜索」/「获取」。
        for title, kind in (("web_search", "search"), ("web_fetch", "fetch")):
            t = by_title.get(title)
            if t is None:
                print(f"{BAD} 没有 {title} 的工具卡片")
                ok = False
                continue
            if t.kind != kind:
                print(f"{BAD} {title} 的 kind 应为 {kind!r}，实为 {t.kind!r}")
                ok = False
            if t.status != "completed":
                print(f"{BAD} {title} 状态应为 completed，实为 {t.status!r}（输出：{t.output[:120]!r}）")
                ok = False

        section("第 3 跳：搜索解析与 count 上限")
        ws = by_title.get("web_search")
        if ws is None:
            ok = False
        else:
            # count=2 → 假页面有 3 条，必须只回 2 条
            if "搜索到 2 条" not in ws.output:
                print(f"{BAD} 应回 2 条结果（count 没生效？），实为：{ws.output[:200]!r}")
                ok = False
            else:
                print(f"{OK} count=2 生效，假页面 3 条里只取了 2 条")
            for want in ("第一条标题（假）", "https://example.com/first", "第一条摘要"):
                if want not in ws.output:
                    print(f"{BAD} 搜索结果里缺少 {want!r}")
                    ok = False
            if "第三条标题（假）" in ws.output:
                print(f"{BAD} 第三条本应被 count 截掉，却出现在结果里")
                ok = False
            if "约 42 条结果" in ws.output:
                print(f"{OK} 结果条数提示（sb_count）也被带出来了")

        section("第 4 跳：网页抓取（HTML → 纯文本）")
        wf = by_title.get("web_fetch")
        if wf is None:
            ok = False
        else:
            for want in ("示例标题", "段落一 <实体> 已还原。", "段落二。"):
                if want not in wf.output:
                    print(f"{BAD} 抓取正文里缺少 {want!r}（实为：{wf.output[:200]!r}）")
                    ok = False
            for ban in ("color:red", "不该出现在正文里", "<p>", "<h1>"):
                if ban in wf.output:
                    print(f"{BAD} 抓取正文里不该出现 {ban!r}（标签/脚本没剥干净）")
                    ok = False
            if all(w in wf.output for w in ("示例标题", "段落二。")):
                print(f"{OK} 正文已折成纯文本：标签与脚本/样式都剥掉了，实体还原了")

        section("第 5 跳：联网工具必须免审批")
        if seen:
            print(
                f"{BAD} 联网工具是只读的，不该弹审批；实际弹了 {len(seen)} 个："
                f"{[r.title for r in seen]}"
            )
            ok = False
        else:
            print(f"{OK} 搜索/抓取全程 0 次审批（只读动作不打扰用户）")

        section("第 6 跳：最终正文")
        print(f"{OK} 正文：{turn.text!r}")
        if not turn.text:
            print(f"{BAD} 正文为空")
            ok = False

        if ok:
            print("\n全通：脚本回放 → ACP → agentd web_search/web_fetch → 本地假后端 → 界面 reducer")
        else:
            print("\n失败，agentd 日志末尾：")
            for line in (logs or [])[-15:]:
                print(f"       | {line}")
        return 0 if ok else 1


# --------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="原生工具端到端验证（不用真模型）")
    parser.add_argument(
        "--agentd-repo",
        default=os.getenv("AGENTD_REPO", r"D:\workspace\Agentd"),
        help="agentd 仓库路径（装进同一个 venv 时其实用不到，留着只为打印）",
    )
    parser.add_argument(
        "--scenario",
        choices=("files", "web"),
        default="files",
        help="files=读写+审批（默认）；web=联网搜索/抓取（离线跑本地假后端）",
    )
    parser.add_argument(
        "--deny",
        action="store_true",
        help="files 场景专用：走拒绝路径，验证被拒的工具绝不产生副作用",
    )
    args = parser.parse_args()
    if args.deny and args.scenario != "files":
        parser.error("--deny 只对 --scenario files 有意义")

    repo = Path(args.agentd_repo)
    if args.scenario == "web":
        return asyncio.run(run_web(repo))
    return asyncio.run(run_files(repo, allow=not args.deny))


if __name__ == "__main__":
    raise SystemExit(main())
