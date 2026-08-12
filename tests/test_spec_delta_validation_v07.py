from __future__ import annotations

import json
from pathlib import Path

import yaml

from sdai.spec_changes import load_current_spec
from sdai.spec_validation import (
    detect_parallel_change_conflicts,
    parse_current_requirements,
    requirement_sha256,
    validate_spec_change,
)


def _current_text(*requirements: tuple[str, str]) -> str:
    lines = [
        "# Signing",
        "",
        "## Goals",
        "- Goal: This colon-bearing prose bullet is not a requirement.",
        "",
        "## Functional Requirements",
    ]
    lines.extend(f"- {requirement_id}: {definition}" for requirement_id, definition in requirements)
    lines.extend(
        [
            "",
            "## Acceptance Criteria",
            "- AC-001: Valid requests satisfy the current signing behavior.",
            "",
        ]
    )
    return "\n".join(lines)


def _write_current(
    root: Path,
    domain: str = "signing",
    requirements: tuple[tuple[str, str], ...] = (
        ("FR-001", "The service MUST sign a PowerShell file."),
        ("FR-002", "The service MUST reject malformed input."),
    ),
) -> Path:
    path = root / "specs" / "current" / domain / "specification.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_current_text(*requirements), encoding="utf-8")
    return path


def _write_change(
    root: Path,
    feature: str,
    *,
    domain: str = "signing",
    baseline: str | None,
    operations: list[dict[str, object]],
) -> None:
    change_root = root / "specs" / "changes" / feature
    delta_root = change_root / "deltas"
    delta_root.mkdir(parents=True, exist_ok=True)
    (change_root / "change.yaml").write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "feature_id": feature,
                "title": f"Change {feature}",
                "description": "Validation fixture.",
                "status": "draft",
                "domains": [domain],
                "baselines": {domain: baseline},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (delta_root / f"{domain}.yaml").write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "domain": domain,
                "baseline_spec_sha256": baseline,
                "operations": operations,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _requirement_hash(root: Path, requirement_id: str, domain: str = "signing") -> str:
    index = parse_current_requirements(load_current_spec(root, domain)).by_id()
    return index[requirement_id].sha256


def test_requirement_index_uses_only_recognized_sections_and_stable_hashes(
    tmp_path: Path,
) -> None:
    _write_current(tmp_path)
    current = load_current_spec(tmp_path, "signing")

    index = parse_current_requirements(current)

    assert tuple(item.requirement_id for item in index.requirements) == (
        "FR-001",
        "FR-002",
        "AC-001",
    )
    assert "Goal" not in index.by_id()
    assert index.by_id()["FR-001"].sha256 == requirement_sha256(
        "FR-001",
        "The service MUST sign a PowerShell file.",
    )
    assert index.duplicate_ids == ()
    assert index.requirement_sections == (
        "Functional Requirements",
        "Acceptance Criteria",
    )


def test_valid_modify_add_remove_and_rename_change_has_no_findings(tmp_path: Path) -> None:
    _write_current(
        tmp_path,
        requirements=(
            ("FR-001", "The service MUST sign a PowerShell file."),
            ("FR-002", "The service MUST reject malformed input."),
            ("FR-003", "The service MUST emit an audit record."),
        ),
    )
    current = load_current_spec(tmp_path, "signing")
    _write_change(
        tmp_path,
        "SIGN-100",
        baseline=current.sha256,
        operations=[
            {
                "op": "MODIFIED",
                "requirement_id": "FR-001",
                "previous_hash": _requirement_hash(tmp_path, "FR-001"),
                "definition": "The service MUST sign a PowerShell file with a trusted key.",
                "reason": "Clarify key trust.",
            },
            {
                "op": "REMOVED",
                "requirement_id": "FR-002",
                "previous_hash": _requirement_hash(tmp_path, "FR-002"),
                "reason": "Move validation to another domain.",
            },
            {
                "op": "RENAMED",
                "requirement_id": "FR-003",
                "new_requirement_id": "AUDIT-001",
                "previous_hash": _requirement_hash(tmp_path, "FR-003"),
                "reason": "Adopt domain-specific identity.",
            },
            {
                "op": "ADDED",
                "requirement_id": "FR-004",
                "definition": "The service MUST return the signed file.",
                "reason": "Define output behavior.",
            },
        ],
    )

    before = (tmp_path / "specs" / "current" / "signing" / "specification.md").read_bytes()
    report = validate_spec_change(tmp_path, "SIGN-100")
    after = (tmp_path / "specs" / "current" / "signing" / "specification.md").read_bytes()

    assert report.valid is True
    assert report.findings == ()
    assert report.current_spec_sha256 == {"signing": current.sha256}
    assert before == after
    assert json.loads(report.to_json())["valid"] is True


