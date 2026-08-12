from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from sdai.skill_resolution import (
    SkillResolutionError,
    compose_resolved_skills,
    load_skill_metadata,
    resolve_skills,
)


def _init(root: Path) -> None:
    path = root / ".sdai" / "config.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "operating_mode": "individual",
                "policy": {"repository": ".sdai/policy.yaml"},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _agent(
    root: Path,
    name: str = "developer",
    *,
    capabilities: tuple[str, ...] = ("coding",),
    skills: tuple[str, ...] = (),
) -> None:
    path = root / ".sdai" / "agents" / f"{name}.agent.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        + yaml.safe_dump(
            {
                "name": name,
                "description": f"{name} semantic role",
                "capabilities": list(capabilities),
                "skills": list(skills),
                "execution_mode": "advisory",
                "providers": {},
            },
            sort_keys=False,
        )
        + "---\n\nOperate within the assigned semantic role.\n",
        encoding="utf-8",
    )


def _skill(
    root: Path,
    name: str,
    *,
    capabilities: tuple[str, ...] = ("coding",),
    compatible_agents: tuple[str, ...] = (),
    requires: tuple[str, ...] = (),
    compatibility: dict[str, dict[str, str | None]] | None = None,
    selection: dict[str, object] | None = None,
    extra: dict[str, object] | None = None,
) -> None:
    skill_root = root / ".agents" / "skills" / name
    skill_root.mkdir(parents=True, exist_ok=True)
    (skill_root / "SKILL.md").write_text(
        f"""---
name: {name}
description: {name} reusable expertise.
---

# {name}

Instructions for {name}.
""",
        encoding="utf-8",
    )
    payload: dict[str, object] = {
        "version": 1,
        "capabilities": list(capabilities),
    }
    if compatible_agents:
        payload["compatible_agents"] = list(compatible_agents)
    if requires:
        payload["requires"] = list(requires)
    if compatibility is not None:
        payload["compatibility"] = compatibility
    if selection is not None:
        payload["selection"] = selection
    if extra:
        payload.update(extra)
    (skill_root / "sdai.yaml").write_text(
        yaml.safe_dump(payload, sort_keys=False),
        encoding="utf-8",
    )


def _java_pom(root: Path, version: str = "17") -> None:
    (root / "pom.xml").write_text(
        f"""<project>
  <properties><java.version>{version}</java.version></properties>
</project>
""",
        encoding="utf-8",
    )


def _policy(root: Path, required: dict[str, list[str]]) -> None:
    path = root / ".sdai" / "policy.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "providers": {},
                "capabilities": {},
                "execution": {},
                "skills": {"required": required},
                "architecture_validation": {},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def test_existing_versioned_skill_sidecar_remains_valid(tmp_path: Path) -> None:
    _init(tmp_path)
    _skill(tmp_path, "secure-coding", capabilities=("coding", "review", "security"))

    metadata = load_skill_metadata(tmp_path, "secure-coding")

    assert metadata.name == "secure-coding"
    assert [item.value for item in metadata.capabilities] == ["coding", "review", "security"]
    assert metadata.source == ".agents/skills/secure-coding/sdai.yaml"


def test_agent_declared_skill_expands_dependencies_dependency_first(tmp_path: Path) -> None:
    _init(tmp_path)
    _agent(tmp_path, skills=("spring-boot",))
    _skill(tmp_path, "java-engineering")
    _skill(tmp_path, "spring-boot", requires=("java-engineering",))
    _java_pom(tmp_path)

    report = resolve_skills(
        tmp_path,
        agent_name="developer",
        capability="coding",
    )

    assert report.selected == ("java-engineering", "spring-boot")
    spring = next(item for item in report.decisions if item.name == "spring-boot")
    java = next(item for item in report.decisions if item.name == "java-engineering")
    assert spring.origins == ("agent",)
    assert "dependency:spring-boot" in java.origins
    composed = compose_resolved_skills(tmp_path, report)
    assert composed.index("## Skill: java-engineering") < composed.index("## Skill: spring-boot")


def test_policy_required_skills_are_additive_to_agent_skills(tmp_path: Path) -> None:
    _init(tmp_path)
    _agent(tmp_path, skills=("implementation-planning",))
    _skill(tmp_path, "implementation-planning")
    _skill(tmp_path, "secure-coding")
    _policy(tmp_path, {"coding": ["secure-coding"]})

    report = resolve_skills(
        tmp_path,
        agent_name="developer",
        capability="coding",
    )

    assert report.selected == ("implementation-planning", "secure-coding")
    assert report.policy_required == ("secure-coding",)
    secure = next(item for item in report.decisions if item.name == "secure-coding")
    assert "policy" in secure.origins


def test_auto_selection_uses_role_capability_and_detected_technology(tmp_path: Path) -> None:
    _init(tmp_path)
    _agent(tmp_path)
    _skill(
        tmp_path,
        "java-engineering",
        compatible_agents=("developer",),
        compatibility={"languages": {"java": ">=17,<22"}},
        selection={"auto": True, "roles": ["developer"], "capabilities": ["coding"]},
    )
    _skill(
        tmp_path,
        "dotnet-engineering",
        compatible_agents=("developer",),
        compatibility={"languages": {"csharp": ">=12"}},
        selection={"auto": True, "roles": ["developer"]},
    )
    _java_pom(tmp_path, "17")

    report = resolve_skills(
        tmp_path,
        agent_name="developer",
        capability="coding",
    )

    assert report.selected == ("java-engineering",)
    rejected = next(item for item in report.decisions if item.name == "dotnet-engineering")
    assert rejected.selected is False
    assert "requires missing technology languages.csharp" in rejected.reasons[0]


