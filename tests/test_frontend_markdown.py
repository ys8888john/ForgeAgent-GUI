"""前端 Markdown 渲染纯函数的断言测试。

index.html 里 /* MD-BEGIN … MD-END */ 标记段是不碰 DOM 的纯字符串函数
（escapeHtml / renderMd / mdImageTag / groupLists / renderTables）；renderMd
的代码块分支还要 HIGHLIGHT 段的 forgeHighlight，两段拼接后一起 eval。

钉的行为（2026-10-13 用户实测"界面很乱"后修）：
  1. 有序列表（1. / 1、/ 1)）渲染成 <ol>，不再掉进段落变 <br> 纯文本；
  2. 一篇里多个列表各自成组，中间的段落不会被吞进上一个 <ul>；
  3. markdown 图片：http(s) 直链走 src，本地路径走 data-src 占位
     （由 hydrateMdImages 换真图），data:/javascript: 不当直链；
  4. escapeHtml 优先：正文里的 <script> 之类不许变成真标签。

node 不在 PATH 时整组 skip（渲染层还有 node --check 兜底语法）。
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


def _extract(begin: str, end: str) -> str:
    src = SRC.read_text(encoding="utf-8")
    m = re.search(rf"/\* ?{begin}.*?\*/(.*?)/\* ?{end}", src, re.S)
    assert m, f"index.html 里的 {begin} 标记段丢了"
    return m.group(1)


def _run_node(snippet: str) -> str:
    node = shutil.which("node")
    # 用 .cjs 文件而不是 -e：新版 node 会对 -e 文本做 TypeScript 解析
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


def _with_markers(js: str) -> str:
    return _extract("HIGHLIGHT-BEGIN", "HIGHLIGHT-END") + "\n;" + _extract("MD-BEGIN", "MD-END") + "\n;" + js


# ---- 有序列表（用户实测的重灾区：行程 D1/D2、计划 1/2/3）----


def test_ordered_list_renders_as_ol():
    out = _run_node(
        _with_markers(
            r"""
            const assert = require("assert");
            const html = renderMd("行程如下：\n\n1. 乌鲁木齐\n2. 喀纳斯\n3. 返程");
            assert(html.includes("<ol><li>乌鲁木齐</li><li>喀纳斯</li><li>返程</li></ol>"), html);
            assert(!/<p>1\./.test(html), "有序列表掉进段落流了: " + html);
            console.log("ok1");
            """
        )
    )
    assert "ok1" in out


def test_ordered_list_chinese_and_paren_markers():
    out = _run_node(
        _with_markers(
            r"""
            const assert = require("assert");
            assert(renderMd("1、甲\n2、乙").includes("<ol><li>甲</li><li>乙</li></ol>"));
            assert(renderMd("1) 甲\n2) 乙").includes("<ol><li>甲</li><li>乙</li></ol>"));
            console.log("ok2");
            """
        )
    )
    assert "ok2" in out


def test_separate_lists_do_not_merge_and_paragraph_survives():
    """回归：旧写法把一篇里多个列表缝成一个 <ul>，中间段落被吞进列表。"""
    out = _run_node(
        _with_markers(
            r"""
            const assert = require("assert");
            const html = renderMd("第一批：\n\n- a\n- b\n\n中间段落\n\n第二批：\n\n1. x\n2. y");
            assert((html.match(/<ul>/g) || []).length === 1, "ul 数量不对: " + html);
            assert((html.match(/<ol>/g) || []).length === 1, "ol 数量不对: " + html);
            assert(html.includes("<p>中间段落</p>"), "段落被吞进列表了: " + html);
            assert(html.includes("<li>a</li><li>b</li>"), html);
            assert(html.includes("<li>x</li><li>y</li>"), html);
            console.log("ok3");
            """
        )
    )
    assert "ok3" in out


def test_unordered_list_bullet_variants():
    out = _run_node(
        _with_markers(
            r"""
            const assert = require("assert");
            assert(renderMd("- 甲\n* 乙\n+ 丙\n• 丁").includes("<ul><li>甲</li><li>乙</li><li>丙</li><li>丁</li></ul>"));
            console.log("ok4");
            """
        )
    )
    assert "ok4" in out


# ---- 图片（图文并茂）----


def test_markdown_image_http_uses_src_directly():
    out = _run_node(
        _with_markers(
            r"""
            const assert = require("assert");
            const html = renderMd("看图：\n\n![喀纳斯湖](https://example.com/kanas.png?a=1&b=2)");
            assert(html.includes('<img class="md-img" src="https://example.com/kanas.png?a=1&amp;b=2" alt="喀纳斯湖">'), html);
            assert(!html.includes("data-src"), "http 直链不该走占位: " + html);
            console.log("ok5");
            """
        )
    )
    assert "ok5" in out


def test_markdown_image_local_path_uses_data_src_placeholder():
    """本地路径（generate_image 产物）不能直接 src —— 浏览器够不着本机路径，
    必须挂 data-src 由 hydrateMdImages 走 /api/artifact_preview 换字节。"""
    out = _run_node(
        _with_markers(
            r"""
            const assert = require("assert");
            const html = renderMd("![生成图](C:\\Users\\me\\.agentd\\tool_results\\imagegen\\a.png)");
            assert(html.includes('data-src="C:\\Users\\me\\.agentd\\tool_results\\imagegen\\a.png"'), html);
            assert(!/\ssrc="/.test(html), "本地路径被直接塞进 src 了: " + html);
            console.log("ok6");
            """
        )
    )
    assert "ok6" in out


def test_markdown_image_rejects_non_http_schemes():
    """data:/javascript: 之类不当直链 —— 按本地路径走服务端白名单。"""
    out = _run_node(
        _with_markers(
            r"""
            const assert = require("assert");
            const js = renderMd("![x](javascript:alert(1))");
            assert(js.includes("data-src="), js);
            // 注意别用 src="javascript: 裸串断言 —— data-src="javascript: 也含它，
            // 属性前面必须有空白才是真 src
            assert(!/\ssrc="javascript:/.test(js), js);
            console.log("ok7");
            """
        )
    )
    assert "ok7" in out


# ---- 回归：既有行为不能坏 ----


def test_inline_markdown_still_works_and_escapes():
    out = _run_node(
        _with_markers(
            r"""
            const assert = require("assert");
            const html = renderMd("**粗体** 和 `code` 和 <script>alert(1)</script>");
            assert(html.includes("<strong>粗体</strong>"), html);
            assert(html.includes("<code>code</code>"), html);
            assert(html.includes("&lt;script&gt;"), "HTML 没被转义: " + html);
            assert(!/<script/i.test(html), "出现真 script 标签");
            console.log("ok8");
            """
        )
    )
    assert "ok8" in out


def test_table_still_renders():
    out = _run_node(
        _with_markers(
            r"""
            const assert = require("assert");
            const html = renderMd("| 天 | 地点 |\n| --- | --- |\n| D1 | 乌鲁木齐 |");
            assert(html.includes('<table class="md-table">'), html);
            assert(html.includes("<th>天</th>") && html.includes("<td>D1</td>"), html);
            console.log("ok9");
            """
        )
    )
    assert "ok9" in out


def test_code_block_still_highlights():
    out = _run_node(
        _with_markers(
            """
            const assert = require("assert");
            const html = renderMd("```python\\ndef f():\\n    return 1\\n```");
            assert(html.includes('<div class="codeblock">'), html);
            assert(html.includes('<span class="token-kw">def</span>'), html);
            console.log("ok10");
            """
        )
    )
    assert "ok10" in out
