from __future__ import annotations

from pathlib import Path

import pytest

from sdai.trace_builder import TraceBuildError, build_feature_trace_graph
from sdai.trace_evidence import (
    EvidenceBinding,
    EvidenceBindingKind,
    EvidenceKind,
    EvidenceProducer,
    EvidenceStatus,
    TraceEvidence,
)
from sdai.trace_graph import TraceNodeType, TraceProvenance, TraceRelation


FEATURE = "TRACE-105"
COMMIT = "a" * 40
DIGEST = "sha256:" + "1" * 64


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")
    return path


def _feature(root: Path) -> Path:
    feature = root / "specs" / "changes" / FEATURE
    _write(
        feature / "requirements.md",
        """# Requirements

- FR-001: Sign café scripts. References ADR-001, THREAT-001, and ADR-MISSING.
- AC-001: Given a valid request signing succeeds. Verified by TEST-001.
- RFC-001: Signing architecture
- COMPONENT-001: Signing service
""",
    )
    _write(
        feature / "tasks.md",
        """# Tasks

- [ ] TASK-001: Implement FR-001 and CONTRACT-001.
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
        """# ADR-001: Use KMS
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
references: [FR-001]
""",
    )
    _write(
        feature / "approvals" / "delivery.yaml",
        """approval_id: APPROVAL-001
status: pending
references: [ADR-001]
""",
    )
    _write(
        root / "src" / "café" / "signing.py",
        """# Explicit trace links: FR-001 RFC-001 COMPONENT-001

def sign() -> None:
    pass
""",
    )
    _write(
        root / "tests" / "test_signing.py",
        """# Explicit trace links: FR-001 TEST-001

def test_signing() -> None:
    assert True
""",
    )
    return feature


def _evidence(feature: Path, *, provider: str = "codex", subject: str = "requirement:FR-001") -> Path:
    record = TraceEvidence(
        evidence_id="EVIDENCE-001",
        kind=EvidenceKind.TEST,
        status=EvidenceStatus.PASSED,
        subject=subject,
        git_commit=COMMIT,
        bindings=(
            EvidenceBinding(EvidenceBindingKind.SOURCE, "src/café/signing.py", DIGEST),
        ),
        provenance=(
            TraceProvenance(
                f"specs/changes/{FEATURE}/evidence/test.json",
                1,
                detail="pytest evidence",
            ),
        ),
        producer=EvidenceProducer("tester", provider, "model-a"),
        result={"passed": 1, "failed": 0},
        command=("python", "-m", "pytest", "tests/test_signing.py"),
        tool="pytest",
    )
    return _write(feature / "evidence" / "test.json", record.to_json())


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink()
    }


def test_builder_links_explicit_artifact_code_test_and_evidence_facts_read_only(tmp_path: Path) -> None:
    feature = _feature(tmp_path)
    _evidence(feature)
    before = _snapshot(tmp_path)

    result = build_feature_trace_graph(tmp_path, FEATURE, environ={})

    assert _snapshot(tmp_path) == before
    types = {node.type for node in result.graph.nodes}
    assert {
        TraceNodeType.REQUIREMENT,
        TraceNodeType.SCENARIO,
        TraceNodeType.RFC,
        TraceNodeType.ADR,
        TraceNodeType.COMPONENT,
        TraceNodeType.CONTRACT,
        TraceNodeType.THREAT,
        TraceNodeType.TASK,
        TraceNodeType.CODE,
        TraceNodeType.TEST,
        TraceNodeType.APPROVAL,
        TraceNodeType.EVIDENCE,
    } <= types

    edges = {(edge.relation, edge.source, edge.target) for edge in result.graph.edges}
    assert (TraceRelation.REFERENCES, "code:src/café/signing.py", "requirement:FR-001") in edges
    assert (TraceRelation.REFERENCES, "test:tests/test_signing.py", "requirement:FR-001") in edges
    assert (TraceRelation.EVIDENCED_BY, "requirement:FR-001", "evidence:EVIDENCE-001") in edges
    assert any(gap.target == "ADR-MISSING" for gap in result.gaps)


def test_builder_is_deterministic_and_preserves_utf8_portable_paths(tmp_path: Path) -> None:
    feature = _feature(tmp_path)
    _evidence(feature)

    first = build_feature_trace_graph(tmp_path, FEATURE, environ={})
    second = build_feature_trace_graph(tmp_path, FEATURE, environ={})

    assert first.graph.to_json() == second.graph.to_json()
    assert first.graph.sha256 == second.graph.sha256
    assert first.sha256 == second.sha256
    assert "\\" not in first.graph.to_json()
    assert "café" in first.graph.to_json()


def test_missing_evidence_subject_is_visible_gap_not_invented_edge(tmp_path: Path) -> None:
    feature = _feature(tmp_path)
    _evidence(feature, subject="requirement:FR-999")

    result = build_feature_trace_graph(tmp_path, FEATURE, environ={})

    assert any(
        gap.kind == "missing-evidence-subject" and gap.target == "requirement:FR-999"
        for gap in result.gaps
    )
    assert not any(
        edge.relation is TraceRelation.EVIDENCED_BY and edge.source == "requirement:FR-999"
        for edge in result.graph.edges
    )


def test_malformed_typed_evidence_fails_closed(tmp_path: Path) -> None:
    feature = _feature(tmp_path)
    _write(
        feature / "evidence" / "bad.json",
        '{"apiVersion":"sdai.trace-evidence/v1","sha256":"bad"}',
    )

    with pytest.raises(TraceBuildError, match="invalid typed trace evidence"):
        build_feature_trace_graph(tmp_path, FEATURE, environ={})


def test_conflicting_duplicate_artifact_nodes_fail_closed(tmp_path: Path) -> None:
    feature = _feature(tmp_path)
    _write(feature / "requirements-extra.md", "- FR-001: Different declaration.\n")

    with pytest.raises(TraceBuildError, match="canonical trace graph conflict"):
        build_feature_trace_graph(tmp_path, FEATURE, environ={})


def test_supported_source_symlink_fails_closed(tmp_path: Path) -> None:
    _feature(tmp_path)
    target = _write(tmp_path / "outside.txt", "FR-001\n")
    link = tmp_path / "src" / "linked.py"
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")

    with pytest.raises(TraceBuildError, match="must not be a symlink"):
        build_feature_trace_graph(tmp_path, FEATURE, environ={})
