from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Iterable

from sdai.audit_contracts import AUDIT_EVENTS_RELATIVE_PATH, AuditProvenanceError
from sdai.audit_ledger import AuditLedger
from sdai.audit_provenance import AuditBinding, AuditEvent
from sdai.path_safety import PathSafetyError, ensure_within_project
from sdai.trace_evidence import TRACE_EVIDENCE_API_VERSION, TraceEvidence, TraceEvidenceError
from sdai.trace_graph import (
    TraceEdge,
    TraceNode,
    TraceNodeType,
    TraceProvenance,
    TraceRelation,
    trace_node_id,
)


class AuditTraceError(RuntimeError):
    """Raised when tamper-evident audit provenance cannot be projected safely."""


@dataclass(frozen=True)
class AuditTraceGap:
    kind: str
    source: str
    line: int
    target: str
    relation: str
    source_node_id: str | None = None
    detail: str | None = None


@dataclass(frozen=True)
class AuditTraceIndex:
    nodes: tuple[TraceNode, ...]
    edges: tuple[TraceEdge, ...]
    gaps: tuple[AuditTraceGap, ...]
    event_count: int
    head_sha256: str | None
    export_sha256: str | None


_MAX_TRACE_EVENTS = 20_000
_MAX_TRACE_REFERENCES = 100_000
_EVIDENCE_BINDING_KINDS = frozenset({"evidence", "trace", "quality", "security", "eval"})
_AUTHORITY_PREFIXES = (
    "workflow-engine2/",
    "model-routing/",
    "contract-",
    "architecture-",
    "verification/",
    "verify/",
    "convergence/",
    "promotion/",
    "completion-barrier/",
)
_REPOSITORY_PREFIXES = (
    ".sdai/",
    ".github/",
    "specs/",
    "src/",
    "tests/",
    "docs/",
)
_REPOSITORY_SUFFIXES = frozenset(
    {
        ".json",
        ".jsonl",
        ".yaml",
        ".yml",
        ".md",
        ".txt",
        ".xml",
        ".sarif",
        ".log",
        ".junit",
    }
)


def _fail(code: str, message: str) -> AuditTraceError:
    return AuditTraceError(f"{code}: {message}")


def _hash_bytes(content: bytes) -> str:
    return "sha256:" + sha256(content).hexdigest()


def _entity_hash(*values: str) -> str:
    encoded = "\x00".join(values).encode("utf-8")
    return sha256(encoded).hexdigest()


def _audit_workspace(root: Path, feature_id: str) -> Path | None:
    modern = root / "specs" / "changes" / feature_id
    legacy = root / "specs" / feature_id
    modern_exists = modern.exists()
    legacy_exists = legacy.exists()
    if modern_exists and legacy_exists:
        raise _fail(
            "SDAI-TRACE-AUDIT-001",
            f"feature {feature_id!r} has both current and legacy workspaces; audit trace authority is ambiguous",
        )
    workspace = modern if modern_exists else legacy if legacy_exists else None
    if workspace is None:
        return None
    if workspace.is_symlink() or not workspace.is_dir():
        raise _fail("SDAI-TRACE-AUDIT-001", "audit feature workspace must be a regular non-symlink directory")
    return workspace


def _audit_source(root: Path, workspace: Path) -> tuple[Path, str]:
    path = workspace / AUDIT_EVENTS_RELATIVE_PATH
    try:
        safe = ensure_within_project(root, path, label="audit trace ledger")
    except PathSafetyError as exc:
        raise _fail("SDAI-TRACE-AUDIT-001", "audit ledger must remain inside the project root") from exc
    return safe, safe.relative_to(root).as_posix()


