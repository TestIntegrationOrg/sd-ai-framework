from __future__ import annotations

from pathlib import Path

import pytest

from sdai.agent_platform.definitions import (
    AgentDefinitionError,
    build_agent_definition_registry,
    explain_agent_definition,
    list_agent_definitions,
    load_agent_definition,
    resolve_agent_definition,
)
from sdai.agent_platform.models import Capability
from sdai.agent_platform.skills import (
    SkillError,
    build_skill_registry,
    explain_skill,
    list_skills,
    load_skill,
)
from sdai.extensions import ExtensionKind, RegistryLayer


def _write_agent(
    root: Path,
    name: str,
    *,
    capabilities: tuple[str, ...] = ("coding",),
    skills: tuple[str, ...] = (),
    providers: str = "",
) -> Path:
    path = root / ".sdai" / "agents" / f"{name}.agent.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    capability_yaml = "\n".join(f"  - {item}" for item in capabilities)
    skill_yaml = "\n".join(f"  - {item}" for item in skills)
    skill_block = f"skills:\n{skill_yaml}\n" if skills else "skills: []\n"
    provider_block = providers if providers else "providers: {}\n"
    path.write_text(
        f"""---
name: {name}
description: {name} semantic agent
capabilities:
{capability_yaml}
{skill_block}execution_mode: advisory
{provider_block}---

Follow the approved specification for {name}.
""",
        encoding="utf-8",
    )
    return path.resolve()


def _write_canonical_skill(
    root: Path,
    name: str,
    *,
    description: str = "Canonical skill",
    capabilities: tuple[str, ...] = ("coding",),
    instructions: str = "Use canonical guidance.",
) -> Path:
    skill_root = root / ".agents" / "skills" / name
    skill_root.mkdir(parents=True, exist_ok=True)
    skill_path = skill_root / "SKILL.md"
    skill_path.write_text(
        f"""---
name: {name}
description: {description}
---

{instructions}
""",
        encoding="utf-8",
    )
    if capabilities:
        capability_yaml = "\n".join(f"  - {item}" for item in capabilities)
        (skill_root / "sdai.yaml").write_text(
            f"capabilities:\n{capability_yaml}\n",
            encoding="utf-8",
        )
    return skill_path.resolve()


def _write_legacy_skill(
    root: Path,
    name: str,
    *,
    description: str = "Legacy skill",
    capabilities: tuple[str, ...] = ("coding",),
    instructions: str = "Use legacy guidance.",
) -> Path:
    skill_root = root / ".sdai" / "skills" / name
    skill_root.mkdir(parents=True, exist_ok=True)
    capability_yaml = "\n".join(f"  - {item}" for item in capabilities)
    (skill_root / "skill.yaml").write_text(
        f"""name: {name}
description: {description}
capabilities:
{capability_yaml}
""",
        encoding="utf-8",
    )
    (skill_root / "SKILL.md").write_text(instructions, encoding="utf-8")
    return (skill_root / "skill.yaml").resolve()


def test_agent_load_is_registry_backed_and_preserves_public_definition(tmp_path: Path) -> None:
    path = _write_agent(
        tmp_path,
        "developer",
        capabilities=("coding", "testing"),
        skills=("secure-coding",),
    )

    entry = explain_agent_definition(tmp_path, "developer")
    definition = load_agent_definition(tmp_path, "developer")

    assert entry.layer is RegistryLayer.REPO
    assert entry.key.kind is ExtensionKind.AGENT
    assert entry.key.id == "developer"
    assert entry.path == path
    assert entry.source == str(path)
    assert entry.manifest.spec == {"format": "sdai-agent-markdown"}
    assert definition.name == "developer"
    assert definition.capabilities == (Capability.CODING, Capability.TESTING)
    assert definition.skills == ("secure-coding",)
    assert definition.path == path
    assert "approved specification" in definition.instructions


def test_agent_registry_and_list_are_deterministic(tmp_path: Path) -> None:
    _write_agent(tmp_path, "tester", capabilities=("testing",))
    _write_agent(tmp_path, "architect", capabilities=("architecture",))

    registry = build_agent_definition_registry(tmp_path)
    definitions = list_agent_definitions(tmp_path)

    assert [entry.key.id for entry in registry.list_resolved(ExtensionKind.AGENT)] == [
        "architect",
        "tester",
    ]
    assert [definition.name for definition in definitions] == ["architect", "tester"]


def test_agent_route_resolution_behavior_is_unchanged(tmp_path: Path) -> None:
    _write_agent(tmp_path, "architect", capabilities=("architecture",))
    routing = tmp_path / ".sdai" / "agent-routing.yaml"
    routing.write_text("routes:\n  architecture: architect\n", encoding="utf-8")

    resolved = resolve_agent_definition(tmp_path, Capability.ARCHITECTURE)

    assert resolved is not None
    assert resolved.name == "architect"


