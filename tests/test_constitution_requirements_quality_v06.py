from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from sdai.constitution import (
    ConstitutionError,
    check_constitution,
    init_constitution,
    load_constitution,
    write_constitution_evidence,
)
from sdai.entrypoint import main as entrypoint_main
from sdai.models import FeatureContext
from sdai.requirements_quality import (
    CLARIFICATION_CATEGORIES,
    analyze_clarifications,
    check_requirements,
    write_clarifications,
    write_requirements_checklist,
)


def _initialized(root: Path) -> None:
    config = root / ".sdai" / "config.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("version: 1\n", encoding="utf-8")


def _write_spec(root: Path, feature: str, text: str) -> Path:
    path = FeatureContext(root, feature).artifact("specification.md")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _complete_spec(feature: str = "SIGN-123") -> str:
    return f"""# Specification — {feature}

## Problem
A caller needs a secure script-signing capability.

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


def _incomplete_spec(feature: str = "SIGN-123") -> str:
    return f"""# Specification — {feature}

## Problem
Build signing.

## Goals
- Sign things.

## Functional Requirements
- FR-001: Sign the input.

## Non-Functional Requirements
- NFR-001: Security MUST be considered.

## Acceptance Criteria
- AC-001: It works.

## Open Questions
- Performance?
- Failure behavior?
"""


def test_default_constitution_initializes_valid_machine_readable_principles(
    tmp_path: Path,
) -> None:
    path = init_constitution(tmp_path)
    constitution = load_constitution(tmp_path)

    assert path == (tmp_path / ".sdai" / "constitution.md").resolve()
    assert constitution.version == 1
    assert [principle.id for principle in constitution.principles] == [
        "CON-001",
        "CON-002",
        "CON-003",
        "CON-004",
        "CON-005",
    ]
    assert len(constitution.sha256) == 64
    assert "Engineering Constitution" in constitution.body


def test_constitution_hash_changes_when_governing_text_changes(tmp_path: Path) -> None:
    path = init_constitution(tmp_path)
    before = load_constitution(tmp_path).sha256
    path.write_text(path.read_text(encoding="utf-8") + "\nAdditional rule.\n", encoding="utf-8")

    after = load_constitution(tmp_path).sha256

    assert after != before


def test_constitution_rejects_duplicate_principle_ids(tmp_path: Path) -> None:
    path = init_constitution(tmp_path)
    text = path.read_text(encoding="utf-8")
    text = text.replace("- id: CON-002", "- id: CON-001", 1)
    path.write_text(text, encoding="utf-8")

    with pytest.raises(ConstitutionError, match="Duplicate constitution principle id"):
        load_constitution(tmp_path)


def test_constitution_never_auto_passes_principle_without_deterministic_rule(
    tmp_path: Path,
) -> None:
    init_constitution(tmp_path)
    _write_spec(tmp_path, "SIGN-123", _complete_spec())

    findings = check_constitution(tmp_path, "SIGN-123")
    by_id = {finding.principle_id: finding for finding in findings}

    assert by_id["CON-001"].status == "pass"
    assert by_id["CON-002"].status == "pass"
    assert by_id["CON-003"].status == "pass"
    assert by_id["CON-004"].status == "pass"
    assert by_id["CON-005"].status == "review"
    assert by_id["CON-005"].evidence == ()


def test_constitution_evidence_binds_hash_and_forbids_implementation_self_approval(
    tmp_path: Path,
) -> None:
    init_constitution(tmp_path)
    _write_spec(tmp_path, "SIGN-123", _complete_spec())

    path = write_constitution_evidence(tmp_path, "SIGN-123")
    evidence = yaml.safe_load(path.read_text(encoding="utf-8"))

    assert evidence["constitution_sha256"] == load_constitution(tmp_path).sha256
    assert evidence["review_owner"] == "requirements-analyst"
    assert evidence["approval_status"] == "pending"
    assert evidence["implementation_self_approval"] == "forbidden"
    assert next(
        item for item in evidence["findings"] if item["principle_id"] == "CON-005"
    )["status"] == "review"


def test_clarification_generates_all_categories_without_rewriting_spec(tmp_path: Path) -> None:
    spec_path = _write_spec(tmp_path, "SIGN-123", _incomplete_spec())
    original = spec_path.read_text(encoding="utf-8")

    findings = analyze_clarifications(tmp_path, "SIGN-123")
    path = write_clarifications(tmp_path, "SIGN-123")
    output = path.read_text(encoding="utf-8")

    assert len(findings) == len(CLARIFICATION_CATEGORIES) == 14
    assert {finding.id for finding in findings} == {
        f"CLAR-{index:03d}" for index in range(1, 15)
    }
    assert any(finding.status == "open" for finding in findings)
    assert "review_owner: requirements-analyst" in output
    assert "approval_status: pending" in output
    assert "implementation_self_approval: forbidden" in output
    assert "candidate-covered" in output
    assert spec_path.read_text(encoding="utf-8") == original


def test_incomplete_requirements_fail_blocking_quality_checks(tmp_path: Path) -> None:
    _write_spec(tmp_path, "SIGN-123", _incomplete_spec())

    report = check_requirements(tmp_path, "SIGN-123")
    by_id = {finding.id: finding for finding in report.findings}

    assert [finding.id for finding in report.findings] == [
        f"RQ-{index:03d}" for index in range(1, 15)
    ]
    assert report.blocking_failures
    assert by_id["RQ-004"].status == "fail"
    assert by_id["RQ-007"].status == "fail"
    assert by_id["RQ-008"].status == "fail"
    assert by_id["RQ-010"].status == "fail"


def test_complete_requirements_clear_all_blocking_quality_checks(tmp_path: Path) -> None:
    _write_spec(tmp_path, "SIGN-123", _complete_spec())

    report = check_requirements(tmp_path, "SIGN-123")

    assert report.blocking_failures == ()
    assert len(report.specification_sha256) == 64


def test_requirements_checklist_is_reviewer_owned_and_hash_bound(tmp_path: Path) -> None:
    _write_spec(tmp_path, "SIGN-123", _complete_spec())

    path, report = write_requirements_checklist(tmp_path, "SIGN-123")
    output = path.read_text(encoding="utf-8")

    assert f"specification_sha256: {report.specification_sha256}" in output
    assert "review_owner: requirements-analyst" in output
    assert "approval_status: pending" in output
    assert "implementation_self_approval: forbidden" in output
    assert "| RQ-001 |" in output
    assert "Decision: pending" in output


def test_cli_constitution_lifecycle_and_quality_commands(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _initialized(tmp_path)
    _write_spec(tmp_path, "SIGN-123", _complete_spec())

    assert entrypoint_main(["constitution", "init", "--path", str(tmp_path)]) == 0
    init_output = capsys.readouterr().out
    assert "sha256=" in init_output

    assert entrypoint_main(["constitution", "validate", "--path", str(tmp_path)]) == 0
    assert "principles=5" in capsys.readouterr().out

    assert entrypoint_main(
        ["constitution", "check", "SIGN-123", "--path", str(tmp_path)]
    ) == 0
    constitution_output = capsys.readouterr().out
    assert "blocking_failures=0" in constitution_output
    assert "review_required=1" in constitution_output

    assert entrypoint_main(["clarify", "SIGN-123", "--path", str(tmp_path)]) == 0
    clarify_output = capsys.readouterr().out
    assert "total=14" in clarify_output

    assert entrypoint_main(
        ["requirements", "check", "SIGN-123", "--path", str(tmp_path)]
    ) == 0
    requirements_output = capsys.readouterr().out
    assert "blocking_failures=0" in requirements_output

    assert (
        tmp_path / "specs" / "SIGN-123" / "quality" / "constitution-check.yaml"
    ).exists()
    assert (
        tmp_path / "specs" / "SIGN-123" / "quality" / "clarifications.md"
    ).exists()
    assert (
        tmp_path / "specs" / "SIGN-123" / "quality" / "requirements-checklist.md"
    ).exists()


def test_cli_requirements_check_returns_nonzero_for_blocking_findings(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _initialized(tmp_path)
    _write_spec(tmp_path, "SIGN-123", _incomplete_spec())

    assert entrypoint_main(
        ["requirements", "check", "SIGN-123", "--path", str(tmp_path)]
    ) == 1
    output = capsys.readouterr().out
    assert "blocking_failures=" in output
    assert "blocking_failures=0" not in output


def test_cli_constitution_check_returns_nonzero_for_blocking_principle_failure(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _initialized(tmp_path)
    init_constitution(tmp_path)
    spec = _complete_spec().replace("Security and privacy", "Safety considerations").replace(
        "signing credentials", "runtime credentials"
    )
    _write_spec(tmp_path, "SIGN-123", spec)

    assert entrypoint_main(
        ["constitution", "check", "SIGN-123", "--path", str(tmp_path)]
    ) == 1
    output = capsys.readouterr().out
    assert "blocking_failures=1" in output


def test_constitution_init_requires_force_to_replace_existing_file(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _initialized(tmp_path)
    assert entrypoint_main(["constitution", "init", "--path", str(tmp_path)]) == 0
    capsys.readouterr()

    path = tmp_path / ".sdai" / "constitution.md"
    path.write_text("custom\n", encoding="utf-8")
    assert entrypoint_main(["constitution", "init", "--path", str(tmp_path)]) == 1
    assert path.read_text(encoding="utf-8") == "custom\n"
    assert "already exists" in capsys.readouterr().err

    assert entrypoint_main(
        ["constitution", "init", "--force", "--path", str(tmp_path)]
    ) == 0
    assert path.read_text(encoding="utf-8").startswith("---\n")
