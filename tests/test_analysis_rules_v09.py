from __future__ import annotations

from pathlib import Path

import pytest

from sdai.analysis_rules import analyze_feature
from sdai.artifact_state import record_artifact_state


FEATURE = "ANALYZE-200"
REQUIRED_CODES = {
    "ORPHAN_REQUIREMENT",
    "ORPHAN_TASK",
    "MISSING_NFR",
    "ARCHITECTURE_CONFLICT",
    "CONTRACT_CONFLICT",
    "UNRESOLVED_ADR",
    "UNTESTED_SCENARIO",
    "UNAPPROVED_BREAKING_CHANGE",
    "UNMITIGATED_THREAT",
    "STALE_ARTIFACT",
}


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")
    return path


def _snapshot(root: Path) -> dict[str, bytes]:
    feature = root / "specs" / "changes" / FEATURE
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in feature.rglob("*")
        if path.is_file()
    }


def _broken_feature(root: Path) -> Path:
    feature = root / "specs" / "changes" / FEATURE
    requirements = _write(
        feature / "requirements.md",
        """# Requirements

- FR-001: Sign a script without an implementation task.
- AC-001: Valid request returns a signature.
""",
    )
    _write(
        feature / "tasks.md",
        """# Tasks

- TASK-001: Implement an unrelated migration.
""",
    )
    _write(
        feature / "adr" / "one.md",
        """# ADR-001: Use AWS KMS
status: proposed
""",
    )
    _write(
        feature / "adr" / "two.md",
        """# ADR-001: Store key material locally
status: accepted
""",
    )
    _write(
        feature / "contracts" / "one.yaml",
        """id: CONTRACT-001
status: breaking
references: [APPROVAL-001]
""",
    )
    _write(
        feature / "contracts" / "two.yaml",
        """id: CONTRACT-001
status: proposed
""",
    )
    _write(
        feature / "approvals" / "contract.yaml",
        """approval_id: APPROVAL-001
status: pending
references: [CONTRACT-001]
""",
    )
    _write(
        feature / "security" / "threats.yaml",
        """threat_id: THREAT-001
status: open
references: [MITIGATION-001]

mitigation_id: MITIGATION-001
status: planned
references: [THREAT-001]
""",
    )
    return requirements


def _clean_feature(root: Path) -> None:
    feature = root / "specs" / "changes" / FEATURE
    _write(
        feature / "requirements.md",
        """# Requirements

- FR-001: Sign a script. TASK-001 implements this requirement.
- NFR-001: Signing completes within two seconds. TASK-001 implements this requirement.
- AC-001: Valid input returns a signature. TEST-001 verifies this scenario.
""",
    )
    _write(
        feature / "tasks.md",
        """# Tasks

- TASK-001: Implement FR-001 and NFR-001.
""",
    )
    _write(
        feature / "tests.md",
        """# Tests

- TEST-001: Verify AC-001 and FR-001.
""",
    )
    _write(
        feature / "adr" / "ADR-001.md",
        """# ADR-001: Use AWS KMS
status: accepted
references: [FR-001, CONTRACT-001]
""",
    )
    _write(
        feature / "contracts" / "api.yaml",
        """id: CONTRACT-001
status: compatible
references: [ADR-001, FR-001]
""",
    )
    _write(
        feature / "security" / "threats.yaml",
        """threat_id: THREAT-001
status: open
references: [MITIGATION-001]

mitigation_id: MITIGATION-001
status: implemented
references: [THREAT-001, TASK-001]
""",
    )


def test_inconsistent_feature_emits_every_required_finding_with_evidence(
    tmp_path: Path,
) -> None:
    requirements = _broken_feature(tmp_path)
    record_artifact_state(
        tmp_path,
        FEATURE,
        "requirements",
        risk="standard",
        environ={},
    )
    requirements.write_text(
        requirements.read_text(encoding="utf-8") + "\nChanged after validation café Δ.\n",
        encoding="utf-8",
        newline="\n",
    )
    before = _snapshot(tmp_path)

    first = analyze_feature(tmp_path, FEATURE, environ={})
    second = analyze_feature(tmp_path, FEATURE, environ={})

    codes = {item.code for item in first.findings}
    assert REQUIRED_CODES <= codes
    assert first.to_json() == second.to_json()
    assert _snapshot(tmp_path) == before
    for finding in first.findings:
        assert finding.evidence, finding.code
        assert all(item.source for item in finding.evidence)
        assert all(item.line >= 1 for item in finding.evidence)
    stale = [item for item in first.findings if item.code == "STALE_ARTIFACT"]
    assert any(item.entity_id == "requirements" for item in stale)


def test_fully_linked_feature_has_no_findings_without_prior_stale_evidence(
    tmp_path: Path,
) -> None:
    _clean_feature(tmp_path)

    report = analyze_feature(tmp_path, FEATURE, environ={})

    assert report.findings == ()


