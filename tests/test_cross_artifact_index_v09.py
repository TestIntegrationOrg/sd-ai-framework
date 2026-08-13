from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml

from sdai.cross_artifact import (
    AnalysisFinding,
    AnalysisReport,
    CrossArtifactError,
    SourceEvidence,
    build_feature_artifact_index,
)


FEATURE = "ANALYZE-100"


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")
    return path


def _feature(root: Path) -> Path:
    feature = root / "specs" / "changes" / FEATURE
    _write(
        feature / "requirements.md",
        """# Requirements

- FR-001: Sign one PowerShell script. Covered by AC-001 and TASK-001.
- NFR-001: Signing MUST finish within two seconds. Verified by TEST-002.
- AC-001: Given a valid request, signing succeeds. TEST-001 covers this scenario.
""",
    )
    _write(
        feature / "tasks.md",
        """# Tasks

- [ ] TASK-001: Implement signing for FR-001 and NFR-001.
- [ ] TASK-002: Add certificate validation for FR-002.
""",
    )
    _write(
        feature / "tests.md",
        """# Tests

- TEST-001: Verify AC-001 and FR-001.
- TEST-002: Verify NFR-001.
""",
    )
    _write(
        feature / "adr" / "ADR-001.md",
        """# ADR-001: Use AWS KMS
status: accepted

ADR-001 governs FR-001 and CONTRACT-001.
""",
    )
    _write(
        feature / "contracts" / "api.yaml",
        """id: CONTRACT-001
status: proposed
references: [FR-001, ADR-001]
""",
    )
    _write(
        feature / "security" / "threats.yaml",
        """threat_id: THREAT-001
status: open
references: [FR-001, MITIGATION-001]

mitigation_id: MITIGATION-001
status: planned
references: [THREAT-001, TASK-002]
""",
    )
    _write(
        feature / "approvals" / "delivery.yaml",
        """approval_id: APPROVAL-001
status: pending
references: [CONTRACT-001, ADR-001]
""",
    )
    return feature


def _snapshot(root: Path) -> dict[str, bytes]:
    feature = root / "specs" / "changes" / FEATURE
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in feature.rglob("*")
        if path.is_file()
    }


def test_index_is_read_only_deterministic_and_contains_exact_source_evidence(
    tmp_path: Path,
) -> None:
    _feature(tmp_path)
    before = _snapshot(tmp_path)

    first = build_feature_artifact_index(tmp_path, FEATURE, environ={})
    second = build_feature_artifact_index(tmp_path, FEATURE, environ={})

    assert first.to_json() == second.to_json()
    assert first.sha256 == second.sha256
    assert _snapshot(tmp_path) == before
    assert "\\" not in first.to_json()
    assert first.to_json().encode("utf-8").decode("utf-8") == first.to_json()

    by_id = first.by_id()
    assert by_id["FR-001"][0].kind == "requirement"
    assert by_id["AC-001"][0].kind == "scenario"
    assert by_id["TASK-001"][0].kind == "task"
    assert by_id["TEST-001"][0].kind == "test"
    assert by_id["ADR-001"][0].kind == "adr"
    assert by_id["ADR-001"][0].status == "accepted"
    assert by_id["CONTRACT-001"][0].kind == "contract"
    assert by_id["THREAT-001"][0].kind == "threat"
    assert by_id["MITIGATION-001"][0].kind == "mitigation"
    assert by_id["APPROVAL-001"][0].kind == "approval"

    edges = {
        (edge.from_id, edge.to_id, edge.source, edge.line)
        for edge in first.relationships
    }
    assert any(source.endswith("requirements.md") and source.startswith("specs/changes/") for _, _, source, _ in edges)
    assert any(left == "TASK-001" and right == "FR-001" for left, right, _, _ in edges)
    assert any(left == "ADR-001" and right == "CONTRACT-001" for left, right, _, _ in edges)
    assert any(left == "THREAT-001" and right == "MITIGATION-001" for left, right, _, _ in edges)
    assert any(left == "APPROVAL-001" and right == "ADR-001" for left, right, _, _ in edges)


