from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
from typing import Iterable, Mapping

from sdai.architecture_drift import (
    ApprovedArchitecture,
    ArchitectureDriftError,
    ArchitectureDriftFinding,
    architecture_topology_path,
    load_approved_architecture,
)
from sdai.trace_graph import (
    TraceEdge,
    TraceGraph,
    TraceNode,
    TraceNodeType,
    TraceProvenance,
    TraceRelation,
)


def _plain(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    if isinstance(value, list):
        return [_plain(item) for item in value]
    return value


def _topology_provenance(approved: ApprovedArchitecture, detail: str) -> tuple[TraceProvenance, ...]:
    return (
        TraceProvenance(
            approved.topology.source,
            1,
            detail=detail,
            declaration_sha256=approved.topology.file_sha256,
        ),
    )


def _merge_provenance(values: Iterable[TraceProvenance]) -> tuple[TraceProvenance, ...]:
    by_location: dict[tuple[str, int], TraceProvenance] = {}
    for value in values:
        previous = by_location.get(value.location)
        if previous is None:
            by_location[value.location] = value
            continue
        by_location[value.location] = min(
            (previous, value),
            key=lambda item: (item.declaration_sha256 or "", item.detail or ""),
        )
    return tuple(
        sorted(
            by_location.values(),
            key=lambda item: (
                item.source.casefold(),
                item.source,
                item.line,
                item.declaration_sha256 or "",
                item.detail or "",
            ),
        )
    )


def architecture_topology_trace_entity(approved: ApprovedArchitecture) -> str:
    return f"architecture-topology:{approved.topology.feature_id}:{approved.topology.topology_id}"


def architecture_component_trace_entity(component_id: str) -> str:
    return f"architecture-component:{component_id}"


def architecture_fact_trace_entity(fact_id: str) -> str:
    return f"architecture-fact:{fact_id}"


def _finding_entity(finding: ArchitectureDriftFinding) -> str:
    payload = json.dumps(
        finding.to_dict(),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return "architecture-drift:" + sha256(payload.encode("utf-8")).hexdigest()[:24]


def project_approved_architecture(
    approved: ApprovedArchitecture,
) -> tuple[tuple[TraceNode, ...], tuple[TraceEdge, ...]]:
    """Project approved architecture truth into the existing canonical trace model."""
    topology_entity = architecture_topology_trace_entity(approved)
    topology_node = TraceNode(
        type=TraceNodeType.COMPONENT,
        entity_id=topology_entity,
        label=f"Approved architecture {approved.topology.topology_id}",
        metadata={
            "architecture_role": "topology",
            "feature_id": approved.topology.feature_id,
            "topology_id": approved.topology.topology_id,
            "topology_sha256": approved.topology.sha256,
            "approval_truth_sha256": approved.approval.truth_sha256,
            "evidence_subject": approved.topology.subject,
        },
        provenance=_topology_provenance(approved, "approved architecture topology"),
    )

    nodes: list[TraceNode] = [topology_node]
    edges: list[TraceEdge] = []
    component_nodes: dict[str, TraceNode] = {}
    for component in approved.topology.components:
        node = TraceNode(
            type=TraceNodeType.COMPONENT,
            entity_id=architecture_component_trace_entity(component.component_id),
            label=component.component_id,
            metadata={
                "architecture_role": "component",
                "component_id": component.component_id,
                "roots": list(component.roots),
                "module_prefixes": list(component.module_prefixes),
                "topology_sha256": approved.topology.sha256,
            },
            provenance=_topology_provenance(
                approved,
                f"approved architecture component {component.component_id}",
            ),
        )
        component_nodes[component.component_id] = node
        nodes.append(node)
        edges.append(
            TraceEdge(
                relation=TraceRelation.REFERENCES,
                source=topology_node.node_id,
                target=node.node_id,
                provenance=_topology_provenance(
                    approved,
                    f"topology contains component {component.component_id}",
                ),
                metadata={"architecture_relation": "contains-component"},
            )
        )

    for fact in approved.topology.facts:
        attributes = json.loads(
            json.dumps(_plain(fact.attributes), sort_keys=True, separators=(",", ":"))
        )
        node = TraceNode(
            type=TraceNodeType.COMPONENT,
            entity_id=architecture_fact_trace_entity(fact.fact_id),
            label=fact.fact_id,
            metadata={
                "architecture_role": "fact",
                "fact_id": fact.fact_id,
                "fact_kind": fact.kind.value,
                "fact_mode": fact.mode.value,
                "source": fact.source,
                "target": fact.target,
                "attributes": attributes,
                "semantic_sha256": fact.semantic_key,
                "topology_sha256": approved.topology.sha256,
            },
            provenance=fact.provenance,
        )
        nodes.append(node)
        edges.append(
            TraceEdge(
                relation=TraceRelation.REFERENCES,
                source=topology_node.node_id,
                target=node.node_id,
                provenance=fact.provenance,
                metadata={"architecture_relation": "declares-fact", "fact_kind": fact.kind.value},
            )
        )
        for endpoint_role, endpoint in (("source", fact.source), ("target", fact.target)):
            component_node = component_nodes.get(endpoint)
            if component_node is None:
                continue
            edges.append(
                TraceEdge(
                    relation=TraceRelation.REFERENCES,
                    source=node.node_id,
                    target=component_node.node_id,
                    provenance=fact.provenance,
                    metadata={
                        "architecture_relation": f"fact-{endpoint_role}",
                        "fact_id": fact.fact_id,
                    },
                )
            )

    return tuple(nodes), tuple(edges)


def project_architecture_drift_findings(
    approved: ApprovedArchitecture,
    findings: Iterable[ArchitectureDriftFinding],
    *,
    report_sha256: str,
) -> tuple[tuple[TraceNode, ...], tuple[TraceEdge, ...]]:
    topology_node_id = TraceNode(
        type=TraceNodeType.COMPONENT,
        entity_id=architecture_topology_trace_entity(approved),
        label="architecture topology identity",
        metadata={"architecture_role": "topology-identity"},
        provenance=_topology_provenance(approved, "approved architecture topology"),
    ).node_id
    nodes: list[TraceNode] = []
    edges: list[TraceEdge] = []
    for finding in findings:
        provenance = _merge_provenance((*finding.approved_provenance, *finding.observed_provenance))
        if not provenance:
            provenance = _topology_provenance(approved, "architecture drift finding")
        node = TraceNode(
            type=TraceNodeType.COMPONENT,
            entity_id=_finding_entity(finding),
            label=finding.code,
            metadata={
                "architecture_role": "drift-finding",
                "code": finding.code,
                "severity": finding.severity.value,
                "fact_kind": finding.kind.value,
                "source": finding.source,
                "target": finding.target,
                "approved_fact_id": finding.approved_fact_id,
                "report_sha256": report_sha256,
            },
            provenance=provenance,
        )
        nodes.append(node)
        edges.append(
            TraceEdge(
                relation=TraceRelation.REFERENCES,
                source=topology_node_id,
                target=node.node_id,
                provenance=provenance,
                metadata={"architecture_relation": "drift-finding"},
            )
        )
    return tuple(nodes), tuple(edges)


def build_feature_trace_graph_with_architecture(
    project_root: Path,
    feature_id: str,
    *,
    environ: Mapping[str, str] | None = None,
):
    """Augment the existing TraceGraph with governed architecture truth and drift.

    The base trace builder remains the single graph authority. This wrapper only adds
    canonical nodes/edges and returns the same ``TraceBuildResult`` type.
    """
    from sdai.architecture_engine import evaluate_architecture_drift
    from sdai.trace_builder import TraceBuildResult, build_feature_trace_graph

    root = project_root.resolve()
    base = build_feature_trace_graph(root, feature_id, environ=environ)
    topology_path = architecture_topology_path(root, feature_id)
    if not topology_path.exists() or topology_path.is_symlink():
        return base
    try:
        approved = load_approved_architecture(root, feature_id)
    except ArchitectureDriftError as exc:
        # Verification/governance owns stale or mismatched approval decisions. An
        # invalid approval must never be projected as approved trace truth.
        if str(exc).startswith("SDAI-ARCH-DRIFT-005:"):
            return base
        raise

    architecture_nodes, architecture_edges = project_approved_architecture(approved)
    topology_node = next(
        node for node in architecture_nodes if node.metadata.get("architecture_role") == "topology"
    )

    approval_node = next(
        (
            node
            for node in base.graph.nodes
            if node.type is TraceNodeType.EVIDENCE and node.entity_id == approved.approval.evidence_id
        ),
        None,
    )
    if approval_node is not None:
        architecture_edges = (
            *architecture_edges,
            TraceEdge(
                relation=TraceRelation.EVIDENCED_BY,
                source=topology_node.node_id,
                target=approval_node.node_id,
                provenance=approval_node.provenance,
                metadata={
                    "architecture_relation": "approved-by",
                    "approval_truth_sha256": approved.approval.truth_sha256,
                },
            ),
        )

    evaluation = evaluate_architecture_drift(root, feature_id, environ=environ)
    if evaluation.report is not None:
        finding_nodes, finding_edges = project_architecture_drift_findings(
            approved,
            evaluation.report.findings,
            report_sha256=evaluation.report.sha256,
        )
        architecture_nodes = (*architecture_nodes, *finding_nodes)
        architecture_edges = (*architecture_edges, *finding_edges)

    graph = TraceGraph(
        feature_id=base.graph.feature_id,
        nodes=(*base.graph.nodes, *architecture_nodes),
        edges=(*base.graph.edges, *architecture_edges),
    )
    gaps = tuple(
        gap
        for gap in base.gaps
        if not (
            gap.kind == "missing-evidence-subject"
            and gap.target == approved.topology.subject
        )
    )
    return TraceBuildResult(graph=graph, gaps=gaps)


__all__ = [
    "architecture_component_trace_entity",
    "architecture_fact_trace_entity",
    "architecture_topology_trace_entity",
    "build_feature_trace_graph_with_architecture",
    "project_approved_architecture",
    "project_architecture_drift_findings",
]
