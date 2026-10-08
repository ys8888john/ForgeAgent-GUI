"""统一数据目录：所有跨平台落点都收在 ``~/.agentd``（参考 WorkBuddy 的 ``~/.workbuddy`` 风格）。

布局（mac / linux / windows 三端完全一致，不写平台分支）：

    ~/.agentd/                  # Agentd 内核自己管理的文件
        memory.md               # 长期事实层（人可读、可直接手改）
        sessions.db             # SQLite 会话库
        hotenv.json             # Agentd 兜底热配置读路径（Agentd 只读，从不写）
        gui/                    # GUI 管理的文件，单独子目录以免和上面撞名
            models.json         # 自定义模型 profile
            mcp.json           # 本地 MCP server 配置
            hotenv.json        # GUI 写入的活动 profile 热配置（GUI 通过 AGENTD_HOTENV 指过去）
            mcp-venv/          # 本地 MCP server 专用 venv

为什么 GUI 放在 ``gui/`` 子目录：Agentd 的兜底热配置读路径是 ``~/.agentd/hotenv.json``，
而 GUI 切模型时会把活动 profile 的环境变量写进自己的 hotenv.json 并设 ``AGENTD_HOTENV``
指过去。若 GUI 也直接写 ``~/.agentd/hotenv.json``，就会覆盖 Agentd 的兜底路径，导致
单独跑 CLI agentd 时误读到 GUI 残留的配置。用子目录隔离后两边各归各位。

旧版本 GUI 把文件放在 ``~/.forgeagent``，首次加载本模块会把旧目录整体搬进
``~/.agentd/gui``（幂等：仅当旧目录存在、新目录不存在时执行）。
"""

from __future__ import annotations

import shutil
from pathlib import Path

# Agentd 内核数据根：mac/linux/windows 一律 ~/.agentd
AGENTD_DIR = Path.home() / ".agentd"

# GUI 管理的文件统一放这里
GUI_DIR = AGENTD_DIR / "gui"

# 空间（Space）目录：用户没指定目录时，工作目录落在这里面
#   ~/.agentd/spaces/           未显式指定路径的 Space 的默认所在（default 等）
#   ~/.agentd/spaces.json       空间注册表 {active, spaces:[{name,path,created_at}]}
#   ~/.agentd/session_spaces.json  会话 -> 空间 绑定（续聊时还原 cwd 用）
SPACES_DIR = AGENTD_DIR / "spaces"


def spaces_meta_path() -> Path:
    """空间注册表文件：和 sessions.db 同目录（~/.agentd 根）。"""
    return AGENTD_DIR / "spaces.json"


def migrate_legacy_forgeagent() -> None:
    """一次性迁移：把旧的 ``~/.forgeagent`` 整体搬进 ``~/.agentd/gui``。

    幂等安全：仅当旧目录存在、新目录尚不存在时执行；失败也不抛异常
    （不应阻断启动，用户可手动移动）。
    """
    legacy = Path.home() / ".forgeagent"
    target = GUI_DIR
    if not legacy.is_dir() or target.exists():
        return
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(legacy), str(target))
    except OSError:
        pass


# 模块加载即尝试一次迁移（幂等）
migrate_legacy_forgeagent()
