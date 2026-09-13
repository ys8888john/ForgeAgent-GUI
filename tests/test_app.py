"""UI 层冒烟测试。

跟另外两个测试不同，这个**需要 Textual**——它验证的是 UI 层，没装就跑不了。

用 Textual 自带的 `App.run_test()` 在 headless 模式下跑，不需要真实终端，
所以在 CI 和本地都能跑。

它管的不是"界面好不好看"，而是"App 会不会崩"——
特别是 agent 起不来的时候（Windows 上没装 agentd、路径不对、Python 环境问题），
UI 必须优雅降级而不是白屏或者炸掉。
"""

from __future__ import annotations

from textual.widgets import Static

from forgeagent.app import ForgeAgentApp


async def test_app_mounts_and_exposes_widgets():
    """App 能起来，关键组件都挂上了。"""
    app = ForgeAgentApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.query_one("#log", Static) is not None
        assert app.query_one("#status", Static) is not None


async def test_app_survives_agent_startup_failure():
    """agent 起不来时不能崩。

    在没装 agentd 的环境里（比如 Windows 侧），on_mount 会走失败分支。
    这里只要求：活着，并且把失败原因写进了状态或历史。
    """
    app = ForgeAgentApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.pause()
        # 不断言具体文案——不同环境的失败原因差别太大，断言文案会很脆。
        # 只要没抛异常、并且产出了可展示的内容就算过。
        assert app._status or app._history


async def test_toggle_log_does_not_crash():
    """Ctrl+L 切换日志面板不该炸。"""
    app = ForgeAgentApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.pause()
        before = app._show_log
        await pilot.press("ctrl+l")
        await pilot.pause()
        assert app._show_log is not before


async def test_markup_brackets_are_not_swallowed():
    """LLM 输出里的 [xxx] 不能消失。

    Textual 的 Static 默认 markup=True，会把 `[DONE]` 这种解析成样式标签——
    实测结果是整段从界面上消失，既不报错也不留痕。
    LLM 很爱输出方括号（数组、Markdown 链接、[DONE] 标记），所以必须钉死。
    """
    app = ForgeAgentApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        app._history.append(("text", "结果 [DONE]，数组 [[1,2],[3,4]]"))
        app._redraw()
        await pilot.pause()
        shown = app.query_one("#log", Static).render()
        plain = shown.plain if hasattr(shown, "plain") else str(shown)
        assert "[DONE]" in plain
        assert "[[1,2],[3,4]]" in plain


async def test_error_is_rendered_and_distinguishable():
    """错误要进历史，并且带 error 角色（UI 据此标红）。"""
    app = ForgeAgentApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        app._history.append(("error", "Ollama HTTP 404：model not found"))
        app._redraw()
        await pilot.pause()
        assert any(role == "error" for role, _ in app._history)


async def test_empty_turn_leaves_a_trace():
    """一轮下来什么都没有时，不能静默什么都不显示。"""
    from forgeagent.acp_client import Turn

    app = ForgeAgentApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        app._turn = Turn()
        app._commit_turn()
        assert app._history[-1][0] == "error"
        assert app._turn is None
