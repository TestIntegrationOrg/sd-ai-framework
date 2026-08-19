from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import subprocess

from sdai.audit_ledger import AuditLedger
from sdai.audit_provenance import AuditAction, AuditActor, AuditBinding
from sdai.trace_builder import build_feature_trace_graph
from sdai.trace_evidence import (
    EvidenceBinding,
    EvidenceBindingKind,
    EvidenceKind,
    EvidenceProducer,
    EvidenceStatus,
    TraceEvidence,
)
from sdai.trace_freshness import ProofFreshness, evaluate_trace_evidence_file
from sdai.trace_graph import TraceProvenance


FEATURE = "AUDIT-FRESH-239"


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        check=True,
        shell=False,
    )
    return result.stdout.strip()


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")
    return path


def _sha(path: Path) -> str:
    return "sha256:" + sha256(path.read_bytes()).hexdigest()


def test_audit_linked_typed_evidence_becomes_stale_via_existing_freshness_engine(tmp_path: Path) -> None:
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "sdai@example.test")
    _git(tmp_path, "config", "user.name", "SDAI Test")
    source = _write(tmp_path / "src" / "service.py", "# FR-001\nVALUE = 1\n")
    feature = tmp_path / "specs" / "changes" / FEATURE
    _write(feature / "requirements.md", "# Requirements\n\n- FR-001: Preserve freshness authority.\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "baseline")
    commit = _git(tmp_path, "rev-parse", "HEAD")

    evidence = TraceEvidence(
        evidence_id="EVIDENCE-FRESH-001",
        kind=EvidenceKind.TEST,
        status=EvidenceStatus.PASSED,
        subject="requirement:FR-001",
        git_commit=commit,
        bindings=(
            EvidenceBinding(EvidenceBindingKind.SOURCE, "src/service.py", _sha(source)),
        ),
        provenance=(
            TraceProvenance(
                f"specs/changes/{FEATURE}/evidence/test.json",
                1,
            ),
        ),
        producer=EvidenceProducer("tester"),
        result={"passed": 1},
        command=("pytest", "-q"),
        tool="pytest",
    )
    evidence_path = _write(feature / "evidence" / "test.json", evidence.to_json())
    relative = evidence_path.relative_to(tmp_path).as_posix()
    AuditLedger(tmp_path, FEATURE).append(
        category="evidence",
        actor=AuditActor("system", "freshness-test"),
        action=AuditAction("evidence.recorded", f"feature:{FEATURE}"),
        bindings=(AuditBinding("evidence", relative, _sha(evidence_path)),),
        metadata={"status": "recorded"},
    )

    graph = build_feature_trace_graph(tmp_path, FEATURE, environ={})
    audit_node = next(
        node for node in graph.graph.nodes if node.metadata.get("audit_trace_role") == "event"
    )
    typed_node = next(
        node for node in graph.graph.nodes if node.entity_id == "EVIDENCE-FRESH-001"
    )
    assert any(
        edge.source == audit_node.node_id and edge.target == typed_node.node_id
        for edge in graph.graph.edges
    )
    assert evaluate_trace_evidence_file(tmp_path, evidence_path).freshness is ProofFreshness.VALID

    source.write_text("# FR-001\nVALUE = 2\n", encoding="utf-8", newline="\n")
    stale = evaluate_trace_evidence_file(tmp_path, evidence_path)
    assert stale.freshness is ProofFreshness.STALE
    assert stale.satisfies_current_coverage is False

    rebuilt = build_feature_trace_graph(tmp_path, FEATURE, environ={})
    assert not any(gap.kind == "stale-audit-binding" for gap in rebuilt.gaps)
    assert next(
        node for node in rebuilt.graph.nodes if node.entity_id == "EVIDENCE-FRESH-001"
    ).node_id == typed_node.node_id
