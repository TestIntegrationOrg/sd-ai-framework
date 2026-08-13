from __future__ import annotations

from pathlib import Path

from sdai.analysis_rules import analyze_feature


FEATURE = "ANALYZE-DEDUP"


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")


def test_duplicate_unresolved_adr_emits_one_finding_with_all_declaration_evidence(
    tmp_path: Path,
) -> None:
    feature = tmp_path / "specs" / "changes" / FEATURE
    _write(
        feature / "requirements.md",
        """- FR-001: Implement feature with TASK-001.
- NFR-001: Complete quickly with TASK-001.
- AC-001: Valid request succeeds with TEST-001.
""",
    )
    _write(feature / "tasks.md", "- TASK-001: Implement FR-001 and NFR-001.\n")
    _write(feature / "tests.md", "- TEST-001: Verify AC-001.\n")
    _write(feature / "adr" / "one.md", "# ADR-001: Pending choice\nstatus: proposed\n")
    _write(feature / "adr" / "two.md", "# ADR-001: Pending choice\nstatus: proposed\n")

    report = analyze_feature(tmp_path, FEATURE, environ={})
    unresolved = [
        item
        for item in report.findings
        if item.code == "UNRESOLVED_ADR" and item.entity_id == "ADR-001"
    ]

    assert len(unresolved) == 1
    assert {(item.source, item.line) for item in unresolved[0].evidence} == {
        (f"specs/changes/{FEATURE}/adr/one.md", 1),
        (f"specs/changes/{FEATURE}/adr/two.md", 1),
    }
    assert not any(item.code == "ARCHITECTURE_CONFLICT" for item in report.findings)
