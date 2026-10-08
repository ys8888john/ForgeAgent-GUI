"""Space（空间）管理：参考 WorkBuddy 的「空间」概念。

一个 Space 就是一个**具名工作目录**。在 GUI 里在某个 Space 下开会话时，会话自动
绑定到该 Space 的目录（cwd）；agentd 的原生工具（读/写/检索/列目录）就以这个目录
为根 —— 多 Space 互不串门。

落盘（沿用 sessions.py 的范式，文件都放在 ``~/.agentd`` 根下，与 sessions.db 同级）：

    ~/.agentd/spaces.json           空间注册表 {active, spaces:[{name,path,created_at}]}
    ~/.agentd/spaces/              未显式指定路径的 Space 的默认所在（default 等）
    ~/.agentd/session_spaces.json  session_id -> space_name 绑定（续聊时还原 cwd 用）

首次运行自动建一个 ``default`` 空间，目录 ``~/.agentd/spaces/default``。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .paths import AGENTD_DIR, SPACES_DIR, spaces_meta_path

DEFAULT_SPACE = "default"


def _expand(p: str) -> Path:
    """展开 ``~`` 并解析成绝对路径（容错：解析不了就原样绝对化）。"""
    try:
        return Path(os.path.expanduser(p)).expanduser().resolve()
    except (OSError, RuntimeError):
        return Path(os.path.expanduser(p)).expanduser().absolute()


def _safe_name(name: str) -> str:
    """空间名只能是简短的标识：去空白、砍掉路径/文件系统禁用的字符。

    Windows 与 POSIX 共用的非法字符一并清掉，避免被当成路径或注入。
    """
    name = (name or "").strip().replace("\\", "-").replace("/", "-")
    bad = set('<>:"|?*\x00\t\n\r')
    name = "".join(ch for ch in name if ch not in bad)
    name = name.strip().strip(".")
    return name[:40]


class SpaceManager:
    """空间注册表 + 会话绑定。UiServer 持有一个；测试可注入假的或换路径。"""

    def __init__(
        self,
        meta_path: Path | None = None,
        session_spaces_path: Path | None = None,
    ) -> None:
        self.meta_path = meta_path or spaces_meta_path()
        self.spaces_path = session_spaces_path or (AGENTD_DIR / "session_spaces.json")
        self._ensure_default()

    # ---- 注册表读写（JSON 文件，和 hidden_sessions.json 同款范式）----

    def _load(self) -> dict:
        try:
            data = json.loads(self.meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        data.setdefault("active", "")
        data.setdefault("spaces", [])
        if not isinstance(data["spaces"], list):
            data["spaces"] = []
        return data

    def _save(self, data: dict) -> None:
        self.meta_path.parent.mkdir(parents=True, exist_ok=True)
        self.meta_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _ensure_default(self) -> None:
        """首次运行创建 default 空间（目录 ``~/.agentd/spaces/default``）。幂等。"""
        data = self._load()
        if any(s.get("name") == DEFAULT_SPACE for s in data["spaces"]):
            if not data.get("active"):
                data["active"] = DEFAULT_SPACE
                self._save(data)
            return
        self.add_space(DEFAULT_SPACE, path=str(SPACES_DIR / DEFAULT_SPACE))
        data = self._load()
        if not data.get("active"):
            data["active"] = DEFAULT_SPACE
            self._save(data)

    # ---- 查询 ----

    @property
    def active_name(self) -> str:
        """当前激活的空间名；找不到就回落到 default。"""
        data = self._load()
        name = data.get("active") or ""
        if name and any(s.get("name") == name for s in data["spaces"]):
            return name
        # active 指向了一个已不存在的空间：回落到第一个，再不行用 default
        if data["spaces"]:
            return data["spaces"][0]["name"]
        return DEFAULT_SPACE

    def active_path(self) -> str:
        """当前激活空间的目录（绝对路径字符串）。"""
        name = self.active_name
        data = self._load()
        for s in data["spaces"]:
            if s.get("name") == name:
                return s.get("path") or str(SPACES_DIR / name)
        return str(SPACES_DIR / name)

    def list_spaces(self) -> list[dict]:
        """给前端下拉框用：每个空间带 is_active 标记。"""
        data = self._load()
        active = self.active_name
        return [
            {
                "name": s.get("name", ""),
                "path": s.get("path", ""),
                "is_active": s.get("name") == active,
            }
            for s in data["spaces"]
        ]

    def resolve_path(self, name: str) -> str | None:
        """空间名 -> 目录；不存在返回 None。"""
        data = self._load()
        for s in data["spaces"]:
            if s.get("name") == name:
                return s.get("path")
        return None

    # ---- 增 / 切 ----

    def add_space(self, name: str, path: str | None = None) -> dict:
        """新建一个空间；不传 path 就放在 ``~/.agentd/spaces/<name>``。

        返回 ``{"name", "path"}``。空间名重复抛 ValueError。
        """
        name = _safe_name(name)
        if not name:
            raise ValueError("空间名不能为空")
        data = self._load()
        if any(s.get("name") == name for s in data["spaces"]):
            raise ValueError(f"空间已存在: {name}")
        p = _expand(path) if path else (SPACES_DIR / name)
        try:
            p.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise OSError(f"无法创建空间目录 {p}: {exc}") from exc
        data["spaces"].append(
            {"name": name, "path": str(p), "created_at": time.time()}
        )
        if not data.get("active"):
            data["active"] = name
        self._save(data)
        return {"name": name, "path": str(p)}

    def set_active(self, name: str) -> None:
        """把某个已存在的空间设为激活。不存在抛 ValueError。"""
        data = self._load()
        if not any(s.get("name") == name for s in data["spaces"]):
            raise ValueError(f"没有这个空间: {name}")
        data["active"] = name
        self._save(data)

    def ensure_space_for_path(self, path: str) -> str:
        """给定任意目录，落到「已存在的空间」或「按目录名新建的空间」，并激活。

        用于 ``--cwd /some/project`` 这类「用这个目录当工作区」的入口：把它变成
        一个可切换、可持久化的 Space，而不是散落的 ad-hoc 目录。重复调用幂等
        （同路径永远映射到同一个空间名）。
        """
        target = _expand(path)
        data = self._load()
        for s in data["spaces"]:
            if _expand(s.get("path", "")) == target:
                if data.get("active") != s["name"]:
                    data["active"] = s["name"]
                    self._save(data)
                return s["name"]
        base = _safe_name(target.name) or "workspace"
        name = base
        i = 2
        while any(s["name"] == name for s in data["spaces"]):
            name = f"{base}{i}"
            i += 1
        self.add_space(name, path=str(target))
        return name

    # ---- 会话绑定 ----

    def _load_bindings(self) -> dict[str, str]:
        try:
            data = json.loads(self.spaces_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save_bindings(self, data: dict[str, str]) -> None:
        self.spaces_path.parent.mkdir(parents=True, exist_ok=True)
        self.spaces_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def bind(self, session_id: str, space_name: str) -> None:
        """把会话归属到某个空间（续聊时据此还原 cwd）。"""
        bindings = self._load_bindings()
        bindings[session_id] = space_name
        self._save_bindings(bindings)

    def space_of(self, session_id: str) -> str | None:
        """这个会话属于哪个空间（不存在绑定返回 None）。"""
        return self._load_bindings().get(session_id)