def test_stale_spec_and_requirement_baselines_are_reported_deterministically(
    tmp_path: Path,
) -> None:
    _write_current(tmp_path)
    original = load_current_spec(tmp_path, "signing")
    original_req_hash = _requirement_hash(tmp_path, "FR-001")
    _write_change(
        tmp_path,
        "SIGN-101",
        baseline=original.sha256,
        operations=[
            {
                "op": "MODIFIED",
                "requirement_id": "FR-001",
                "previous_hash": original_req_hash,
                "definition": "Updated signing requirement.",
                "reason": "Change behavior.",
            }
        ],
    )

    _write_current(
        tmp_path,
        requirements=(
            ("FR-001", "The service MUST sign using a newly approved policy."),
            ("FR-002", "The service MUST reject malformed input."),
        ),
    )

    report = validate_spec_change(tmp_path, "SIGN-101")
    kinds = [finding.kind for finding in report.findings]

    assert report.valid is False
    assert "STALE_SPEC_BASELINE" in kinds
    assert "STALE_REQUIREMENT_BASELINE" in kinds
    stale_requirement = next(
        finding for finding in report.findings if finding.kind == "STALE_REQUIREMENT_BASELINE"
    )
    assert stale_requirement.expected == original_req_hash
    assert stale_requirement.actual == _requirement_hash(tmp_path, "FR-001")


def test_add_existing_missing_target_and_rename_destination_conflicts(tmp_path: Path) -> None:
    _write_current(tmp_path)
    current = load_current_spec(tmp_path, "signing")
    _write_change(
        tmp_path,
        "SIGN-102",
        baseline=current.sha256,
        operations=[
            {
                "op": "ADDED",
                "requirement_id": "FR-001",
                "definition": "Duplicate definition.",
                "reason": "Should fail.",
            },
            {
                "op": "MODIFIED",
                "requirement_id": "FR-999",
                "previous_hash": "sha256:" + "f" * 64,
                "definition": "Missing target.",
                "reason": "Should fail.",
            },
            {
                "op": "RENAMED",
                "requirement_id": "FR-002",
                "new_requirement_id": "AC-001",
                "previous_hash": _requirement_hash(tmp_path, "FR-002"),
                "reason": "Destination already exists.",
            },
        ],
    )

    report = validate_spec_change(tmp_path, "SIGN-102")
    kinds = {finding.kind for finding in report.findings}

    assert report.valid is False
    assert kinds == {
        "ADDED_REQUIREMENT_EXISTS",
        "TARGET_REQUIREMENT_MISSING",
        "RENAME_DESTINATION_EXISTS",
    }


def test_missing_current_spec_and_new_domain_semantics_are_fail_closed(tmp_path: Path) -> None:
    baseline = "sha256:" + "a" * 64
    _write_change(
        tmp_path,
        "SIGN-103",
        baseline=baseline,
        operations=[
            {
                "op": "MODIFIED",
                "requirement_id": "FR-001",
                "previous_hash": "sha256:" + "b" * 64,
                "definition": "Missing current truth.",
                "reason": "Should fail.",
            }
        ],
    )
    missing = validate_spec_change(tmp_path, "SIGN-103")
    assert [finding.kind for finding in missing.findings] == ["CURRENT_SPEC_MISSING"]

    _write_change(
        tmp_path,
        "SIGN-104",
        domain="new-signing",
        baseline=None,
        operations=[
            {
                "op": "MODIFIED",
                "requirement_id": "FR-001",
                "previous_hash": "sha256:" + "c" * 64,
                "definition": "Cannot modify absent domain.",
                "reason": "Should fail.",
            }
        ],
    )
    new_domain = validate_spec_change(tmp_path, "SIGN-104")
    assert [finding.kind for finding in new_domain.findings] == [
        "TARGET_REQUIREMENT_MISSING"
    ]


def test_new_domain_add_is_valid_until_current_truth_appears(tmp_path: Path) -> None:
    _write_change(
        tmp_path,
        "SIGN-105",
        domain="new-signing",
        baseline=None,
        operations=[
            {
                "op": "ADDED",
                "requirement_id": "FR-001",
                "definition": "First domain requirement.",
                "reason": "Create domain.",
            }
        ],
    )

    first = validate_spec_change(tmp_path, "SIGN-105")
    assert first.valid is True

    _write_current(
        tmp_path,
        domain="new-signing",
        requirements=(("FR-001", "Another change created this requirement."),),
    )
    second = validate_spec_change(tmp_path, "SIGN-105")
    kinds = {finding.kind for finding in second.findings}
    assert second.valid is False
    assert "UNEXPECTED_CURRENT_SPEC" in kinds
    assert "ADDED_REQUIREMENT_EXISTS" in kinds


