"""产物卡片的静态结构回归 —— index.html 必须长着完整的提取→展示→打开链路。

参考 test_gui_blank_pill.py 的做法：渲染层的行为没法脱离图形环境单测，
但「代码在不在、接没接上」可以用静态断言钉住；真机渲染验证走 CDP。
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _index() -> str:
    return (ROOT / "forgeagent" / "gui" / "assets" / "index.html").read_text(encoding="utf-8")


def test_artifact_extraction_wired_into_tool_events():
    raw = _index()
    # 提取逻辑存在
    assert "ARTIFACT_RE" in raw
    assert "已(?:新建|覆盖|修改)" in raw
    # 只对 edit 类工具、完成状态触发（write_file / edit / make_xlsx）
    assert 'card._kindVal === "edit"' in raw
    assert "extractArtifact(card" in raw


def test_artifact_open_goes_through_server_routes():
    raw = _index()
    # 拿大小/绝对路径 + 打开，走的是带 token 的 /api 路由，不是裸 shell
    assert '"/api/file_info"' in raw
    assert '"/api/open_path"' in raw


def test_server_has_artifact_routes():
    raw = (ROOT / "forgeagent" / "gui" / "server.py").read_text(encoding="utf-8")
    assert 'u.path == "/api/file_info"' in raw
    assert 'u.path == "/api/open_path"' in raw
    # 白名单与扩展名黑名单都在
    assert "_tool_results_root" in raw
    assert "_ARTIFACT_DENYLIST_EXT" in raw
