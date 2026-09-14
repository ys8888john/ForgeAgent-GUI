"""验证 mcp.json 里配的本地 MCP server 真的能用。

**刻意复用 `agentd.kernel.mcp.McpHub`**，而不是自己写一份精简的 MCP 客户端 ——
验证要验的就是 agentd 实际走的那条路（连接方式、env 合并规则、工具名前缀规则），
自己另写一份很容易"验过了但跑起来还是不通"。

    python scripts/verify_local_mcp.py            # 连 + 列工具 + 调探针
    python scripts/verify_local_mcp.py --list     # 只连 + 列工具，不调探针
    python scripts/verify_local_mcp.py --strict   # 探针返回错误也算失败

退出码的约定（分清楚两件不同的事）：

- **server 连不上 / 列不出工具** → 退出码 1。这是配置或安装坏了，必须修。
- **探针调用返回错误** → 默认只报警告（退出码 0），加 `--strict` 才当失败。
  理由：探针是"顺手打个真实调用"的冒烟测试，它的错误可能根本不是 server 的锅
  —— 实测 `fetch` 探针会因为**出网被拦**而失败（"Failed to fetch robots.txt
  ... connection issue"），但 server 本身连通、工具列表正常。
  把这两件事混成一个"失败"，只会让人以为是配置写错了。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from forgeagent.gui.mcp_config import config_path, load_mcp_servers  # noqa: E402
from forgeagent.gui.mcp_presets import CWD_PLACEHOLDER, PRESETS  # noqa: E402

MAX_OUTPUT = 300


def _clip(text: str, limit: int = MAX_OUTPUT) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[:limit] + f" …(+{len(flat) - limit} 字)"


def _probe_args(preset, cwd: str) -> dict:
    out = {}
    for key, value in preset.probe_args.items():
        out[key] = cwd if value == CWD_PLACEHOLDER else value
    return out


async def _run(servers: list[dict], *, do_probe: bool) -> tuple[int, list[str]]:
    """返回 (连不上的 server 数, 探针返回错误的 server 名)。"""
    try:
        from agentd.kernel.mcp import McpHub
    except ImportError as exc:
        print(f"[跳过] 需要 agentd 可导入（pip install -e ../Agentd）：{exc}")
        raise SystemExit(3)

    broken = 0
    probe_issues: list[str] = []
    async with McpHub(servers, cwd=str(Path.cwd())) as hub:
        # 工具名规则是 {server}__{tool}（见 agentd/kernel/mcp.py）
        by_server: dict[str, list[str]] = {}
        for item in hub.tool_schema():
            full = item["function"]["name"]
            server, _, tool = full.partition("__")
            by_server.setdefault(server, []).append(tool)

        for cfg in servers:
            name = str(cfg.get("name"))
            tools = sorted(by_server.get(name) or [])
            if not tools:
                print(f"[{name}] 失败：没列出任何工具")
                broken += 1
                continue
            print(f"[{name}] OK   工具 {len(tools)} 个：{', '.join(tools)}")

            preset = PRESETS.get(name)
            if not do_probe or preset is None or preset.probe_tool is None:
                continue
            if preset.probe_tool not in tools:
                print(f"       探针：预设里的 {preset.probe_tool} 不在工具列表里，跳过")
                continue
            args = _probe_args(preset, str(Path.cwd()))
            full = f"{name}__{preset.probe_tool}"
            result = await hub.call(full, json.dumps(args))
            failed = str(result).startswith("[错误]")
            mark = "⚠️ " if failed else ""
            print(f"       {mark}探针 {full}({json.dumps(args, ensure_ascii=False)})")
            print(f"            → {_clip(result)}")
            if failed:
                probe_issues.append(name)

        for err in hub.errors:
            print(f"[错误] {err}")
            broken += 1

    return broken, probe_issues


def main() -> int:
    parser = argparse.ArgumentParser(description="验证本地 MCP server 连通性")
    parser.add_argument("--list", action="store_true", help="只连 + 列工具，不调探针")
    parser.add_argument("--strict", action="store_true", help="探针返回错误也算失败")
    parser.add_argument("--path", default=None, help="覆盖 mcp.json 位置")
    args = parser.parse_args()

    path = Path(args.path) if args.path else config_path()
    servers = load_mcp_servers(path)
    print(f"配置文件：{path}（{'存在' if path.is_file() else '不存在'}）")
    if not servers:
        print("没有解析出任何 server。先跑：python scripts/install_local_mcp.py --install --write")
        return 2
    print(f"发现 {len(servers)} 个 server：{', '.join(str(s.get('name')) for s in servers)}")
    print()

    broken, probe_issues = asyncio.run(_run(servers, do_probe=not args.list))
    print()

    if broken:
        print(f"{broken} 个 server 没连上或没列出工具 —— 这是配置/安装问题，看上面输出。")
        return 1
    if probe_issues and args.strict:
        print(f"{len(probe_issues)} 个探针返回错误（--strict）：{', '.join(probe_issues)}")
        return 1
    if probe_issues:
        print(f"server 全部连通。有 {len(probe_issues)} 个探针返回了错误"
              f"（{', '.join(probe_issues)}）—— 多半是出网/环境问题，不是 server 坏了；"
              "要把它当失败就加 --strict。")
        return 0
    print("全部通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
