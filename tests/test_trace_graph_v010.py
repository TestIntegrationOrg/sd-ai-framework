from __future__ import annotations

import json
from pathlib import Path

import pytest

from sdai.trace_graph import (
    TRACE_GRAPH_API_VERSION,
    TraceEdge,
    TraceGraph,
    TraceGraphError,
    TraceNode,
    TraceNodeType,
    TraceProvenance,
    TraceRelation,
    trace_node_id,
    trace_provenance_for_path,
)


FEATURE = "TRACE-100"


def _p(source: str, line: int, detail: str | None = None) -> TraceProvenance:
    return TraceProvenance(source=source, line=line, detail=detail)


def _node(
    kind: TraceNodeType,
    entity_id: str,
    source: str,
    line: int,
    *,
    label: str | None = None,
    metadata: dict[str, object] | None = None,
) -> TraceNode:
    return TraceNode(
        type=kind,
        entity_id=entity_id,
        label=label,
        metadata=metadata or {},
        provenance=(_p(source, line),),
    )


def _all_nodes() -> tuple[TraceNode, ...]:
    return (
        _node(TraceNodeType.REQUIREMENT, "FR-001", "specs/changes/TRACE-100/requirements.md", 3),
        _node(TraceNodeType.SCENARIO, "AC-001", "specs/changes/TRACE-100/requirements.md", 4),
        _node(TraceNodeType.RFC, "RFC-001", "specs/changes/TRACE-100/rfc/RFC-001.md", 1),
        _node(TraceNodeType.ADR, "ADR-001", "specs/changes/TRACE-100/adr/ADR-001.md", 1),
        _node(TraceNodeType.COMPONENT, "signing-service", "specs/changes/TRACE-100/architecture.md", 12),
        _node(TraceNodeType.CONTRACT, "CONTRACT-001", "specs/changes/TRACE-100/contracts/api.yaml", 2),
        _node(TraceNodeType.THREAT, "THREAT-001", "specs/changes/TRACE-100/security/threats.yaml", 2),
        _node(TraceNodeType.TASK, "TASK-001", "specs/changes/TRACE-100/tasks.md", 3),
        _node(TraceNodeType.CODE, "src/signing/service.py#SigningService.sign", "src/signing/service.py", 27),
        _node(TraceNodeType.TEST, "tests/test_signing.py#test_signs", "tests/test_signing.py", 14),
        _node(TraceNodeType.APPROVAL, "APPROVAL-001", "specs/changes/TRACE-100/approvals/architecture.yaml", 2),
        _node(TraceNodeType.EVIDENCE, "EVIDENCE-001", "specs/TRACE-100/.sdai/execution/run-1/tasks/TASK-001/evidence.json", 1),
    )


def _edge(
    relation: TraceRelation,
    source_type: TraceNodeType,
    source_id: str,
    target_type: TraceNodeType,
    target_id: str,
    line: int,
) -> TraceEdge:
    return TraceEdge(
        relation=relation,
        source=trace_node_id(source_type, source_id),
        target=trace_node_id(target_type, target_id),
        provenance=(_p("specs/changes/TRACE-100/trace.yaml", line),),
    )


def _all_edges() -> tuple[TraceEdge, ...]:
    return (
        _edge(TraceRelation.HAS_SCENARIO, TraceNodeType.REQUIREMENT, "FR-001", TraceNodeType.SCENARIO, "AC-001", 1),
        _edge(TraceRelation.DESIGNED_BY, TraceNodeType.REQUIREMENT, "FR-001", TraceNodeType.ADR, "ADR-001", 2),
        _edge(TraceRelation.DESIGNED_BY, TraceNodeType.SCENARIO, "AC-001", TraceNodeType.CONTRACT, "CONTRACT-001", 3),
        _edge(TraceRelation.IMPLEMENTED_BY, TraceNodeType.REQUIREMENT, "FR-001", TraceNodeType.TASK, "TASK-001", 4),
        _edge(TraceRelation.IMPLEMENTED_BY, TraceNodeType.TASK, "TASK-001", TraceNodeType.CODE, "src/signing/service.py#SigningService.sign", 5),
        _edge(TraceRelation.VERIFIED_BY, TraceNodeType.REQUIREMENT, "FR-001", TraceNodeType.TEST, "tests/test_signing.py#test_signs", 6),
        _edge(TraceRelation.THREATENED_BY, TraceNodeType.CONTRACT, "CONTRACT-001", TraceNodeType.THREAT, "THREAT-001", 7),
        _edge(TraceRelation.MITIGATED_BY, TraceNodeType.THREAT, "THREAT-001", TraceNodeType.TASK, "TASK-001", 8),
        _edge(TraceRelation.APPROVED_BY, TraceNodeType.ADR, "ADR-001", TraceNodeType.APPROVAL, "APPROVAL-001", 9),
        _edge(TraceRelation.EVIDENCED_BY, TraceNodeType.TASK, "TASK-001", TraceNodeType.EVIDENCE, "EVIDENCE-001", 10),
        _edge(TraceRelation.CONTAINS, TraceNodeType.COMPONENT, "signing-service", TraceNodeType.CODE, "src/signing/service.py#SigningService.sign", 11),
        _edge(TraceRelation.DEPENDS_ON, TraceNodeType.ADR, "ADR-001", TraceNodeType.RFC, "RFC-001", 12),
        _edge(TraceRelation.REFERENCES, TraceNodeType.CONTRACT, "CONTRACT-001", TraceNodeType.RFC, "RFC-001", 13),
    )


