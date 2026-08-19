from __future__ import annotations

from hashlib import sha256
from pathlib import Path

import pytest

from sdai.audit_ledger import AuditLedger
from sdai.audit_provenance import AuditAction, AuditActor, AuditBinding
from sdai.audit_trace import AuditTraceError, build_audit_trace_index
from sdai.trace_builder import TraceBuildError, build_feature_trace_graph
from sdai.trace_evidence import (
    EvidenceBinding,
    EvidenceBindingKind,
    EvidenceKind,
    EvidenceProducer,
    EvidenceStatus,
    TraceEvidence,
)
from sdai.trace_graph import TraceProvenance, TraceRelation


FEATURE = "AUDIT-TRACE-239"
COMMIT = "a" * 40


def _sha(content: bytes) -> str:
    return "sha256:" + sha256(content).hexdigest()


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")
    return path


def _feature(root: Path) -> Path:
    feature = root / "specs" / "changes" / FEATURE
    _write(feature / "requirements.md", "# Requirements\n\n- FR-001: Trace signing evidence.\n")
    _write(root / "src" / "signing.py", "# Trace: FR-001\n\ndef sign() -> None:\n    pass\n")
    return feature


def _typed_evidence(root: Path, feature: Path) -> tuple[Path, TraceEvidence]:
    source = root / "src" / "signing.py"
    record = TraceEvidence(
        evidence_id="EVIDENCE-AUDIT-001",
        kind=EvidenceKind.TEST,
        status=EvidenceStatus.PASSED,
        subject="requirement:FR-001",
        git_commit=COMMIT,
        bindings=(
            EvidenceBinding(EvidenceBindingKind.SOURCE, "src/signing.py", _sha(source.read_bytes())),
        ),
        provenance=(
            TraceProvenance(
                f"specs/changes/{FEATURE}/evidence/test.json",
                1,
                detail="audit trace fixture",
            ),
        ),
        producer=EvidenceProducer("tester"),
        result={"passed": 1},
        command=("pytest", "-q"),
        tool="pytest",
    )
    path = _write(feature / "evidence" / "test.json", record.to_json())
    return path, record


def _event(root: Path, *, bindings: tuple[AuditBinding, ...] = (), action: str = "evidence.recorded"):
    return AuditLedger(root, FEATURE).append(
        category="evidence",
        actor=AuditActor("system", "trace-test"),
        action=AuditAction(action, f"feature:{FEATURE}"),
        bindings=bindings,
        metadata={"status": "recorded"},
    )


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink()
    }


def test_verified_audit_event_reuses_typed_evidence_node_and_is_deterministic(tmp_path: Path) -> None:
    feature = _feature(tmp_path)
    evidence_path, record = _typed_evidence(tmp_path, feature)
    relative = evidence_path.relative_to(tmp_path).as_posix()
    event = _event(
        tmp_path,
        bindings=(AuditBinding("evidence", relative, _sha(evidence_path.read_bytes())),),
    )
    ledger_snapshot = AuditLedger(tmp_path, FEATURE).verify()

    first = build_feature_trace_graph(tmp_path, FEATURE, environ={})
    second = build_feature_trace_graph(tmp_path, FEATURE, environ={})

    assert first.graph.to_json() == second.graph.to_json()
    ledger_node = next(node for node in first.graph.nodes if node.entity_id == f"audit-ledger:{FEATURE}")
    audit_node = next(node for node in first.graph.nodes if node.entity_id == f"audit-event:{event.event_id}")
    typed_node = next(node for node in first.graph.nodes if node.entity_id == record.evidence_id)
    assert ledger_node.metadata["head_sha256"] == ledger_snapshot.head_sha256
    assert ledger_node.metadata["export_sha256"] == ledger_snapshot.export_sha256
    assert audit_node.metadata["event_sha256"] == event.sha256
    assert "actor" not in audit_node.metadata
    assert sum(node.entity_id == record.evidence_id for node in first.graph.nodes) == 1
    refs = {(edge.source, edge.target, edge.relation) for edge in first.graph.edges}
    assert (audit_node.node_id, typed_node.node_id, TraceRelation.REFERENCES) in refs
    assert ("requirement:FR-001", typed_node.node_id, TraceRelation.EVIDENCED_BY) in refs


def test_tampered_audit_chain_fails_closed_before_graph_projection(tmp_path: Path) -> None:
    _feature(tmp_path)
    _event(tmp_path)
    events_path = tmp_path / "specs" / "changes" / FEATURE / ".sdai" / "audit" / "events.jsonl"
    content = events_path.read_text(encoding="utf-8")
    events_path.write_text(
        content.replace("evidence.recorded", "evidence.recordex", 1),
        encoding="utf-8",
        newline="\n",
    )

    with pytest.raises(TraceBuildError, match="audit ledger integrity verification failed"):
        build_feature_trace_graph(tmp_path, FEATURE, environ={})


def test_feature_without_audit_ledger_preserves_read_only_trace_behavior(tmp_path: Path) -> None:
    _feature(tmp_path)
    before = _snapshot(tmp_path)
    result = build_feature_trace_graph(tmp_path, FEATURE, environ={})
    assert _snapshot(tmp_path) == before
    assert not any(node.metadata.get("audit_trace_role") for node in result.graph.nodes)
    assert not (tmp_path / "specs" / "changes" / FEATURE / ".sdai" / "audit").exists()


def test_audit_trace_projection_limit_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _feature(tmp_path)
    _event(tmp_path)
    _event(tmp_path)
    from sdai import audit_trace

    monkeypatch.setattr(audit_trace, "_MAX_TRACE_EVENTS", 1)
    with pytest.raises(AuditTraceError, match="exceeds 1 events"):
        build_audit_trace_index(tmp_path, FEATURE, ())
