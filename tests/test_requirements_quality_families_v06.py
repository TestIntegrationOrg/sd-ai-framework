from __future__ import annotations

from pathlib import Path

from sdai.models import FeatureContext
from sdai.requirements_quality import check_requirements


def _write_spec(root: Path, feature: str, text: str) -> None:
    path = FeatureContext(root, feature).artifact("specification.md")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_structured_id_check_requires_fr_nfr_and_acceptance_families(
    tmp_path: Path,
) -> None:
    _write_spec(
        tmp_path,
        "ONLY-AC",
        """# Specification — ONLY-AC

## Problem
A user needs an observable API response.

## Goals
- Provide a response.

## Functional Requirements
- AC-001: A valid request returns an output.

## Non-Functional Requirements
Security, failure observability, compatibility, deployment, rollback, retention, and compliance are described with a 2 second target.

## Acceptance Criteria
- AC-002: The response is visible to the caller.

## Open Questions
None
""",
    )

    report = check_requirements(tmp_path, "ONLY-AC")
    by_id = {finding.id: finding for finding in report.findings}

    assert by_id["RQ-003"].status == "fail"
    assert "Missing structured requirement families: FR, NFR" in by_id["RQ-003"].detail
    assert by_id["RQ-004"].status == "fail"
    assert "Both FR-NNN and NFR-NNN requirements are required" in by_id["RQ-004"].detail
