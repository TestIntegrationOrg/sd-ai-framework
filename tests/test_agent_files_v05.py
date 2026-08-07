from pathlib import Path

import pytest

from sdai.agent_platform import AgentRuntime, Capability
from sdai.agent_platform.definitions import (
    AgentDefinitionError,
    list_agent_definitions,
    load_agent_definition,
    resolve_agent_definition,
)
from sdai.agent_platform.skills import list_skills, load_skill
from sdai.artifacts import write_text
from sdai.scaffold import init_project
from sdai.v05_scaffold import install_v05_scaffold


def _project(tmp_path: Path) -> None:
    init_project(tmp_path)
    install_v05_scaffold(tmp_path)
    write_text(
        tmp_path / "specs" / "V05-1" / "00-intake.md",
        "# Feature Intake — V05-1\n\n## Title\nSemantic agents\n\n## Description\nUse agent files and shared skills.\n",
    )


def test_v05_scaffold_installs_semantic_agents_and_shared_skills(tmp_path: Path):
    _project(tmp_path)

    definitions = {item.name for item in list_agent_definitions(tmp_path)}
    assert {
        "requirements-analyst",
        "architect",
        "planner",
        "developer",
        "code-reviewer",
        "tester",
        "security-reviewer",
        "documentation-writer",
    }.issubset(definitions)

    skills = {item.name for item in list_skills(tmp_path)}
    assert {
        "requirements-analysis",
        "architecture-design",
        "architecture-review",
        "implementation-planning",
        "spec-traceability",
        "secure-coding",
        "test-design",
        "documentation-quality",
    }.issubset(skills)

    assert load_skill(tmp_path, "spec-traceability").root == (
        tmp_path / ".agents" / "skills" / "spec-traceability"
    )


def test_runtime_routes_through_semantic_agent_and_merges_skills(tmp_path: Path):
    _project(tmp_path)

    definition = resolve_agent_definition(tmp_path, Capability.ARCHITECTURE)
    assert definition is not None
    assert definition.name == "architect"

    invocation = AgentRuntime(tmp_path).build_invocation("V05-1", Capability.ARCHITECTURE)
    assert invocation.agent_name == "architect"
    assert invocation.profile.name == "claude"
    assert "Semantic agent: architect" in invocation.system
    assert "Generate viable alternatives" in invocation.system or "generate viable alternatives" in invocation.system
    assert "Skill: architecture-design" in invocation.system
    assert "Skill: architecture-review" in invocation.system


def test_explicit_provider_profile_override_does_not_replace_semantic_role(tmp_path: Path):
    _project(tmp_path)

    invocation = AgentRuntime(tmp_path).build_invocation(
        "V05-1",
        Capability.ARCHITECTURE,
        agent_name="architect",
        profile_name="codex",
    )
    assert invocation.agent_name == "architect"
    assert invocation.profile.name == "codex"
    assert "Semantic agent: architect" in invocation.system


def test_agent_definition_rejects_credential_like_provider_configuration(tmp_path: Path):
    _project(tmp_path)
    path = tmp_path / ".sdai" / "agents" / "bad-agent.agent.md"
    write_text(
        path,
        """---
name: bad-agent
description: invalid test agent
capabilities: [architecture]
providers:
  codex:
    api_token: do-not-store-this
---
# Bad Agent

Do work.
""",
    )
    with pytest.raises(AgentDefinitionError):
        load_agent_definition(tmp_path, "bad-agent")
