import os
from pathlib import Path

import pytest

from sdai.agent_platform.definitions import load_agent_definition
from sdai.agent_platform.native import sync_native_agents
from sdai.agent_platform.skills import load_skill
from sdai.path_safety import PathSafetyError
from sdai.scaffold import init_project
from sdai.v05_scaffold import install_v05_scaffold


def _symlink(target: Path, link: Path) -> None:
    try:
        os.symlink(target, link, target_is_directory=target.is_dir())
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks unavailable in test environment: {exc}")


def test_native_sync_rejects_provider_directory_symlink_escape(tmp_path: Path):
    init_project(tmp_path)
    install_v05_scaffold(tmp_path)
    outside = tmp_path.parent / f"{tmp_path.name}-outside-native"
    outside.mkdir()
    _symlink(outside, tmp_path / ".github")

    with pytest.raises(PathSafetyError, match="inside the project workspace"):
        sync_native_agents(tmp_path, provider="copilot")


def test_agent_definition_rejects_symlink_escape(tmp_path: Path):
    init_project(tmp_path)
    outside = tmp_path.parent / f"{tmp_path.name}-outside-agents"
    outside.mkdir()
    (outside / "architect.agent.md").write_text(
        """---
name: architect
description: outside agent
capabilities: [architecture]
---
# Outside
Do not load this file through a project symlink.
""",
        encoding="utf-8",
    )
    _symlink(outside, tmp_path / ".sdai" / "agents")

    with pytest.raises(PathSafetyError, match="inside the project workspace"):
        load_agent_definition(tmp_path, "architect")


def test_skill_name_cannot_traverse_outside_project(tmp_path: Path):
    init_project(tmp_path)
    with pytest.raises(RuntimeError):
        load_skill(tmp_path, "../../outside")
