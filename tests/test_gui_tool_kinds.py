"""界面上的工具 kind / status 文案：别让卡片显示英文裸词。

为什么值得一个测试：
    index.html 里是 `TOOL_KIND_LABEL[ev.kind] || ev.kind` —— 表里缺哪个 kind，
    卡片上就安静地显示英文裸词（"fetch" / "search"），不报错、不崩、没人发现。
    2026-09-14 加原生联网工具（web_search=search / web_fetch=fetch）时正踩在这个
    风险点上：agentd 那边加个新 kind，界面这边不会有任何提示。

    本仓库不 import agentd —— kind 的**全集**取自 ACP 协议本身，两个仓库各自
    钉自己那一半：agentd 钉"我发的 kind 合法"（tests/test_native_tools.py 里的
    _ACP_KIND），界面钉"合法的 kind 我都有中文标签"。两端合起来才算闭环。
"""

from __future__ import annotations

import re
from pathlib import Path

_INDEX = Path(__file__).resolve().parent.parent / "forgeagent" / "gui" / "assets" / "index.html"

# ACP 的 ToolKind 全集（协议里就这么几个）。agentd 的 tools.py 用同一份枚举。
ACP_TOOL_KINDS = (
    "read",
    "edit",
    "delete",
    "move",
    "search",
    "execute",
    "think",
    "fetch",
    "switch_mode",
    "other",
)

# acp_client 可能给出的状态：内核报上来的四种，外加客户端把 DENY_MARK
# 还原出来的 cancelled（见 acp_client.py 里 failed → cancelled 那段）。
CLIENT_TOOL_STATUSES = ("pending", "in_progress", "completed", "failed", "cancelled")


def _index_text() -> str:
    assert _INDEX.is_file(), f"找不到界面资源：{_INDEX}"
    return _INDEX.read_text(encoding="utf-8")


def _js_object(name: str) -> dict[str, str]:
    """把 index.html 里 `var NAME = { k: "v", ... };` 抠成 dict。

    刻意用正则而不是引 JS 引擎：这个文件是给浏览器跑的，测试只想知道表里有哪些键，
    为此拉一个 JS 运行时进来不值。
    """
    m = re.search(rf"var {name} = \{{(.*?)\}};", _index_text(), re.S)
    assert m, f"index.html 里找不到 var {name} —— 改名了？"
    pairs = re.findall(r'(\w+)\s*:\s*"([^"]*)"', m.group(1))
    assert pairs, f"{name} 里一个键都没解析出来 —— 换写法了？"
    return dict(pairs)


def test_every_acp_tool_kind_has_a_label():
    labels = _js_object("TOOL_KIND_LABEL")
    missing = [k for k in ACP_TOOL_KINDS if k not in labels]
    assert not missing, (
        f"这些 ACP kind 没有中文标签，卡片上会显示英文裸词：{missing}"
    )


def test_tool_kind_labels_are_not_blank():
    labels = _js_object("TOOL_KIND_LABEL")
    blank = [k for k, v in labels.items() if not v.strip()]
    assert not blank, f"这些 kind 的标签是空的（等于没写）：{blank}"


def test_web_tool_kinds_have_the_expected_labels():
    """联网工具用的就是 search / fetch 两个 kind，别再落回英文裸词。"""
    labels = _js_object("TOOL_KIND_LABEL")
    assert labels.get("search") == "搜索"
    assert labels.get("fetch") == "获取"


def test_tool_status_labels_cover_what_the_client_can_emit():
    labels = _js_object("TOOL_STATUS_LABEL")
    missing = [s for s in CLIENT_TOOL_STATUSES if s not in labels]
    assert not missing, f"这些状态没有文案，卡片上会显示英文：{missing}"