def _event_node(event: AuditEvent, source: str) -> TraceNode:
    status = event.metadata.get("status")
    return TraceNode(
        type=TraceNodeType.EVIDENCE,
        entity_id=f"audit-event:{event.event_id}",
        label=event.action.kind,
        metadata={
            "audit_trace_role": "event",
            "event_id": event.event_id,
            "event_sha256": event.sha256,
            "sequence": event.sequence,
            "category": event.category,
            "action": event.action.kind,
            "status": status if isinstance(status, str) else None,
        },
        provenance=(
            TraceProvenance(
                source=source,
                line=event.sequence,
                detail="validated sdai.audit-event/v1 ledger record",
            ),
        ),
    )


def _ledger_node(
    feature_id: str,
    source: str,
    *,
    event_count: int,
    head_sha256: str,
    export_sha256: str,
) -> TraceNode:
    return TraceNode(
        type=TraceNodeType.EVIDENCE,
        entity_id=f"audit-ledger:{feature_id}",
        label="audit-ledger",
        metadata={
            "audit_trace_role": "ledger",
            "event_count": event_count,
            "head_sha256": head_sha256,
            "export_sha256": export_sha256,
        },
        provenance=(
            TraceProvenance(
                source=source,
                line=1,
                detail="verified tamper-evident audit ledger",
            ),
        ),
    )


def _safe_repo_file(root: Path, source: str) -> Path | None:
    try:
        safe = ensure_within_project(root, root / PurePosixPath(source), label="audit trace binding")
    except (PathSafetyError, ValueError) as exc:
        raise _fail("SDAI-TRACE-AUDIT-002", f"audit binding escapes project root: {source!r}") from exc
    relative = safe.relative_to(root)
    current = root
    for part in relative.parts:
        current = current / part
        if current.exists() and current.is_symlink():
            raise _fail(
                "SDAI-TRACE-AUDIT-002",
                f"audit binding contains a symlink component: {source!r}",
            )
    if not safe.exists():
        return None
    if safe.is_symlink() or not safe.is_file():
        raise _fail(
            "SDAI-TRACE-AUDIT-002",
            f"audit binding must resolve to a regular non-symlink file: {source!r}",
        )
    return safe


def _looks_repository_path(source: str) -> bool:
    path = PurePosixPath(source)
    return source.startswith(_REPOSITORY_PREFIXES) or path.suffix.casefold() in _REPOSITORY_SUFFIXES


def _is_interesting_synthetic(binding: AuditBinding) -> bool:
    return binding.kind in _EVIDENCE_BINDING_KINDS or binding.source.startswith(_AUTHORITY_PREFIXES)


def _binding_node(
    binding: AuditBinding,
    *,
    provenance: TraceProvenance,
    local_assertion_only: bool = False,
) -> TraceNode:
    return TraceNode(
        type=TraceNodeType.EVIDENCE,
        entity_id="audit-binding:" + _entity_hash(binding.kind, binding.source, binding.sha256),
        label=f"{binding.kind}:{PurePosixPath(binding.source).name}",
        metadata={
            "audit_trace_role": "binding",
            "binding_kind": binding.kind,
            "source": binding.source,
            "sha256": binding.sha256,
            "artifact_provenance": {
                "source": binding.source,
                "line": 1,
                "declaration_sha256": binding.sha256,
            },
            "local_assertion_only": local_assertion_only,
        },
        provenance=(provenance,),
    )


def _edge_metadata(bindings: Iterable[AuditBinding], *, role: str) -> dict[str, object]:
    rows = [
        {"kind": item.kind, "source": item.source, "sha256": item.sha256}
        for item in sorted(bindings, key=lambda item: (item.kind, item.source, item.sha256))
    ]
    return {"audit_trace_role": role, "bindings": rows}


def _gap(
    event: AuditEvent,
    source: str,
    binding: AuditBinding,
    *,
    kind: str,
    detail: str,
) -> AuditTraceGap:
    return AuditTraceGap(
        kind=kind,
        source=source,
        line=event.sequence,
        source_node_id=trace_node_id(TraceNodeType.EVIDENCE, f"audit-event:{event.event_id}"),
        target=binding.source,
        relation=TraceRelation.REFERENCES.value,
        detail=detail,
    )


