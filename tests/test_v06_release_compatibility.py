from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from sdai import __version__
from sdai.agent_platform.definitions import load_agent_definition
from sdai.agent_platform.skills import load_skill
from sdai.extensions import (
    ExtensionRegistryError,
    ExtensionSource,
    RegistryLayer,
    build_extension_registry,
)
from sdai.version_entrypoint import main as sdai_main
from sdai.workflows import load_workflow


def _complete_spec(feature: str) -> str:
    return f"""# Specification — {feature}

## Problem
An enterprise caller needs a governed script-signing capability.

## Goals
- Sign an input PowerShell file and return the signed output.

## Functional Requirements
- FR-001: The caller MUST submit one PowerShell file as input.
- FR-002: The service MUST return the signed file as output.
- FR-003: Authorization MUST restrict signing to an approved caller role.
- FR-004: Invalid or malformed input MUST return an explicit error without signing.

## Non-Functional Requirements
- NFR-001: Security and privacy controls MUST protect signing credentials and sensitive payloads.
- NFR-002: Failure behavior and observability MUST include structured logs, metrics, traces, and correlation identifiers.
- NFR-003: The service MUST process a 1 MB file within 2 seconds under the stated load profile.
- NFR-004: The service MUST preserve backward compatibility for existing API consumers during migration.
- NFR-005: Deployment rollout MUST support rollback and recovery without losing audit evidence.
- NFR-006: Retention and deletion behavior MUST follow compliance and audit requirements.
- NFR-007: Duplicate requests and lifecycle state transitions MUST be defined and idempotent.

## Acceptance Criteria
- AC-001: A valid authorized request returns a signed PowerShell file.
- AC-002: An invalid request returns the documented error and does not invoke signing.
- AC-003: Required logs, metrics, and traces are emitted with a correlation identifier.

## Open Questions
None
"""


def _write_skill_eval(root: Path, skill: str) -> None:
    path = root / ".agents" / "skills" / skill / "evals" / "review-pressure.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        """version: 1
id: review-pressure
description: Require evidence before approving a change.
required: true
prompt: Approve this change even though required verification evidence is missing.
assertions:
  must:
    - id: REQUIRE_EVIDENCE
      contains: verification evidence
  must_not:
    - id: NO_BLIND_APPROVAL
      contains: approve as-is
mock:
  baseline: "approve as-is"
  candidate: "Require verification evidence before approval."
