"""前端语法高亮纯函数的断言测试。

index.html 里 /* HIGHLIGHT-BEGIN … HIGHLIGHT-END */ 标记段是**不碰 DOM 的
纯字符串函数**（forgeHighlight：escape 后文本 in → 带 token span 的 html out）。
测试用 node 把标记段原样 eval 后跑断言：

    pytest tests/test_frontend_highlight.py

node 不在本机时整组 skip（渲染层还有 node --check 兜底语法）。
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "forgeagent" / "gui" / "assets" / "index.html"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="需要 node")


def _extract_marker() -> str:
    src = SRC.read_text(encoding="utf-8")
    # 结构：/* HIGHLIGHT-BEGIN（可带文案）*/ <JS 主体> /* HIGHLIGHT-END */
    m = re.search(r"/\* ?HIGHLIGHT-BEGIN.*?\*/(.*?)/\* ?HIGHLIGHT-END", src, re.S)
    assert m, "index.html 里的 HIGHLIGHT 标记段丢了"
    return m.group(1)


def _run_node(snippet: str) -> str:
    node = shutil.which("node")
    # 用 .cjs 文件而不是 -e：新版 node 会对 -e 文本做 TypeScript 解析，
    # 把断言里的 `<span class=...>` 当泛型/TSX 报语法错。
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".cjs", delete=False, encoding="utf-8") as f:
        f.write(snippet)
        path = f.name
    try:
        proc = subprocess.run([node, path], capture_output=True, text=True, timeout=30)
    finally:
        Path(path).unlink(missing_ok=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return proc.stdout


def _with_marker(js: str) -> str:
    return _extract_marker() + "\n;" + js


def test_highlight_wraps_keywords_strings_numbers():
    out = _run_node(
        _with_marker(
            r"""
            const assert = require("assert");
            const html = forgeHighlight('def save_note(text): return "hi"', "python");
            assert(/<span class="token-kw">def<\/span>/.test(html), "关键字 def 未命中: " + html);
            assert(html.includes('<span class="token-kw">return</span>'));
            assert(/<span class="token-str">"hi"<\/span>/.test(html), "字符串未命中");
            assert(html.includes("save_note"), "普通标识符必须原样保留");
            console.log("ok1");
            """
        )
    )
    assert "ok1" in out


def test_highlight_escapes_nothing_extra_keeps_entities():
    # 输入是 escapeHtml 之后的文本：实体应原样保留、不得二次转义
    out = _run_node(
        _with_marker(
            r"""
            const assert = require("assert");
            const html = forgeHighlight("if (a &lt; 2 &amp;&amp; b) { return 42; }", "js");
            assert(html.includes("&lt;"), "实体被二次转义: " + html);
            assert(html.includes("42"));
            assert(!html.includes("&amp;lt;"), "出现双重转义");
            assert(/<span class="token-kw">if<\/span>/.test(html));
            assert(/<span class="token-num">42<\/span>/.test(html));
            console.log("ok2");
            """
        )
    )
    assert "ok2" in out


def test_highlight_comments_per_language():
    out = _run_node(
        _with_marker(
            r"""
            const assert = require("assert");
            const py = forgeHighlight("# 注释行\nx = 1", "python");
            assert(/<span class="token-com"># 注释行<\/span>/.test(py), "python 行注释未命中: " + py);
            const js = forgeHighlight("// 注释\nlet x = 1;", "js");
            assert(/<span class="token-com">\/\/ 注释<\/span>/.test(js), "js 行注释未命中");
            const blk = forgeHighlight("/* block */ y = 2;", "js");
            assert(/<span class="token-com">\/\* block \*\/<\/span>/.test(blk));
            console.log("ok3");
            """
        )
    )
    assert "ok3" in out


def test_highlight_unknown_language_falls_back_and_is_safe():
    out = _run_node(
        _with_marker(
            r"""
            const assert = require("assert");
            const html = forgeHighlight("const x = 1;", "raku");   // 未收录语言 → js 表
            assert(/<span class="token-kw">const<\/span>/.test(html));
            assert(!/<script/i.test(html), "高亮输出不允许出现 script 标签");
            console.log("ok4");
            """
        )
    )
    assert "ok4" in out
