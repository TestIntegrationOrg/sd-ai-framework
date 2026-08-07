from __future__ import annotations

import sys
from pathlib import Path

from sdai.agent_platform import AgentRuntime, Capability, ExecutionMode
from sdai.agent_platform.profiles import load_profiles, resolve_profile
from sdai.agent_platform.prompts import list_prompts, render_template
from sdai.agent_platform.skills import list_skills
from sdai.artifacts import write_text
from sdai.providers.cli import CliProvider
from sdai.providers.factory import ProviderFactory
from sdai.scaffold import init_project


def _feature(tmp_path: Path) -> None:
    write_text(
        tmp_path / "specs" / "AGENT-1" / "00-intake.md",
        """# Feature Intake — AGENT-1\n\n## Title\nAgent platform\n\n## Description\nSupport multiple coding agents.\n""",
    )


def test_scaffold_includes_multi_agent_profiles_skills_and_prompts(tmp_path: Path):
    init_project(tmp_path)

    profiles = load_profiles(tmp_path)
    assert {"codex", "copilot", "claude", "gemini"}.issubset(profiles)
    assert resolve_profile(tmp_path, Capability.ARCHITECTURE).name == "claude"
    assert resolve_profile(tmp_path, Capability.CODING).name == "codex"
    assert resolve_profile(tmp_path, Capability.REVIEW).name == "copilot"

    prompt_names = list_prompts(tmp_path)
    assert "architect.md" in prompt_names
    assert "developer.md" in prompt_names

    skill_names = {skill.name for skill in list_skills(tmp_path)}
    assert {"spec-traceability", "architecture-review", "secure-coding", "test-design"}.issubset(skill_names)


def test_runtime_composes_capability_prompt_and_only_applicable_skills(tmp_path: Path):
    init_project(tmp_path)
    _feature(tmp_path)

    invocation = AgentRuntime(tmp_path).build_invocation(
        "AGENT-1",
        Capability.ARCHITECTURE,
        mode=ExecutionMode.ADVISORY,
    )

    assert invocation.profile.name == "claude"
    assert "Architecture Task" in invocation.prompt
    assert "Skill: spec-traceability" in invocation.system
    assert "Skill: architecture-review" in invocation.system
    assert "Skill: secure-coding" not in invocation.system
    assert "Do not modify repository files" in invocation.system


def test_profile_override_allows_different_vendor_for_same_capability(tmp_path: Path):
    init_project(tmp_path)
    _feature(tmp_path)

    invocation = AgentRuntime(tmp_path).build_invocation(
        "AGENT-1",
        Capability.ARCHITECTURE,
        profile_name="codex",
    )
    assert invocation.profile.name == "codex"
    assert invocation.profile.provider == "codex"


def test_named_provider_factory_enforces_advisory_boundaries(tmp_path: Path):
    init_project(tmp_path)
    profiles = load_profiles(tmp_path)

    codex = ProviderFactory.create(profiles["codex"], mode=ExecutionMode.ADVISORY, cwd=tmp_path)
    assert codex.command[:2] == ["codex", "exec"]
    assert "read-only" in codex.command

    copilot = ProviderFactory.create(profiles["copilot"], mode=ExecutionMode.ADVISORY, cwd=tmp_path)
    assert copilot.command[0] == "copilot"
    assert "--no-ask-user" in copilot.command
    assert "--plan" in copilot.command
    assert "--available-tools=view,grep,glob" in copilot.command
    assert "--deny-tool=write" in copilot.command
    assert "--deny-tool=shell" in copilot.command
    assert not any(arg.startswith("--allow-tool=write") for arg in copilot.command)

    claude = ProviderFactory.create(profiles["claude"], mode=ExecutionMode.ADVISORY, cwd=tmp_path)
    assert "--permission-mode" in claude.command
    assert claude.command[claude.command.index("--permission-mode") + 1] == "plan"
    assert "--no-session-persistence" in claude.command

    gemini = ProviderFactory.create(profiles["gemini"], mode=ExecutionMode.ADVISORY, cwd=tmp_path)
    assert "--approval-mode" in gemini.command
    assert gemini.command[gemini.command.index("--approval-mode") + 1] == "plan"


def test_named_provider_factory_requires_explicit_workspace_write_mode(tmp_path: Path):
    init_project(tmp_path)
    profiles = load_profiles(tmp_path)

    copilot = ProviderFactory.create(
        profiles["copilot"], mode=ExecutionMode.WORKSPACE_WRITE, cwd=tmp_path
    )
    assert "--allow-tool=write" in copilot.command
    assert "--plan" not in copilot.command

    claude = ProviderFactory.create(
        profiles["claude"], mode=ExecutionMode.WORKSPACE_WRITE, cwd=tmp_path
    )
    assert claude.command[claude.command.index("--permission-mode") + 1] == "acceptEdits"

    gemini = ProviderFactory.create(
        profiles["gemini"], mode=ExecutionMode.WORKSPACE_WRITE, cwd=tmp_path
    )
    assert gemini.command[gemini.command.index("--approval-mode") + 1] == "auto_edit"


def test_cli_provider_can_execute_stdin_agent(tmp_path: Path):
    provider = CliProvider(
        [sys.executable, "-c", "import sys; print(sys.stdin.read())"],
        cwd=tmp_path,
        provider_name="test",
    )
    output = provider.complete(system="system rules", prompt="do work")
    assert "system rules" in output
    assert "do work" in output


def test_prompt_renderer_is_strict():
    assert render_template("Hello {{name}}", {"name": "SD-AI"}) == "Hello SD-AI"


def test_prompt_guard_blocks_private_key_material(tmp_path: Path):
    from sdai.agent_platform.guardrails import enforce_prompt_safety

    try:
        enforce_prompt_safety("system", "-----BEGIN PRIVATE KEY-----\nredacted")
    except RuntimeError as exc:
        assert "PRIVATE_KEY" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected sensitive-content guard to block invocation")


def test_upgrade_adds_agent_platform_without_overwriting_existing_project(tmp_path: Path):
    from sdai.scaffold import upgrade_project

    legacy = tmp_path / ".sdai"
    legacy.mkdir(parents=True)
    (legacy / "config.yaml").write_text("version: 1\ndefault_workflow: standard\n", encoding="utf-8")
    (legacy / "constitution.yaml").write_text("custom: true\n", encoding="utf-8")

    created = upgrade_project(tmp_path)
    assert created
    assert (legacy / "agents.yaml").exists()
    assert (legacy / "routing.yaml").exists()
    assert (legacy / "prompts" / "architect.md").exists()
    assert (legacy / "skills" / "architecture-review" / "SKILL.md").exists()
    assert (legacy / "constitution.yaml").read_text(encoding="utf-8") == "custom: true\n"