def test_unrecorded_artifacts_are_not_reported_as_stale(tmp_path: Path) -> None:
    _clean_feature(tmp_path)

    report = analyze_feature(tmp_path, FEATURE, environ={})

    assert not any(item.code == "STALE_ARTIFACT" for item in report.findings)


def test_requirement_or_task_reference_must_point_to_declared_counterpart(
    tmp_path: Path,
) -> None:
    feature = tmp_path / "specs" / "changes" / FEATURE
    _write(
        feature / "requirements.md",
        """- FR-001: References TASK-999, which is not declared.
- NFR-001: References TASK-999 too.
""",
    )
    _write(
        feature / "tasks.md",
        "- TASK-001: References FR-999, which is not declared.\n",
    )

    report = analyze_feature(tmp_path, FEATURE, environ={})
    orphans = {(item.code, item.entity_id) for item in report.findings if item.code.startswith("ORPHAN_")}

    assert ("ORPHAN_REQUIREMENT", "FR-001") in orphans
    assert ("ORPHAN_REQUIREMENT", "NFR-001") in orphans
    assert ("ORPHAN_TASK", "TASK-001") in orphans


def test_identical_duplicate_adr_and_contract_declarations_are_not_conflicts(
    tmp_path: Path,
) -> None:
    _clean_feature(tmp_path)
    feature = tmp_path / "specs" / "changes" / FEATURE
    _write(feature / "adr" / "copy.md", "# ADR-001: Use AWS KMS\nstatus: accepted\n")
    _write(feature / "contracts" / "copy.yaml", "id: CONTRACT-001\nstatus: compatible\n")

    report = analyze_feature(tmp_path, FEATURE, environ={})

    assert not any(item.code == "ARCHITECTURE_CONFLICT" for item in report.findings)
    assert not any(item.code == "CONTRACT_CONFLICT" for item in report.findings)


def test_approved_breaking_contract_is_not_reported(tmp_path: Path) -> None:
    _clean_feature(tmp_path)
    feature = tmp_path / "specs" / "changes" / FEATURE
    (feature / "contracts" / "api.yaml").write_text(
        "id: CONTRACT-001\nstatus: breaking\nreferences: [APPROVAL-001]\n",
        encoding="utf-8",
        newline="\n",
    )
    _write(
        feature / "approvals" / "breaking.yaml",
        "approval_id: APPROVAL-001\nstatus: approved\nreferences: [CONTRACT-001]\n",
    )

    report = analyze_feature(tmp_path, FEATURE, environ={})

    assert not any(item.code == "UNAPPROVED_BREAKING_CHANGE" for item in report.findings)


def test_breaking_marker_is_explicit_and_non_breaking_text_is_not_misclassified(
    tmp_path: Path,
) -> None:
    _clean_feature(tmp_path)
    feature = tmp_path / "specs" / "changes" / FEATURE
    contract = feature / "contracts" / "api.yaml"
    contract.write_text(
        "id: CONTRACT-001\nstatus: compatible\n",
        encoding="utf-8",
        newline="\n",
    )
    _write(feature / "contracts" / "note.md", "# CONTRACT-002: Non-breaking compatibility update\nstatus: compatible\n")

    report = analyze_feature(tmp_path, FEATURE, environ={})

    assert not any(item.code == "UNAPPROVED_BREAKING_CHANGE" for item in report.findings)

    (feature / "contracts" / "note.md").write_text(
        "# CONTRACT-002: [breaking] Remove legacy field\nstatus: proposed\n",
        encoding="utf-8",
        newline="\n",
    )
    changed = analyze_feature(tmp_path, FEATURE, environ={})
    assert any(
        item.code == "UNAPPROVED_BREAKING_CHANGE" and item.entity_id == "CONTRACT-002"
        for item in changed.findings
    )


def test_completed_mitigation_or_resolved_threat_suppresses_unmitigated_finding(
    tmp_path: Path,
) -> None:
    _clean_feature(tmp_path)

    first = analyze_feature(tmp_path, FEATURE, environ={})
    assert not any(item.code == "UNMITIGATED_THREAT" for item in first.findings)

    threat_file = tmp_path / "specs" / "changes" / FEATURE / "security" / "threats.yaml"
    threat_file.write_text(
        "threat_id: THREAT-001\nstatus: resolved\n\nmitigation_id: MITIGATION-001\nstatus: planned\n",
        encoding="utf-8",
        newline="\n",
    )
    second = analyze_feature(tmp_path, FEATURE, environ={})
    assert not any(item.code == "UNMITIGATED_THREAT" for item in second.findings)


def test_analysis_is_byte_for_byte_read_only(tmp_path: Path) -> None:
    _clean_feature(tmp_path)
    before = _snapshot(tmp_path)

    analyze_feature(tmp_path, FEATURE, environ={})

    assert _snapshot(tmp_path) == before
