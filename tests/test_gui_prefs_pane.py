"""「个性化设置」面板的回归测试（2026-10-13）。

背景（用户截图 + 原话）：多媒体模型（默认图像识别 / 图像生成）以前内嵌在「模型配置」
弹窗里，是两个自由文本输入框 —— 模型名要靠手打，打错不报错、静默失败；而且它放在
模型管理里容易被当成"又一项模型参数"。

改动：
1. 从「模型配置」里移出，在「设置（⚙）」菜单新开「个性化设置」面板；
2. 两个输入框换成下拉框，只列**当前配置**里标了对应能力标签的模型
   （图像处理 ← 图像识别，图像生成 ← 图像生成），直接选，不用手打；
3. 存的口径没变：仍 POST /api/model/media 写进激活 profile（tests/test_gui_media_settings.py
   继续锁后端行为）。

这里锁两类东西：
- 静态守卫（同 test_gui_blank_pill.py 的思路）：面板/入口在不在、旧区块移没移出、
  title 指引改没改 —— DOM 逻辑没有可单测的纯函数切面，真跑要拖一整个 DOM 桩；
- PREFS-BEGIN/END 标记段里的纯函数 capableModelIds：用 node 原样 eval 后跑断言
  （同 test_frontend_highlight.py 的思路）。node 不在时这组 skip。
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "forgeagent" / "gui" / "assets" / "index.html"


def _index_text() -> str:
    assert INDEX.is_file(), f"找不到界面资源：{INDEX}"
    return INDEX.read_text(encoding="utf-8")


# ---- 静态守卫 ----


def test_prefs_pane_exists_with_capability_selects():
    """个性化设置面板 + 两个能力下拉 + 保存按钮必须都在。"""
    src = _index_text()
    for frag in (
        'id="prefs-pane"',
        'id="sm-prefs"',               # 设置菜单入口
        'id="pf-vision-model"',        # 图像处理下拉
        'id="pf-image-model"',         # 图像生成下拉
        'id="pf-save"',
        'id="pf-close"',
        "图像处理（看懂图片）",          # 下拉 label（说清这个能力是干什么的）
        "图像生成（对话里出图）",
    ):
        assert frag in src, f"index.html 里少了 {frag}"


def test_media_section_moved_out_of_models_pane():
    """「模型配置」里不许再留多媒体区块（面板已单开，留两份必然改一处忘一处）。"""
    src = _index_text()
    for frag in ('id="m-media"', 'id="m-vision-model"', 'id="m-image-model"', "m-media-save"):
        assert frag not in src, f"index.html 里还残留旧的多媒体区块引用：{frag}"
    # 旧的两个手打输入框的占位提示也别留（打错模型名不报错就是它造成的）
    assert "默认图像识别模型，如" not in src


def test_prefs_menu_item_opens_pane():
    """设置菜单的「个性化设置」入口必须真的调 openPrefsPane（不是死按钮）。"""
    src = _index_text()
    m = re.search(
        r'getElementById\("sm-prefs"\)\.addEventListener\("click",\s*function \(\) \{(.*?)\}\)',
        src,
        re.S,
    )
    assert m, "sm-prefs 的点击处理没了"
    body = m.group(1)
    assert "closeSettings()" in body and "openPrefsPane()" in body


def test_save_prefs_posts_media_api():
    """保存仍走 /api/model/media（后端行为由 test_gui_media_settings.py 锁）。"""
    src = _index_text()
    m = re.search(r"\n  function savePrefsPane\(", src)
    assert m, "savePrefsPane 没了"
    i = src.index("{", m.end() - 1)
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                body = src[i : j + 1]
                break
    else:  # pragma: no cover - 括号不闭合时上面循环不会 break
        raise AssertionError("savePrefsPane 的大括号没闭合")
    assert '"/api/model/media"' in body
    assert "pf-vision-model" in body and "pf-image-model" in body


def test_gen_only_row_title_points_to_prefs():
    """纯图像生成模型行的 title 必须指向新位置（旧文案指向上已经不存在的区块）。"""
    src = _index_text()
    m = re.search(r'row\.title = "([^"]*图像生成模型，不能当聊天模型用[^"]*)"', src)
    assert m, "gen-only 行的 title 没了"
    assert "个性化设置" in m.group(1), f"title 没指向新面板：{m.group(1)}"


# ---- PREFS 标记段：node 纯函数断言 ----


def _extract_marker() -> str:
    src = _index_text()
    m = re.search(r"/\* ?PREFS-BEGIN.*?\*/(.*?)/\* ?PREFS-END", src, re.S)
    assert m, "index.html 里的 PREFS 标记段丢了"
    return m.group(1)


def _run_node(snippet: str) -> str:
    node = shutil.which("node")
    assert node, "需要 node"
    # 用 .cjs 文件而不是 -e：新版 node 会对 -e 文本做 TypeScript 解析，
    # 把断言里的 `<span class=...>` 当泛型/TSX 报语法错。
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


@pytest.mark.skipif(shutil.which("node") is None, reason="需要 node")
def test_capable_model_ids_filters_by_explicit_caps():
    out = _run_node(
        _with_marker(
            r"""
            const assert = require("assert");
            const entries = [
              { id: "glm-4.5-air", caps: ["文字"] },
              { id: "glm-4.6v", caps: ["文字", "图像识别"] },
              { id: "glm-image", caps: ["图像生成"] },
            ];
            const vis = capableModelIds(entries, "图像识别", () => ["文字"]);
            assert.deepStrictEqual(vis, ["glm-4.6v"], "显式标签过滤不对: " + vis);
            const gen = capableModelIds(entries, "图像生成", () => ["文字"]);
            assert.deepStrictEqual(gen, ["glm-image"], "图像生成过滤不对: " + gen);
            console.log("ok1");
            """
        )
    )
    assert "ok1" in out


@pytest.mark.skipif(shutil.which("node") is None, reason="需要 node")
def test_capable_model_ids_falls_back_to_name_heuristic():
    """caps 为空（没标过 / 老格式）时回落调用方传入的名字启发式，与列表徽章同口径。"""
    out = _run_node(
        _with_marker(
            r"""
            const assert = require("assert");
            const entries = [{ id: "glm-4.6v", caps: [] }, "cogview-3-plus"];
            // 启发式桩：带 v 收尾 → 图像识别；cogview → 图像生成
            const capsOf = (id) => /v$/.test(id) ? ["文字", "图像识别"] : ["图像生成"];
            assert.deepStrictEqual(capableModelIds(entries, "图像识别", capsOf), ["glm-4.6v"]);
            assert.deepStrictEqual(capableModelIds(entries, "图像生成", capsOf), ["cogview-3-plus"]);
            console.log("ok2");
            """
        )
    )
    assert "ok2" in out


@pytest.mark.skipif(shutil.which("node") is None, reason="需要 node")
def test_capable_model_ids_dedupes_and_skips_blank():
    out = _run_node(
        _with_marker(
            r"""
            const assert = require("assert");
            const entries = [
              { id: "glm-4.6v", caps: ["图像识别"] },
              { id: "glm-4.6v", caps: ["图像识别"] },   // 重复 id
              { id: "  ", caps: ["图像识别"] },          // 空白 id
              { id: "qwen3-vl", caps: ["图像识别"] },
            ];
            const vis = capableModelIds(entries, "图像识别", () => []);
            assert.deepStrictEqual(vis, ["glm-4.6v", "qwen3-vl"], "去重/顺序不对: " + vis);
            console.log("ok3");
            """
        )
    )
    assert "ok3" in out
