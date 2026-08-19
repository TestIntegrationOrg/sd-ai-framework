from __future__ import annotations

from hashlib import sha256
from pathlib import Path

from sdai.audit_ledger import AuditLedger
from sdai.audit_provenance import AuditAction, AuditActor, AuditBinding
from sdai.trace_builder import build_feature_trace_graph
from sdai.trace_graph import TraceRelation


FEATURE = "AUDIT-BIND-239"


def _sha(content: bytes) -> str:
    return "sha256:" + sha256(content).hexdigest()


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")
    return path


def _feature(root: Path) -> Path:
    feature = root / "specs" / "changes" / FEATURE
    _write(feature / "requirements.md", "# Requirements\n\n- FR-001: Bind audit evidence.\n")
    return feature


def _event(root: Path, *, bindings: tuple[AuditBinding, ...] = (), action: str = "evidence.recorded"):
    return AuditLedger(root, FEATURE).append(
        category="evidence",
        actor=AuditActor("system", "trace-test"),
        action=AuditAction(action, f"feature:{FEATURE}"),
        bindings=bindings,
        metadata={"status": "recorded"},
    )


def test_missing_audit_bound_repository_evidence_is_visible_gap(tmp_path: Path) -> None:
    feature = _feature(tmp_path)
    report = _write(feature / "quality" / "report.md", "quality passed\n")
    relative = report.relative_to(tmp_path).as_posix()
    _event(tmp_path, bindings=(AuditBinding("quality", relative, _sha(report.read_bytes())),))
    report.unlink()

    result = build_feature_trace_graph(tmp_path, FEATURE, environ={})

    gap = next(item for item in result.gaps if item.kind == "missing-audit-binding")
    assert gap.target == relative
    assert gap.relation == TraceRelation.REFERENCES.value
    assert gap.source.endswith("/.sdai/audit/events.jsonl")


def test_mutated_audit_bound_repository_evidence_is_stale_gap(tmp_path: Path) -> None:
    feature = _feature(tmp_path)
    report = _write(feature / "security" / "review.md", "security passed\n")
    relative = report.relative_to(tmp_path).as_posix()
    _event(tmp_path, bindings=(AuditBinding("security", relative, _sha(report.read_bytes())),))
    report.write_text("security changed\n", encoding="utf-8", newline="\n")

    result = build_feature_trace_graph(tmp_path, FEATURE, environ={})

    gap = next(item for item in result.gaps if item.kind == "stale-audit-binding")
    assert gap.target == relative
    assert "no longer matches" in (gap.detail or "")
    assert not any(
        node.metadata.get("audit_trace_role") == "binding"
        and node.metadata.get("source") == relative
        for node in result.graph.nodes
    )


def test_local_approval_artifact_is_hash_bound_without_identity_claim(tmp_path: Path) -> None:
    feature = _feature(tmp_path)
    approval = _write(
        feature / "approvals" / "release.yaml",
        "version: 2\ngate: release\napprovals:\n  - principal: local-reviewer\n    role: architect\n",
    )
    relative = approval.relative_to(tmp_path).as_posix()
    _event(tmp_path, bindings=(AuditBinding("evidence", relative, _sha(approval.read_bytes())),))

    result = build_feature_trace_graph(tmp_path, FEATURE, environ={})

    node = next(
        item
        for item in result.graph.nodes
        if item.metadata.get("audit_trace_role") == "binding"
        and item.metadata.get("source") == relative
    )
    assert node.metadata["local_assertion_only"] is True
    graph_json = result.graph.to_json()
    assert "local-reviewer" not in graph_json
    assert "identityVerified" not in graph_json
    assert "authorized" not in graph_json
    assert "enterprise identity not verified" in graph_json


def test_synthetic_authority_hash_is_projected_without_repository_file(tmp_path: Path) -> None:
    _feature(tmp_path)
    digest = "sha256:" + "7" * 64
    event = _event(
        tmp_path,
        bindings=(AuditBinding("evidence", "workflow-engine2/run-status", digest),),
    )

    result = build_feature_trace_graph(tmp_path, FEATURE, environ={})

    authority = next(
        node
        for node in result.graph.nodes
        if node.metadata.get("audit_trace_role") == "binding"
        and node.metadata.get("source") == "workflow-engine2/run-status"
    )
    assert authority.metadata["sha256"] == digest
    audit = next(node for node in result.graph.nodes if node.entity_id == f"audit-event:{event.event_id}")
    assert any(
        edge.source == audit.node_id
        and edge.target == authority.node_id
        and edge.relation is TraceRelation.REFERENCES
        for edge in result.graph.edges
    )
    assert not any(gap.target == "workflow-engine2/run-status" for gap in result.gaps)


def test_explicit_audit_event_hash_reference_links_existing_event_node(tmp_path: Path) -> None:
    _feature(tmp_path)
    first = _event(tmp_path, action="workflow.step.started")
    second = _event(
        tmp_path,
        action="workflow.step.completed",
        bindings=(AuditBinding("evidence", "workflow/step-start/specification", first.sha256),),
    )

    result = build_feature_trace_graph(tmp_path, FEATURE, environ={})

    first_node = next(node for node in result.graph.nodes if node.entity_id == f"audit-event:{first.event_id}")
    second_node = next(node for node in result.graph.nodes if node.entity_id == f"audit-event:{second.event_id}")
    edge = next(
        edge
        for edge in result.graph.edges
        if edge.source == second_node.node_id and edge.target == first_node.node_id
    )
    assert edge.relation is TraceRelation.REFERENCES
    bindings = edge.metadata["bindings"]
    assert isinstance(bindings, tuple)
    assert bindings[0]["sha256"] == first.sha256
