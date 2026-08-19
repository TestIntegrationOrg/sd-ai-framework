from __future__ import annotations

import json
from typing import Mapping

from sdai.architecture_drift import ApprovedArchitecture
from sdai.trace_graph import TraceEdge, TraceNode, TraceNodeType, TraceProvenance, TraceRelation


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


def architecture_topology_trace_entity(approved: ApprovedArchitecture) -> str:
    return f"architecture-topology:{approved.topology.feature_id}:{approved.topology.topology_id}"


def architecture_component_trace_entity(component_id: str) -> str:
    return f"architecture-component:{component_id}"


def architecture_fact_trace_entity(fact_id: str) -> str:
    return f"architecture-fact:{fact_id}"


def project_approved_architecture(
    approved: ApprovedArchitecture,
) -> tuple[tuple[TraceNode, ...], tuple[TraceEdge, ...]]:
    """Project approved architecture truth into the existing canonical trace model.

    The trace API is not forked or version-bumped: architecture topology, component,
    and fact records are represented as design/component nodes with explicit
    ``architecture_role`` metadata. The topology node advertises the exact evidence
    subject used by typed architecture approval so the existing evidence relation can
    bind it without changing approval authority semantics.
    """
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
        attributes = _plain(fact.attributes)
        # Round-trip once so trace metadata contains only ordinary JSON values.
        attributes = json.loads(json.dumps(attributes, sort_keys=True, separators=(",", ":")))
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


__all__ = [
    "architecture_component_trace_entity",
    "architecture_fact_trace_entity",
    "architecture_topology_trace_entity",
    "project_approved_architecture",
]
