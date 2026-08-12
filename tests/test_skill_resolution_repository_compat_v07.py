from __future__ import annotations

from pathlib import Path

from sdai.agent_platform.skills import list_skills
from sdai.skill_resolution import load_skill_metadata


def test_all_repository_builtin_skill_sidecars_remain_resolver_compatible() -> None:
    root = Path(__file__).resolve().parents[1]
    skills = list_skills(root)

    assert skills
    metadata = [load_skill_metadata(root, skill) for skill in skills]

    assert [item.name for item in metadata] == [skill.name for skill in skills]
    assert all(item.source.startswith(".agents/skills/") for item in metadata)