def test_graph_contains_every_required_node_type_and_typed_relationship() -> None:
    graph = TraceGraph(FEATURE, _all_nodes(), _all_edges())

    assert {item.type for item in graph.nodes} == set(TraceNodeType)
    assert {item.relation for item in graph.edges} == set(TraceRelation)
    assert graph.as_dict()["apiVersion"] == TRACE_GRAPH_API_VERSION
    assert graph.sha256.startswith("sha256:")
    assert all(item.provenance for item in graph.nodes)
    assert all(item.provenance for item in graph.edges)


def test_input_order_does_not_change_canonical_json_or_hash() -> None:
    nodes = _all_nodes()
    edges = _all_edges()
    forward = TraceGraph(FEATURE, nodes, edges)
    reverse = TraceGraph(FEATURE, tuple(reversed(nodes)), tuple(reversed(edges)))

    assert forward.to_json() == reverse.to_json()
    assert forward.sha256 == reverse.sha256


def test_identical_duplicate_node_and_edge_merge_all_declaration_provenance() -> None:
    base_node = _node(
        TraceNodeType.REQUIREMENT,
        "FR-001",
        "specs/changes/TRACE-100/requirements.md",
        3,
        label="Sign script",
        metadata={"priority": "critical"},
    )
    duplicate_node = _node(
        TraceNodeType.REQUIREMENT,
        "FR-001",
        "specs/changes/TRACE-100/copied-requirements.md",
        8,
        label="Sign script",
        metadata={"priority": "critical"},
    )
    task = _node(TraceNodeType.TASK, "TASK-001", "specs/changes/TRACE-100/tasks.md", 3)
    first_edge = TraceEdge(
        TraceRelation.IMPLEMENTED_BY,
        base_node.node_id,
        task.node_id,
        (_p("specs/changes/TRACE-100/requirements.md", 3),),
        {"explicit": True},
    )
    duplicate_edge = TraceEdge(
        TraceRelation.IMPLEMENTED_BY,
        base_node.node_id,
        task.node_id,
        (_p("specs/changes/TRACE-100/tasks.md", 3),),
        {"explicit": True},
    )

    graph = TraceGraph(
        FEATURE,
        (base_node, duplicate_node, task),
        (first_edge, duplicate_edge),
    )

    requirement = graph.node_map[base_node.node_id]
    edge = graph.edge_map[first_edge.edge_id]
    assert [(item.source, item.line) for item in requirement.provenance] == [
        ("specs/changes/TRACE-100/copied-requirements.md", 8),
        ("specs/changes/TRACE-100/requirements.md", 3),
    ]
    assert {(item.source, item.line) for item in edge.provenance} == {
        ("specs/changes/TRACE-100/requirements.md", 3),
        ("specs/changes/TRACE-100/tasks.md", 3),
    }


def test_conflicting_duplicate_node_or_edge_fails_closed() -> None:
    first = _node(
        TraceNodeType.REQUIREMENT,
        "FR-001",
        "requirements.md",
        1,
        label="Original",
    )
    conflict = _node(
        TraceNodeType.REQUIREMENT,
        "FR-001",
        "copy.md",
        1,
        label="Different meaning",
    )
    with pytest.raises(TraceGraphError, match="conflicting duplicate trace node"):
        TraceGraph(FEATURE, (first, conflict), ())

    task = _node(TraceNodeType.TASK, "TASK-001", "tasks.md", 1)
    first_edge = TraceEdge(
        TraceRelation.IMPLEMENTED_BY,
        first.node_id,
        task.node_id,
        (_p("requirements.md", 1),),
        {"confidence": "explicit"},
    )
    conflict_edge = TraceEdge(
        TraceRelation.IMPLEMENTED_BY,
        first.node_id,
        task.node_id,
        (_p("tasks.md", 1),),
        {"confidence": "inferred"},
    )
    with pytest.raises(TraceGraphError, match="conflicting duplicate trace edge"):
        TraceGraph(FEATURE, (first, task), (first_edge, conflict_edge))