def test_task_and_domain_filters_keep_auto_selection_minimal(tmp_path: Path) -> None:
    _init(tmp_path)
    _agent(tmp_path)
    _skill(
        tmp_path,
        "aws-kms-signing",
        compatibility={"platforms": {"aws": None}},
        selection={
            "auto": True,
            "task_keywords": ["kms", "signing"],
            "domains": ["code-signing"],
        },
    )
    tech = tmp_path / ".sdai" / "technology.yaml"
    tech.write_text(
        yaml.safe_dump({"version": 1, "platforms": {"aws": None}}, sort_keys=False),
        encoding="utf-8",
    )

    no_match = resolve_skills(
        tmp_path,
        agent_name="developer",
        capability="coding",
        task="implement file upload",
        domain="storage",
    )
    matched = resolve_skills(
        tmp_path,
        agent_name="developer",
        capability="coding",
        task="implement AWS KMS signing",
        domain="code-signing",
    )

    assert no_match.selected == ()
    assert matched.selected == ("aws-kms-signing",)


def test_incompatible_agent_or_policy_required_skill_fails_deterministically(tmp_path: Path) -> None:
    _init(tmp_path)
    _agent(tmp_path, skills=("java-21-only",))
    _skill(
        tmp_path,
        "java-21-only",
        compatibility={"languages": {"java": ">=21"}},
    )
    _java_pom(tmp_path, "17")

    with pytest.raises(
        SkillResolutionError,
        match="SDAI-SKILL-003.*java-21-only.*java version 17.*>=21",
    ):
        resolve_skills(tmp_path, agent_name="developer", capability="coding")


def test_weak_dependency_bound_is_not_treated_as_exact_version(tmp_path: Path) -> None:
    _init(tmp_path)
    _agent(tmp_path)
    _skill(
        tmp_path,
        "fastapi-engineering",
        compatibility={"frameworks": {"fastapi": ">=0.115"}},
        selection={"auto": True},
    )
    (tmp_path / "pyproject.toml").write_text(
        """[project]
name = "sample"
requires-python = ">=3.11"
dependencies = ["fastapi>=0.115"]
""",
        encoding="utf-8",
    )

    auto = resolve_skills(tmp_path, agent_name="developer", capability="coding")
    assert auto.selected == ()
    rejected = next(item for item in auto.decisions if item.name == "fastapi-engineering")
    assert "cannot prove version compatibility" in rejected.reasons[0]

    with pytest.raises(SkillResolutionError, match="SDAI-SKILL-003.*cannot prove"):
        resolve_skills(
            tmp_path,
            agent_name="developer",
            capability="coding",
            requested=("fastapi-engineering",),
        )


def test_explicit_exact_technology_pin_enables_versioned_skill(tmp_path: Path) -> None:
    _init(tmp_path)
    _agent(tmp_path)
    _skill(
        tmp_path,
        "fastapi-engineering",
        compatibility={"frameworks": {"fastapi": ">=0.115,<1"}},
        selection={"auto": True},
    )
    (tmp_path / "pyproject.toml").write_text(
        """[project]
name = "sample"
dependencies = ["fastapi>=0.115"]
""",
        encoding="utf-8",
    )
    (tmp_path / ".sdai" / "technology.yaml").write_text(
        yaml.safe_dump(
            {"version": 1, "frameworks": {"fastapi": "0.115"}},
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    report = resolve_skills(tmp_path, agent_name="developer", capability="coding")

    assert report.selected == ("fastapi-engineering",)


def test_compatible_agents_is_strict_for_direct_and_auto_selection(tmp_path: Path) -> None:
    _init(tmp_path)
    _agent(tmp_path, name="developer", skills=("security-review",))
    _skill(
        tmp_path,
        "security-review",
        compatible_agents=("security-reviewer",),
    )

    with pytest.raises(
        SkillResolutionError,
        match="SDAI-SKILL-002.*not compatible with semantic agent developer",
    ):
        resolve_skills(tmp_path, agent_name="developer", capability="coding")


def test_dependency_cycle_and_missing_dependency_fail_before_resolution(tmp_path: Path) -> None:
    _init(tmp_path)
    _agent(tmp_path)
    _skill(tmp_path, "a", requires=("b",))
    _skill(tmp_path, "b", requires=("a",))

    with pytest.raises(SkillResolutionError, match="SDAI-SKILL-005.*a -> b -> a"):
        resolve_skills(tmp_path, agent_name="developer", capability="coding")

    (tmp_path / ".agents" / "skills" / "b" / "sdai.yaml").write_text(
        yaml.safe_dump(
            {"version": 1, "capabilities": ["coding"], "requires": ["missing"]},
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    with pytest.raises(SkillResolutionError, match="SDAI-SKILL-006.*missing"):
        resolve_skills(tmp_path, agent_name="developer", capability="coding")


def test_unknown_skill_metadata_key_and_constraint_fail_closed(tmp_path: Path) -> None:
    _init(tmp_path)
    _agent(tmp_path)
    _skill(tmp_path, "bad", extra={"provider": "codex"})

    with pytest.raises(SkillResolutionError, match="SDAI-SKILL-001.*provider"):
        resolve_skills(tmp_path, agent_name="developer", capability="coding")

    _skill(
        tmp_path,
        "bad",
        compatibility={"languages": {"java": "^17"}},
    )
    with pytest.raises(SkillResolutionError, match="SDAI-SKILL-001.*unsupported constraint"):
        resolve_skills(tmp_path, agent_name="developer", capability="coding")