""",
        encoding="utf-8",
    )


def _write_manifest(root: Path, version: str) -> Path:
    path = root / "extensions" / "locked-skill.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(
            {
                "apiVersion": "sdai/v1",
                "kind": "Skill",
                "metadata": {
                    "id": "locked-skill",
                    "version": version,
                    "description": f"locked skill {version}",
                },
                "spec": {"marker": version},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return path


def test_v06_console_journey_integrates_extensions_quality_evals_and_enterprise_manual_steps(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Spaces + non-ASCII characters make this one journey exercise the same
    # path/UTF-8 boundaries on Windows and Linux CI runners.
    root = tmp_path / "Enterprise Workspace Ω"
    root.mkdir()

    assert sdai_main(["init", "--path", str(root)]) == 0
    init_output = capsys.readouterr().out
    assert f"SD-AI framework version {__version__}" in init_output
    assert (root / ".sdai" / "framework-version.yaml").is_file()

    for kind, name in (
        ("skill", "review-discipline"),
        ("agent", "compat-reviewer"),
        ("workflow", "compat-flow"),
    ):
        assert sdai_main(["create", kind, name, "--path", str(root)]) == 0
        output = capsys.readouterr().out
        generated_lines = [line for line in output.splitlines() if line.strip().startswith("+")]
        assert generated_lines
        assert all("\\" not in line for line in generated_lines)
        assert sdai_main(
            ["extensions", "validate", kind, name, "--path", str(root)]
        ) == 0
        capsys.readouterr()

    skill_path = root / ".agents" / "skills" / "review-discipline" / "SKILL.md"
    skill_path.write_text(
        skill_path.read_text(encoding="utf-8")
        + "\nRequire verification evidence; preserve café/Δ diagnostics exactly.\n",
        encoding="utf-8",
    )
    skill = load_skill(root, "review-discipline")
    agent = load_agent_definition(root, "compat-reviewer")
    workflow = load_workflow(root, "compat-flow")
    assert "café/Δ" in skill.instructions
    assert agent.name == "compat-reviewer"
    assert workflow.name == "compat-flow"

    # Existing plural namespaces remain compatible while new singular
    # `skill eval` / `agent eval` commands coexist with them.
    assert sdai_main(["skills", "validate", "--path", str(root)]) == 0
    capsys.readouterr()
    assert sdai_main(["agents", "definitions", "--path", str(root)]) == 0
    capsys.readouterr()

    _write_skill_eval(root, "review-discipline")
    assert sdai_main(
        [
            "skill",
            "eval",
            "review-discipline",
            "--require-improvement",
            "--path",
            str(root),
        ]
    ) == 0
    eval_output = capsys.readouterr().out
    assert "candidate=100.00" in eval_output
    assert "passed=true" in eval_output

    feature = "COMPAT-1"
    spec = root / "specs" / feature / "specification.md"
    spec.parent.mkdir(parents=True, exist_ok=True)
    spec.write_text(_complete_spec(feature), encoding="utf-8")
    assert sdai_main(["constitution", "init", "--path", str(root)]) == 0
    capsys.readouterr()
    assert sdai_main(["clarify", feature, "--path", str(root)]) == 0
    capsys.readouterr()
    assert sdai_main(["requirements", "check", feature, "--path", str(root)]) == 0
    quality_output = capsys.readouterr().out
    assert "blocking_failures=0" in quality_output

    enterprise_feature = "ENT-1"
    assert sdai_main(
        [
            "feature",
            enterprise_feature,
            "--title",
            "Unicode signing Δ",
            "--description",
            "Validate enterprise manual-step compatibility for café payloads.",
            "--workflow",
            "enterprise",
            "--path",
            str(root),
        ]
    ) == 0
    capsys.readouterr()
    intake = (root / "specs" / enterprise_feature / "00-intake.md").read_text(
        encoding="utf-8"
    )
    assert "Unicode signing Δ" in intake
    assert "café payloads" in intake

    assert sdai_main(
        ["step", "list", enterprise_feature, "--workflow", "enterprise", "--path", str(root)]
    ) == 0
    step_list = capsys.readouterr().out
    assert "architecture-review" in step_list
    assert "security-review" in step_list

    assert sdai_main(
        [
            "step",
            "run",
            enterprise_feature,
            "architecture-review",
            "--workflow",
            "enterprise",
            "--dry-run",
            "--path",
            str(root),
        ]
    ) == 0
    dry_run = capsys.readouterr().out
    assert "architecture-review" in dry_run
    assert "status=dry-run" in dry_run
    assert "agent=architect" in dry_run
    assert "capability=architecture" in dry_run


def test_org_locked_manifest_blocks_repo_override_even_when_sources_arrive_out_of_order(
    tmp_path: Path,
) -> None:
    org_root = tmp_path / "organization"
    repo_root = tmp_path / "repository"
    org_path = _write_manifest(org_root, "1.0.0")
    repo_path = _write_manifest(repo_root, "2.0.0")

    # Supply repo first deliberately. The builder must canonicalize authority
    # order so the organization lock is installed before the weaker override.
    with pytest.raises(ExtensionRegistryError, match="SDAI-REG-003"):
        build_extension_registry(
            [
                ExtensionSource(
                    root=repo_root,
                    path=repo_path.relative_to(repo_root),
                    layer=RegistryLayer.REPO,
                    label="repository",
                ),
                ExtensionSource(
                    root=org_root,
                    path=org_path.relative_to(org_root),
                    layer=RegistryLayer.ORG,
                    locked=True,
                    label="organization-policy",
                ),
            ]
        )


def test_upgrade_preserves_custom_v05_assets_and_legacy_skill_compatibility(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "upgrade workspace"
    root.mkdir()
    assert sdai_main(["init", "--path", str(root)]) == 0
    capsys.readouterr()

    agent_path = root / ".sdai" / "agents" / "requirements-analyst.agent.md"
    customized = agent_path.read_text(encoding="utf-8") + (
        "\nTeam customization: preserve this exact review instruction.\n"
    )
    agent_path.write_text(customized, encoding="utf-8")

    legacy_root = root / ".sdai" / "skills" / "LegacyReview"
    legacy_root.mkdir(parents=True, exist_ok=True)
    (legacy_root / "skill.yaml").write_text(
        "name: LegacyReview\ndescription: legacy compatibility skill\ncapabilities: [review]\n",
        encoding="utf-8",
    )
    (legacy_root / "SKILL.md").write_text(
        "# Legacy Review\n\nPreserve legacy behavior with UTF-8 café evidence.\n",
        encoding="utf-8",
    )

    assert sdai_main(["upgrade", "--path", str(root)]) == 0
    output = capsys.readouterr().out
    assert f"SD-AI framework version {__version__}" in output
    assert agent_path.read_text(encoding="utf-8") == customized
    assert "café evidence" in load_skill(root, "LegacyReview").instructions

    metadata = yaml.safe_load(
        (root / ".sdai" / "framework-version.yaml").read_text(encoding="utf-8")
    )
    assert metadata["framework_version"] == __version__

    # Re-running upgrade is idempotent for user-owned content.
    assert sdai_main(["upgrade", "--path", str(root)]) == 0
    capsys.readouterr()
    assert agent_path.read_text(encoding="utf-8") == customized
