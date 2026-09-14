"""本地 MCP 预设（P1）的纯逻辑测试。

mcp_presets 不碰网络也不碰 agentd，全是纯函数，所以可以完整地离线测。
真正"能不能连上"由 `scripts/verify_local_mcp.py` 负责，不在这里重复。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from forgeagent.gui import mcp_presets
from forgeagent.gui.mcp_config import load_mcp_servers
from forgeagent.gui.mcp_presets import (
    CWD_PLACEHOLDER,
    PRESETS,
    Preset,
    build_entries,
    config_root,
    find_git_dir,
    merge_config,
    path_with_git,
    python_bin,
    server_entry,
    venv_dir,
)


# ---------- 路径推断 ----------


def test_python_bin_windows():
    assert python_bin("D:/v", platform="win32") == Path("D:/v/Scripts/python.exe")


def test_python_bin_posix():
    assert python_bin("/home/u/v", platform="linux") == Path("/home/u/v/bin/python")
    assert python_bin("/home/u/v", platform="darwin") == Path("/home/u/v/bin/python")


def test_venv_dir_sits_next_to_config(tmp_path):
    """专用 venv 要和 mcp.json 同一个目录（~/.forgeagent/），互相不干扰。"""
    assert config_root(tmp_path) == tmp_path / ".forgeagent"
    assert venv_dir(tmp_path) == tmp_path / ".forgeagent" / "mcp-venv"


# ---------- 条目生成 ----------


def test_entry_uses_absolute_interpreter_and_module(tmp_path):
    entry = server_entry(PRESETS["time"], tmp_path)
    assert entry["command"] == str(python_bin(tmp_path))
    assert entry["args"] == ["-m", PRESETS["time"].module]
    # 不该习惯性塞 env：PATH 在 SDK 的默认继承白名单里
    assert "env" not in entry


def test_only_git_preset_carries_env(tmp_path, monkeypatch):
    git_dir = tmp_path / "gitbin"
    monkeypatch.setattr(mcp_presets, "find_git_dir", lambda: git_dir)
    entry = server_entry(PRESETS["git"], tmp_path)
    assert list(entry["env"]) == ["PATH"]
    assert entry["env"]["PATH"].split(os.pathsep)[0] == str(git_dir)

    for name in ("time", "fetch"):
        assert "env" not in server_entry(PRESETS[name], tmp_path)


def test_build_entries_unknown_name_raises(tmp_path):
    with pytest.raises(KeyError):
        build_entries(["nope"], tmp_path)


def test_build_entries_keys_are_preset_names(tmp_path):
    entries = build_entries(["time", "git"], tmp_path)
    assert set(entries) == {"time", "git"}


# ---------- PATH 拼装 ----------


def test_path_with_git_prepends(monkeypatch, tmp_path):
    git_dir = tmp_path / "gitbin"
    monkeypatch.setattr(mcp_presets, "find_git_dir", lambda: git_dir)
    base = os.pathsep.join([str(tmp_path / "usr"), str(tmp_path / "bin")])
    out = path_with_git(base)
    assert out.split(os.pathsep)[0] == str(git_dir)
    assert out.endswith(base)


def test_path_with_git_is_idempotent(monkeypatch, tmp_path):
    git_dir = tmp_path / "gitbin"
    monkeypatch.setattr(mcp_presets, "find_git_dir", lambda: git_dir)
    once = path_with_git(os.pathsep.join([str(git_dir), str(tmp_path / "usr")]))
    assert path_with_git(once) == once


def test_path_with_git_without_git_returns_base(monkeypatch):
    monkeypatch.setattr(mcp_presets, "find_git_dir", lambda: None)
    assert path_with_git("/usr/bin") == "/usr/bin"


def test_path_with_git_dedupes_and_drops_empty(monkeypatch):
    """从某些 shell 继承下来的 PATH 会有重复段落和空项，不能原样写进配置。"""
    monkeypatch.setattr(mcp_presets, "find_git_dir", lambda: None)
    raw = os.pathsep.join(["", "/a", "/b", "/a", "", "/b"])
    assert path_with_git(raw) == os.pathsep.join(["/a", "/b"])


def test_find_git_dir_prefers_path_lookup(monkeypatch, tmp_path):
    fake = tmp_path / "gitbin" / "git"
    monkeypatch.setattr(mcp_presets.shutil, "which", lambda _: str(fake))
    assert find_git_dir() == fake.parent


def test_find_git_dir_result_actually_contains_git():
    """真跑一遍 find_git_dir（不 mock）。找不到就跳过 —— 这台机器上它应该能找到
    PortableGit。找到了就必须真的是个含 git 可执行文件的目录。"""
    found = find_git_dir()
    if found is None:
        pytest.skip("本机没有可定位的 git")
    assert (found / "git.exe").is_file() or (found / "git").is_file()


# ---------- 配置合并 ----------


def test_merge_preserves_other_servers_and_top_level_keys():
    """一键安装不该把用户手配的 server 或其它顶层字段抹掉。"""
    existing = {
        "mcpServers": {"echo": {"command": "python", "args": ["s.py"]}},
        "somethingElse": 42,
    }
    merged = merge_config(existing, {"time": {"command": "x", "args": []}})
    assert merged["somethingElse"] == 42
    assert set(merged["mcpServers"]) == {"echo", "time"}
    assert merged["mcpServers"]["echo"]["args"] == ["s.py"]


def test_merge_overrides_same_name():
    merged = merge_config(
        {"mcpServers": {"time": {"command": "old"}}},
        {"time": {"command": "new"}},
    )
    assert merged["mcpServers"]["time"]["command"] == "new"


def test_merge_handles_missing_or_broken_existing():
    assert merge_config(None, {"a": {"command": "x"}}) == {
        "mcpServers": {"a": {"command": "x"}}
    }
    # 顶层不是 dict、mcpServers 不是 dict，都要能兜住
    assert merge_config(["nope"], {"a": {"command": "x"}})["mcpServers"] == {
        "a": {"command": "x"}
    }
    assert merge_config({"mcpServers": "nope"}, {"a": {"command": "x"}})["mcpServers"] == {
        "a": {"command": "x"}
    }


def test_merge_does_not_mutate_inputs():
    existing = {"mcpServers": {"echo": {"command": "python"}}}
    merge_config(existing, {"time": {"command": "x"}})
    assert set(existing["mcpServers"]) == {"echo"}


# ---------- 预设表本身的约束 ----------


def test_presets_are_wellformed():
    for key, preset in PRESETS.items():
        assert key == preset.name
        assert preset.package and preset.module and preset.summary
        assert preset.module.islower(), f"{key} 的模块名应该是 snake_case"
        assert "__" not in (preset.probe_tool or ""), "探针工具名里不该带命名空间前缀"


def test_filesystem_preset_is_intentionally_absent():
    """锁住设计决定：agentd 的原生工具已覆盖本地文件操作，
    再加一个 filesystem MCP server 只会让模型多一个选择、多一次审批。"""
    assert "filesystem" not in PRESETS


def test_git_preset_is_the_only_one_needing_git():
    assert {n for n, p in PRESETS.items() if p.needs_git} == {"git"}


def test_probe_placeholder_only_used_by_git():
    for name, preset in PRESETS.items():
        if CWD_PLACEHOLDER in str(preset.probe_args):
            assert name == "git"


def test_preset_is_frozen():
    with pytest.raises(Exception):
        PRESETS["time"].name = "other"  # type: ignore[misc]


def test_custom_preset_without_probe_is_allowed():
    p = Preset(name="x", package="p", module="m", summary="s")
    assert p.probe_tool is None and p.probe_args == {}


# ---------- 生成的配置必须能过 ACP 校验 ----------


def test_generated_config_survives_mcp_config_roundtrip(tmp_path):
    """回归最有价值的一条：生成的 mcp.json 经 mcp_config 转出来的结构，
    必须每一项都带齐 command / args / env —— 少一个字段 ACP 的 pydantic
    union 会**静默**把整份 mcpServers 变成空列表。"""
    config = merge_config(None, build_entries(list(PRESETS), tmp_path))
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")

    servers = load_mcp_servers(path)
    assert [s["name"] for s in servers] == list(PRESETS)
    for server in servers:
        assert server["command"], "command 不能为空"
        assert isinstance(server["args"], list) and server["args"]
        assert isinstance(server["env"], list)


def test_generated_config_passes_acp_new_session_request(tmp_path):
    """比上一条更硬：真的丢给 ACP 的 NewSessionRequest 校验一遍。"""
    try:
        from acp.schema import NewSessionRequest
    except ImportError:  # 没装 acp SDK 时跳过（可选依赖）
        pytest.skip("未安装 acp SDK")

    config = merge_config(None, build_entries(list(PRESETS), tmp_path))
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")

    req = NewSessionRequest.model_validate(
        {"cwd": str(tmp_path), "mcpServers": load_mcp_servers(path)}
    )
    assert [s.name for s in req.mcp_servers] == list(PRESETS)


def test_git_entry_env_survives_roundtrip(tmp_path):
    """git 那条的 env 是 dict，转成 ACP 的 [{name,value}] 后不能丢。"""
    config = merge_config(None, build_entries(["git"], tmp_path))
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    server = load_mcp_servers(path)[0]
    assert [p["name"] for p in server["env"]] == ["PATH"]