def test_duplicate_or_unstructured_current_truth_is_blocking(tmp_path: Path) -> None:
    _write_current(
        tmp_path,
        requirements=(
            ("FR-001", "First definition."),
            ("FR-001", "Duplicate definition."),
        ),
    )
    current = load_current_spec(tmp_path, "signing")
    _write_change(
        tmp_path,
        "SIGN-106",
        baseline=current.sha256,
        operations=[
            {
                "op": "ADDED",
                "requirement_id": "FR-002",
                "definition": "Another requirement.",
                "reason": "Validation fixture.",
            }
        ],
    )
    duplicate = validate_spec_change(tmp_path, "SIGN-106")
    assert "DUPLICATE_CURRENT_REQUIREMENT" in {
        finding.kind for finding in duplicate.findings
    }

    path = tmp_path / "specs" / "current" / "signing" / "specification.md"
    path.write_text("# Signing\n\n## Goals\n- Only prose goals exist.\n", encoding="utf-8")
    latest = load_current_spec(tmp_path, "signing")
    _write_change(
        tmp_path,
        "SIGN-107",
        baseline=latest.sha256,
        operations=[
            {
                "op": "ADDED",
                "requirement_id": "FR-002",
                "definition": "Requirement cannot be deterministically placed yet.",
                "reason": "Validation fixture.",
            }
        ],
    )
    unstructured = validate_spec_change(tmp_path, "SIGN-107")
    assert [finding.kind for finding in unstructured.findings] == [
        "NO_STRUCTURED_REQUIREMENTS"
    ]


def test_parallel_same_requirement_changes_conflict_but_disjoint_changes_do_not(
    tmp_path: Path,
) -> None:
    _write_current(tmp_path)
    current = load_current_spec(tmp_path, "signing")
    fr1 = _requirement_hash(tmp_path, "FR-001")
    fr2 = _requirement_hash(tmp_path, "FR-002")

    _write_change(
        tmp_path,
        "SIGN-A",
        baseline=current.sha256,
        operations=[
            {
                "op": "MODIFIED",
                "requirement_id": "FR-001",
                "previous_hash": fr1,
                "definition": "Change A.",
                "reason": "A.",
            }
        ],
    )
    _write_change(
        tmp_path,
        "SIGN-B",
        baseline=current.sha256,
        operations=[
            {
                "op": "MODIFIED",
                "requirement_id": "FR-001",
                "previous_hash": fr1,
                "definition": "Change B.",
                "reason": "B.",
            }
        ],
    )
    _write_change(
        tmp_path,
        "SIGN-C",
        baseline=current.sha256,
        operations=[
            {
                "op": "MODIFIED",
                "requirement_id": "FR-002",
                "previous_hash": fr2,
                "definition": "Change C.",
                "reason": "C.",
            }
        ],
    )

    conflict = detect_parallel_change_conflicts(tmp_path, ("SIGN-B", "SIGN-A"))
    assert conflict.valid is False
    assert conflict.feature_ids == ("SIGN-A", "SIGN-B")
    assert len(conflict.findings) == 1
    assert conflict.findings[0].kind == "PARALLEL_CHANGE_CONFLICT"
    assert conflict.findings[0].requirement_id == "FR-001"
    assert conflict.findings[0].related_features == ("SIGN-A", "SIGN-B")

    disjoint = detect_parallel_change_conflicts(tmp_path, ("SIGN-A", "SIGN-C"))
    assert disjoint.valid is True
    assert disjoint.findings == ()


def test_parallel_rename_destination_conflicts_with_other_change_add(tmp_path: Path) -> None:
    _write_current(tmp_path)
    current = load_current_spec(tmp_path, "signing")
    _write_change(
        tmp_path,
        "SIGN-R",
        baseline=current.sha256,
        operations=[
            {
                "op": "RENAMED",
                "requirement_id": "FR-001",
                "new_requirement_id": "FR-010",
                "previous_hash": _requirement_hash(tmp_path, "FR-001"),
                "reason": "Rename.",
            }
        ],
    )
    _write_change(
        tmp_path,
        "SIGN-N",
        baseline=current.sha256,
        operations=[
            {
                "op": "ADDED",
                "requirement_id": "FR-010",
                "definition": "New requirement using rename destination.",
                "reason": "Conflict fixture.",
            }
        ],
    )

    report = detect_parallel_change_conflicts(tmp_path, ("SIGN-R", "SIGN-N"))

    assert report.valid is False
    assert [(item.requirement_id, item.related_features) for item in report.findings] == [
        ("FR-010", ("SIGN-N", "SIGN-R"))
    ]
    assert json.loads(report.to_json())["findings"][0]["code"] == "SDAI-SPECVAL-009"
