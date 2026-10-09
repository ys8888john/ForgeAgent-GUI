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
    # 完成状态即触发、kind 不设限：make_xlsx 在 agentd 里是 kind=execute，
    # 只认 edit 会把它的产物整个漏掉（2026-10-09 真机首跑实锤——Excel 生成
    # 了、正文也报了路径，产物卡和托盘却都没出现）。是否落盘输出交给
    # ARTIFACT_RE 把关，这里钉住「edit 门槛不能再回来」。
    assert "extractArtifact(card" in raw
    assert 'card._kindVal === "edit"' not in raw


def test_artifact_open_goes_through_server_routes():
    raw = _index()
    # 拿大小/绝对路径 + 打开，走的是带 token 的 /api 路由，不是裸 shell
    assert '"/api/file_info"' in raw
    assert '"/api/open_path"' in raw


def test_session_artifact_tray_wired_and_reset():
    """会话级「查看所有产物 (N)」托盘：注册、去重、切会话清空，三件都在。"""
    raw = _index()
    assert "查看所有产物 (" in raw
    assert "function trayRegister" in raw
    assert "function resetTray" in raw
    assert "function ensureTray" in raw
    # 注册产物后必须把托盘翻回可见（ensureTray 建节点时是 display:none）。
    # 2026-10-09 实锤：删展开列表时把显示切换弄丢了，托盘计数走到 1、
    # 胶囊却永远 display:none。两个注册分支都要调 showTray。
    assert "function showTray" in raw
    assert raw.count("showTray()") >= 2
    # 切会话的两处（renderHistory / newChat）都必须清托盘；新一轮开始的
    # 防御性 toolCards 重置不能清（产物跨回合累积）。
    assert raw.count("resetTray()") >= 2
    # 托盘常驻消息流末尾靠 CSS order，而不是追着 appendPoint 挪
    assert "order: 9999" in raw


def test_inline_shows_only_latest_and_tray_click_reveals_folder():
    """行为对齐 WorkBuddy：消息流里只挂最新目标产物；点托盘 = 弹文件夹。"""
    raw = _index()
    # 内联替换：新产物出现时旧的降级（turnInline.row.remove()）
    assert "turnInline" in raw and "turnInline.row.remove()" in raw
    # 托盘点击不再展开列表，而是 reveal 到文件管理器
    assert "revealLatest" in raw
    assert 'reveal: true' in raw
    assert "artifact-tray-list" not in raw


def test_server_has_artifact_routes():
    raw = (ROOT / "forgeagent" / "gui" / "server.py").read_text(encoding="utf-8")
    assert 'u.path == "/api/file_info"' in raw
    assert 'u.path == "/api/open_path"' in raw
    # 白名单与扩展名黑名单都在
    assert "_tool_results_root" in raw
    assert "_ARTIFACT_DENYLIST_EXT" in raw
    # reveal 分支：文件管理器定位，不落扩展名黑名单
    assert "def _reveal_in_folder" in raw
    assert 'body.get("reveal")' in raw