def test_same_source_line_with_conflicting_declaration_hash_fails_closed() -> None:
    with pytest.raises(TraceGraphError, match="conflicting provenance declaration"):
        TraceNode(
            TraceNodeType.REQUIREMENT,
            "FR-001",
            (
                TraceProvenance("requirements.md", 3, declaration_sha256="sha256:" + "1" * 64),
                TraceProvenance("requirements.md", 3, declaration_sha256="sha256:" + "2" * 64),
            ),
        )


def test_missing_endpoint_and_invalid_relation_endpoint_types_fail_closed() -> None:
    requirement = _node(TraceNodeType.REQUIREMENT, "FR-001", "requirements.md", 1)
    missing = TraceEdge(
        TraceRelation.IMPLEMENTED_BY,
        requirement.node_id,
        trace_node_id(TraceNodeType.TASK, "TASK-404"),
        (_p("requirements.md", 1),),
    )
    with pytest.raises(TraceGraphError, match="missing endpoint"):
        TraceGraph(FEATURE, (requirement,), (missing,))

    test = _node(TraceNodeType.TEST, "TEST-001", "tests.md", 1)
    invalid = TraceEdge(
        TraceRelation.HAS_SCENARIO,
        requirement.node_id,
        test.node_id,
        (_p("requirements.md", 1),),
    )
    with pytest.raises(TraceGraphError, match="does not allow requirement -> test"):
        TraceGraph(FEATURE, (requirement, test), (invalid,))


def test_json_round_trip_validates_canonical_sha_and_rejects_tampering() -> None:
    graph = TraceGraph(FEATURE, _all_nodes(), _all_edges())
    restored = TraceGraph.from_json(graph.to_json())
    assert restored.to_json() == graph.to_json()

    tampered = json.loads(graph.to_json())
    tampered["nodes"][0]["metadata"]["tampered"] = True
    with pytest.raises(TraceGraphError, match="SHA-256 does not match"):
        TraceGraph.from_mapping(tampered)


def test_from_mapping_is_strict_about_types_and_unknown_fields() -> None:
    graph = TraceGraph(FEATURE, _all_nodes(), _all_edges())
    payload = json.loads(graph.to_json())
    payload["nodes"][0]["provenance"][0]["detail"] = 123
    with pytest.raises(TraceGraphError, match="detail"):
        TraceGraph.from_mapping(payload)

    payload = json.loads(graph.to_json())
    payload["unexpected"] = True
    with pytest.raises(TraceGraphError, match="unknown field"):
        TraceGraph.from_mapping(payload)


def test_metadata_is_deep_copied_from_mutable_caller_data() -> None:
    metadata: dict[str, object] = {"tags": ["security", "signing"]}
    node = _node(
        TraceNodeType.REQUIREMENT,
        "FR-001",
        "requirements.md",
        1,
        metadata=metadata,
    )
    graph = TraceGraph(FEATURE, (node,), ())
    original_json = graph.to_json()

    metadata["tags"].append("mutated")  # type: ignore[union-attr]

    assert graph.to_json() == original_json
    stored_tags = graph.node_map[node.node_id].metadata["tags"]
    assert stored_tags == ("security", "signing")
    with pytest.raises(AttributeError):
        stored_tags.append("direct-mutation")  # type: ignore[union-attr]
    with pytest.raises(TypeError):
        graph.node_map[node.node_id].metadata["new"] = "mutation"  # type: ignore[index]
    assert graph.to_json() == original_json


def test_repository_provenance_helper_preserves_unicode_and_rejects_symlinks(
    tmp_path: Path,
) -> None:
    root = tmp_path / "Trace Ω repo"
    source = root / "specs" / "changes" / FEATURE / "café requirements.md"
    source.parent.mkdir(parents=True)
    source.write_text("# FR-001: Sign Δ\n", encoding="utf-8")

    provenance = trace_provenance_for_path(root, source, line=1, detail="café Δ")

    assert provenance.source == f"specs/changes/{FEATURE}/café requirements.md"
    assert provenance.line == 1
    assert provenance.declaration_sha256 == "sha256:" + __import__("hashlib").sha256(source.read_bytes()).hexdigest()

    with pytest.raises(TraceGraphError, match="outside"):
        trace_provenance_for_path(root, source, line=2)

    invalid_utf8 = root / "invalid.bin"
    invalid_utf8.write_bytes(b"\xff\xfe")
    with pytest.raises(TraceGraphError, match="valid UTF-8"):
        trace_provenance_for_path(root, invalid_utf8, line=1)

    outside = tmp_path / "outside.md"
    outside.write_text("outside\n", encoding="utf-8")
    with pytest.raises(TraceGraphError, match="inside the project root"):
        trace_provenance_for_path(root, outside, line=1)

    link = root / "linked.md"
    try:
        link.symlink_to(source)
    except OSError:
        pytest.skip("symlink creation is unavailable on this platform")
    with pytest.raises(TraceGraphError, match="symlink"):
        trace_provenance_for_path(root, link, line=1)