def test_agent_capability_mismatch_still_fails(tmp_path: Path) -> None:
    _write_agent(tmp_path, "developer", capabilities=("coding",))

    with pytest.raises(AgentDefinitionError, match="does not support 'security'"):
        resolve_agent_definition(
            tmp_path,
            Capability.SECURITY,
            requested="developer",
        )


def test_unknown_agent_error_is_preserved(tmp_path: Path) -> None:
    with pytest.raises(AgentDefinitionError, match="Unknown semantic agent 'missing'"):
        load_agent_definition(tmp_path, "missing")


def test_agent_provider_security_validation_still_runs_after_registry_resolution(
    tmp_path: Path,
) -> None:
    _write_agent(
        tmp_path,
        "developer",
        providers="providers:\n  codex:\n    api_token: do-not-store-this\n",
    )

    with pytest.raises(AgentDefinitionError, match="credential-like key"):
        load_agent_definition(tmp_path, "developer")


def test_canonical_skill_wins_over_legacy_with_same_name(tmp_path: Path) -> None:
    canonical_path = _write_canonical_skill(
        tmp_path,
        "secure-coding",
        description="Canonical security",
        instructions="Canonical instructions",
    )
    _write_legacy_skill(
        tmp_path,
        "secure-coding",
        description="Legacy security",
        instructions="Legacy instructions",
    )

    entry = explain_skill(tmp_path, "secure-coding")
    skill = load_skill(tmp_path, "secure-coding")

    assert entry.path == canonical_path
    assert entry.manifest.spec == {"format": "agents-skill"}
    assert skill.description == "Canonical security"
    assert skill.instructions == "Canonical instructions"
    assert skill.root == canonical_path.parent


def test_legacy_skill_is_registry_backed_when_no_canonical_skill_exists(
    tmp_path: Path,
) -> None:
    manifest_path = _write_legacy_skill(
        tmp_path,
        "legacy-only",
        description="Legacy only",
        capabilities=("review",),
        instructions="Legacy review instructions",
    )

    entry = explain_skill(tmp_path, "legacy-only")
    skill = load_skill(tmp_path, "legacy-only")

    assert entry.layer is RegistryLayer.REPO
    assert entry.path == manifest_path
    assert entry.manifest.spec == {"format": "sdai-legacy-skill"}
    assert skill.description == "Legacy only"
    assert skill.capabilities == (Capability.REVIEW,)
    assert skill.instructions == "Legacy review instructions"


def test_existing_uppercase_legacy_skill_name_remains_compatible(tmp_path: Path) -> None:
    _write_legacy_skill(tmp_path, "LegacySkill", instructions="Compatible legacy skill")

    entry = explain_skill(tmp_path, "LegacySkill")
    skill = load_skill(tmp_path, "LegacySkill")

    assert entry.key.id == "LegacySkill"
    assert skill.name == "LegacySkill"
    assert skill.instructions == "Compatible legacy skill"


def test_skill_registry_lists_canonical_and_non_shadowed_legacy_skills(tmp_path: Path) -> None:
    _write_canonical_skill(tmp_path, "alpha")
    _write_canonical_skill(tmp_path, "shared", instructions="canonical shared")
    _write_legacy_skill(tmp_path, "shared", instructions="legacy shared")
    _write_legacy_skill(tmp_path, "zeta")

    registry = build_skill_registry(tmp_path)
    skills = list_skills(tmp_path)

    assert [entry.key.id for entry in registry.list_resolved(ExtensionKind.SKILL)] == [
        "alpha",
        "shared",
        "zeta",
    ]
    assert [skill.name for skill in skills] == ["alpha", "shared", "zeta"]
    assert next(skill for skill in skills if skill.name == "shared").instructions == (
        "canonical shared"
    )


def test_missing_skill_error_is_preserved(tmp_path: Path) -> None:
    with pytest.raises(
        SkillError,
        match="Skill 'missing' must contain skill.yaml and SKILL.md",
    ):
        load_skill(tmp_path, "missing")


def test_malformed_canonical_skill_still_fails_in_original_parser(tmp_path: Path) -> None:
    skill_root = tmp_path / ".agents" / "skills" / "broken"
    skill_root.mkdir(parents=True)
    (skill_root / "SKILL.md").write_text("not frontmatter", encoding="utf-8")

    entry = explain_skill(tmp_path, "broken")
    assert entry.manifest.spec == {"format": "agents-skill"}

    with pytest.raises(SkillError, match="must start with YAML frontmatter"):
        load_skill(tmp_path, "broken")