def build_audit_trace_index(
    project_root: Path,
    feature_id: str,
    existing_nodes: tuple[TraceNode, ...],
) -> AuditTraceIndex:
    """Project a verified audit chain into the existing trace graph.

    Audit remains provenance only. This function never evaluates approvals, promotion,
    verification, contracts, architecture, or workflow decisions. It verifies the
    audit chain and exact referenced bytes, then emits ordinary trace nodes/edges/gaps.
    """

    root = project_root.resolve()
    workspace = _audit_workspace(root, feature_id)
    if workspace is None:
        return AuditTraceIndex((), (), (), 0, None, None)
    events_path, source = _audit_source(root, workspace)
    if not events_path.exists():
        return AuditTraceIndex((), (), (), 0, None, None)
    if events_path.is_symlink() or not events_path.is_file():
        raise _fail("SDAI-TRACE-AUDIT-001", "audit events must be a regular non-symlink file")

    try:
        ledger = AuditLedger(root, feature_id)
        snapshot = ledger.verify()
        events = ledger.read()
    except AuditProvenanceError as exc:
        raise _fail("SDAI-TRACE-AUDIT-001", f"audit ledger integrity verification failed: {exc}") from exc

    if not events:
        return AuditTraceIndex((), (), (), 0, snapshot.head_sha256, snapshot.export_sha256)
    if snapshot.event_count != len(events) or snapshot.head_sha256 != events[-1].sha256:
        raise _fail("SDAI-TRACE-AUDIT-001", "audit snapshot does not match verified event sequence")
    if len(events) > _MAX_TRACE_EVENTS:
        raise _fail(
            "SDAI-TRACE-AUDIT-004",
            f"audit trace projection exceeds {_MAX_TRACE_EVENTS} events; compact/export before graph projection",
        )
    total_bindings = sum(len(event.bindings) for event in events)
    if total_bindings > _MAX_TRACE_REFERENCES:
        raise _fail(
            "SDAI-TRACE-AUDIT-004",
            f"audit trace projection exceeds {_MAX_TRACE_REFERENCES} binding references",
        )

    nodes: list[TraceNode] = []
    edges: list[TraceEdge] = []
    gaps: list[AuditTraceGap] = []

    ledger_node = _ledger_node(
        feature_id,
        source,
        event_count=snapshot.event_count,
        head_sha256=snapshot.head_sha256,
        export_sha256=snapshot.export_sha256,
    )
    nodes.append(ledger_node)

    event_nodes = {event.sha256: _event_node(event, source) for event in events}
    event_by_node_id = {event_nodes[event.sha256].node_id: event for event in events}
    nodes.extend(event_nodes[event.sha256] for event in events)
    head_node = event_nodes[events[-1].sha256]
    edges.append(
        TraceEdge(
            relation=TraceRelation.REFERENCES,
            source=ledger_node.node_id,
            target=head_node.node_id,
            provenance=(
                TraceProvenance(source=source, line=events[-1].sequence, detail="verified audit ledger head"),
            ),
            metadata={"audit_trace_role": "ledger-head", "head_sha256": snapshot.head_sha256},
        )
    )

    existing = {node.node_id: node for node in existing_nodes}
    binding_nodes: dict[str, TraceNode] = {}
    edge_bindings: dict[tuple[str, str], list[AuditBinding]] = {}

    def link(source_node: str, target_node: str, binding: AuditBinding) -> None:
        edge_bindings.setdefault((source_node, target_node), []).append(binding)

    for event in events:
        event_node = event_nodes[event.sha256]
        for binding in event.bindings:
            referenced_event = event_nodes.get(binding.sha256)
            if referenced_event is not None and referenced_event.node_id != event_node.node_id:
                link(event_node.node_id, referenced_event.node_id, binding)
                continue

            path = _safe_repo_file(root, binding.source)
            if path is None:
                if _looks_repository_path(binding.source):
                    gaps.append(
                        _gap(
                            event,
                            source,
                            binding,
                            kind="missing-audit-binding",
                            detail="audit-bound repository evidence is missing",
                        )
                    )
                    continue
                if not _is_interesting_synthetic(binding):
                    continue
                provenance = TraceProvenance(
                    source=source,
                    line=event.sequence,
                    detail="hash-only deterministic authority binding from audit event",
                )
                node = _binding_node(binding, provenance=provenance)
                binding_nodes.setdefault(node.node_id, node)
                link(event_node.node_id, node.node_id, binding)
                continue

            try:
                content = path.read_bytes()
            except OSError as exc:
                raise _fail(
                    "SDAI-TRACE-AUDIT-002",
                    f"unable to read audit binding {binding.source!r}: {exc}",
                ) from exc
            current_sha = _hash_bytes(content)
            if current_sha != binding.sha256:
                gaps.append(
                    _gap(
                        event,
                        source,
                        binding,
                        kind="stale-audit-binding",
                        detail="audit-bound repository evidence SHA-256 no longer matches current bytes",
                    )
                )
                continue

            if TRACE_EVIDENCE_API_VERSION.encode("utf-8") in content:
                try:
                    evidence = TraceEvidence.from_json(content)
                except TraceEvidenceError as exc:
                    raise _fail(
                        "SDAI-TRACE-AUDIT-003",
                        f"audit-bound typed trace evidence is invalid: {binding.source!r}: {exc}",
                    ) from exc
                target_id = trace_node_id(TraceNodeType.EVIDENCE, evidence.evidence_id)
                if target_id not in existing:
                    gaps.append(
                        _gap(
                            event,
                            source,
                            binding,
                            kind="missing-audit-evidence-node",
                            detail="validated audit-bound trace evidence is absent from canonical trace graph",
                        )
                    )
                    continue
                link(event_node.node_id, target_id, binding)
                continue

            if binding.kind not in _EVIDENCE_BINDING_KINDS:
                continue
            local_assertion = "/approvals/" in f"/{binding.source}"
            provenance = TraceProvenance(
                source=source,
                line=event.sequence,
                detail=(
                    "local approval assertion referenced by audit; enterprise identity not verified"
                    if local_assertion
                    else "exact audit-bound evidence artifact reference"
                ),
            )
            node = _binding_node(
                binding,
                provenance=provenance,
                local_assertion_only=local_assertion,
            )
            binding_nodes.setdefault(node.node_id, node)
            link(event_node.node_id, node.node_id, binding)

    nodes.extend(sorted(binding_nodes.values(), key=lambda item: item.node_id))
    for (source_node, target_node), bindings in sorted(edge_bindings.items()):
        edge_source = event_by_node_id[source_node]
        edges.append(
            TraceEdge(
                relation=TraceRelation.REFERENCES,
                source=source_node,
                target=target_node,
                provenance=(
                    TraceProvenance(
                        source=source,
                        line=edge_source.sequence,
                        detail="verified audit binding reference",
                    ),
                ),
                metadata=_edge_metadata(bindings, role="binding-reference"),
            )
        )

    return AuditTraceIndex(
        nodes=tuple(nodes),
        edges=tuple(edges),
        gaps=tuple(
            sorted(
                gaps,
                key=lambda item: (
                    item.kind,
                    item.source.casefold(),
                    item.source,
                    item.line,
                    item.target,
                    item.detail or "",
                ),
            )
        ),
        event_count=snapshot.event_count,
        head_sha256=snapshot.head_sha256,
        export_sha256=snapshot.export_sha256,
    )


__all__ = [
    "AuditTraceError",
    "AuditTraceGap",
    "AuditTraceIndex",
    "build_audit_trace_index",
]
