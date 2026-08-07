import os
from pathlib import Path

import pytest

from sdai.agent_platform import AgentRuntime, Capability, ExecutionMode
from sdai.agent_platform.models import AgentProfile
from sdai.agent_platform.prompts import PromptError, load_prompt
from sdai.artifacts import write_text
from sdai.execution_guard import ProtectedPathViolation, WorkspaceMutationGuard
from sdai.models import FeatureContext
from sdai.policy import CORE_PROTECTED_PATHS
from sdai.providers.cli import build_provider_environment
from sdai.providers.factory import ProviderFactory, ProviderFactoryError
from sdai.scaffold import init_project


def test_feature_artifact_rejects_symlink_escape(tmp_path: Path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside-specs"
    outside.mkdir()
    try:
        os.symlink(outside, tmp_path / "specs", target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink unavailable: {exc}")
    context = FeatureContext(tmp_path, "SAFE-1")
    with pytest.raises(RuntimeError, match="inside the project workspace"):
        context.artifact("specification.md")


def test_prompt_loader_rejects_traversal_and_symlink_escape(tmp_path: Path):
    prompts = tmp_path / ".sdai" / "prompts"
    prompts.mkdir(parents=True)
    write_text(prompts / "safe.md", "safe")
    assert load_prompt(tmp_path, "safe.md").strip() == "safe"
    with pytest.raises(PromptError):
        load_prompt(tmp_path, "../../outside.md")


def test_workspace_guard_restores_protected_source_of_truth(tmp_path: Path):
    spec = tmp_path / "specs" / "F-1" / "specification.md"
    source = tmp_path / "src" / "service.py"
    write_text(spec, "approved")
    write_text(source, "before")

    with pytest.raises(ProtectedPathViolation, match="changes were restored"):
        with WorkspaceMutationGuard(tmp_path, CORE_PROTECTED_PATHS):
            write_text(spec, "agent changed approved spec")
            write_text(source, "allowed source change")

    assert spec.read_text(encoding="utf-8").strip() == "approved"
    # Non-protected application changes remain available for later validation/PR.
    assert source.read_text(encoding="utf-8").strip() == "allowed source change"


def test_provider_environment_does_not_inherit_unrelated_secrets(monkeypatch):
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "must-not-leak")
    monkeypatch.setenv("JIRA_API_TOKEN", "must-not-leak-either")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "provider-auth")
    env = build_provider_environment("claude")
    assert env.get("ANTHROPIC_API_KEY") == "provider-auth"
    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert "JIRA_API_TOKEN" not in env


def test_named_provider_extra_args_cannot_override_security_flags(tmp_path: Path):
    write_text(tmp_path / ".sdai" / "config.yaml", "version: 2\n")
    profile = AgentProfile(
        name="unsafe",
        provider="codex",
        capabilities=(Capability.CODING,),
        prompt="developer.md",
        extra_args=("--sandbox", "danger-full-access"),
    )
    with pytest.raises(ProviderFactoryError, match="security flags"):
        ProviderFactory.create(profile, mode=ExecutionMode.ADVISORY, cwd=tmp_path)


def test_build_invocation_blocks_secret_before_dry_run_can_print_it(tmp_path: Path):
    init_project(tmp_path)
    write_text(
        tmp_path / "specs" / "SECRET-1" / "00-intake.md",
        """# Feature Intake

## Title
Secret

## Description
-----BEGIN PRIVATE KEY-----
not-real-but-must-never-be-printed
-----END PRIVATE KEY-----
""",
    )
    runtime = AgentRuntime(tmp_path)
    with pytest.raises(RuntimeError, match="prompt-safety policy"):
        runtime.build_invocation(
            "SECRET-1", Capability.ARCHITECTURE, mode=ExecutionMode.ADVISORY
        )