def test_effective_artifact_schema_graph_is_embedded_as_read_only_facts(tmp_path: Path) -> None:
    _feature(tmp_path)
    schema = tmp_path / ".sdai" / "schemas" / "operations.yaml"
    schema.parent.mkdir(parents=True, exist_ok=True)
    schema.write_text(
        yaml.safe_dump(
            {
                "apiVersion": "sdai/v1",
                "kind": "ArtifactSchema",
                "metadata": {"id": "analysis-operations", "version": "1.0.0"},
                "spec": {
                    "artifacts": [
                        {
                            "id": "operations",
                            "path": "specs/changes/{feature}/operations.md",
                            "type": "markdown",
                            "required": False,
                            "depends_on": ["architecture"],
                        }
                    ]
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    _write(tmp_path / "specs" / "changes" / FEATURE / "operations.md", "# Operations café Δ\n")

    index = build_feature_artifact_index(tmp_path, FEATURE, environ={})
    facts = {item.id: item for item in index.schema_artifacts}

    assert "requirements" in facts
    assert facts["requirements"].resolved_path == f"specs/changes/{FEATURE}/requirements.md"
    assert facts["requirements"].exists is True
    assert facts["operations"].depends_on == ("architecture",)
    assert facts["operations"].exists is True
    assert index.schema_topological_order.index("architecture") < index.schema_topological_order.index("operations")
    assert "repo:.sdai/schemas/operations.yaml" in index.schema_sources


def test_duplicate_entity_ids_are_preserved_for_later_analysis_not_silently_collapsed(
    tmp_path: Path,
) -> None:
    _feature(tmp_path)
    _write(
        tmp_path / "specs" / "changes" / FEATURE / "requirements-extra.md",
        "- FR-001: Conflicting duplicate declaration.\n",
    )

    index = build_feature_artifact_index(tmp_path, FEATURE, environ={})

    duplicates = index.by_id()["FR-001"]
    assert len(duplicates) == 2
    assert {item.source for item in duplicates} == {
        f"specs/changes/{FEATURE}/requirements.md",
        f"specs/changes/{FEATURE}/requirements-extra.md",
    }
    assert duplicates[0].key != duplicates[1].key


def test_relationship_context_tracks_references_until_next_declaration(tmp_path: Path) -> None:
    feature = tmp_path / "specs" / "changes" / FEATURE
    _write(
        feature / "tasks.md",
        """- TASK-001: Implement the service.
requirements: [FR-001, NFR-001]
tests: [TEST-001]
- TASK-002: Follow-up.
requirements: [FR-002]
""",
    )

    index = build_feature_artifact_index(tmp_path, FEATURE, environ={})
    edges = {(item.from_id, item.to_id, item.line) for item in index.relationships}

    assert ("TASK-001", "FR-001", 2) in edges
    assert ("TASK-001", "NFR-001", 2) in edges
    assert ("TASK-001", "TEST-001", 3) in edges
    assert ("TASK-002", "FR-002", 5) in edges
    assert ("TASK-001", "FR-002", 5) not in edges


def test_invalid_utf8_and_symlink_sources_fail_closed(tmp_path: Path) -> None:
    feature = tmp_path / "specs" / "changes" / FEATURE
    feature.mkdir(parents=True)
    invalid = feature / "requirements.md"
    invalid.write_bytes(b"\xff\xfe\x00")

    with pytest.raises(CrossArtifactError, match="SDAI-ANALYSIS-003"):
        build_feature_artifact_index(tmp_path, FEATURE, environ={})

    invalid.unlink()
    outside = tmp_path / "outside.md"
    outside.write_text("- FR-999: outside\n", encoding="utf-8")
    link = feature / "linked.md"
    try:
        os.symlink(outside, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is unavailable")

    with pytest.raises(CrossArtifactError, match="SDAI-ANALYSIS-002.*symlink"):
        build_feature_artifact_index(tmp_path, FEATURE, environ={})


def test_missing_feature_directory_fails_closed_without_creating_it(tmp_path: Path) -> None:
    target = tmp_path / "specs" / "changes" / FEATURE

    with pytest.raises(CrossArtifactError, match="SDAI-ANALYSIS-001"):
        build_feature_artifact_index(tmp_path, FEATURE, environ={})

    assert not target.exists()


def test_findings_v1_contract_is_stable_sorted_and_evidence_backed(tmp_path: Path) -> None:
    _feature(tmp_path)
    index = build_feature_artifact_index(tmp_path, FEATURE, environ={})
    report = AnalysisReport(
        feature_id=FEATURE,
        index_sha256=index.sha256,
        findings=(
            AnalysisFinding(
                "ORPHAN_TASK",
                "warning",
                "TASK-002 does not map to a declared requirement.",
                entity_id="TASK-002",
                evidence=(
                    SourceEvidence(
                        f"specs/changes/{FEATURE}/tasks.md",
                        4,
                        "TASK-002",
                        "task declaration",
                    ),
                ),
            ),
            AnalysisFinding(
                "MISSING_NFR",
                "blocking",
                "No performance NFR is mapped to the contract.",
                evidence=(
                    SourceEvidence(
                        f"specs/changes/{FEATURE}/contracts/api.yaml",
                        1,
                        "CONTRACT-001",
                    ),
                ),
            ),
        ),
    )

    first = report.to_json()
    second = report.to_json()
    payload = json.loads(first)

    assert first == second
    assert payload["apiVersion"] == "sdai.findings/v1"
    assert payload["feature_id"] == FEATURE
    assert payload["index_sha256"] == index.sha256
    assert [item["code"] for item in payload["findings"]] == ["MISSING_NFR", "ORPHAN_TASK"]
    assert payload["findings"][0]["evidence"][0]["source"].startswith("specs/changes/")
    assert "provider" not in first.casefold()
    assert "model" not in first.casefold()


def test_invalid_finding_code_or_severity_is_rejected() -> None:
    with pytest.raises(ValueError, match="unsupported analysis severity"):
        AnalysisFinding("ORPHAN_TASK", "critical", "bad")
    with pytest.raises(ValueError, match="invalid analysis finding code"):
        AnalysisFinding("bad-code", "warning", "bad")
