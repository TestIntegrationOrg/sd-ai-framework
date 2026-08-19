from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
from typing import Iterable

from sdai.audit_contracts import (
    AUDIT_EVENTS_RELATIVE_PATH,
    AuditProvenanceError,
    _feature_workspace,
    _safe_component_chain,
)
from sdai.audit_ledger import AuditLedger
from sdai.audit_provenance import AuditEvent
from sdai.audit_trace import AuditTraceError, build_audit_trace_index
from sdai.trace_builder import TraceBuildError, TraceGap, build_feature_trace_graph
from sdai.trace_graph import TraceNodeType, TraceRelation, trace_node_id


AUDIT_REPORT_API_VERSION = "sdai.audit-report/v1"
AUDIT_REPORT_MAX_EVENTS = 500
_ZERO_HASH = "sha256:" + ("0" * 64)
_EMPTY_EXPORT_SHA = "sha256:" + sha256(b"").hexdigest()
_AUDIT_GAP_KINDS = frozenset(
    {
        "missing-audit-binding",
        "stale-audit-binding",
        "missing-audit-evidence-node",
    }
)


class AuditReportError(RuntimeError):
    """Raised when a verified bounded audit report cannot be produced safely."""


def _fail(code: str, message: str) -> AuditReportError:
    return AuditReportError(f"{code}: {message}")


def _canonical_bytes(payload: object) -> bytes:
    try:
        return json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise _fail("SDAI-AUDIT-REPORT-001", "audit report is not canonical JSON") from exc


def _hash_json(payload: object) -> str:
    return "sha256:" + sha256(_canonical_bytes(payload)).hexdigest()


def _status(event: AuditEvent) -> str | None:
    value = event.metadata.get("status")
    return value if isinstance(value, str) else None


def _execution_summary(event: AuditEvent) -> dict[str, object]:
    return {
        "runId": event.execution.run_id,
        "workflow": event.execution.workflow,
        "stepId": event.execution.step_id,
        "taskId": event.execution.task_id,
        "gitCommit": event.execution.git_commit,
    }


def _event_summary(event: AuditEvent) -> dict[str, object]:
    return {
        "sequence": event.sequence,
        "eventId": event.event_id,
        "eventSha256": event.sha256,
        "occurredAt": event.occurred_at,
        "category": event.category,
        "actorKind": event.actor.kind,
        "action": event.action.kind,
        "status": _status(event),
        "execution": _execution_summary(event),
        "bindings": [
            {"kind": item.kind, "source": item.source, "sha256": item.sha256}
            for item in event.bindings
        ],
    }


@dataclass(frozen=True, slots=True)
class AuditSelectors:
    category: str | None = None
    actor_kind: str | None = None
    action: str | None = None
    run_id: str | None = None
    workflow: str | None = None
    step_id: str | None = None
    task_id: str | None = None
    binding: str | None = None
    status: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "category": self.category,
            "actorKind": self.actor_kind,
            "action": self.action,
            "runId": self.run_id,
            "workflow": self.workflow,
            "stepId": self.step_id,
            "taskId": self.task_id,
            "binding": self.binding,
            "status": self.status,
        }

    @property
    def active(self) -> bool:
        return any(value is not None for value in self.as_dict().values())


def _binding_matches(event: AuditEvent, selector: str) -> bool:
    return any(selector in {item.kind, item.source, item.sha256} for item in event.bindings)


def _matches(event: AuditEvent, selectors: AuditSelectors) -> bool:
    checks = (
        selectors.category is None or event.category == selectors.category,
        selectors.actor_kind is None or event.actor.kind == selectors.actor_kind,
        selectors.action is None or event.action.kind == selectors.action,
        selectors.run_id is None or event.execution.run_id == selectors.run_id,
        selectors.workflow is None or event.execution.workflow == selectors.workflow,
        selectors.step_id is None or event.execution.step_id == selectors.step_id,
        selectors.task_id is None or event.execution.task_id == selectors.task_id,
        selectors.binding is None or _binding_matches(event, selectors.binding),
        selectors.status is None or _status(event) == selectors.status,
    )
    return all(checks)


def _audit_source(root: Path, events_path: Path) -> str:
    return events_path.relative_to(root).as_posix()


def _selected_node_ids(events: Iterable[AuditEvent]) -> frozenset[str]:
    return frozenset(
        trace_node_id(TraceNodeType.EVIDENCE, f"audit-event:{event.event_id}")
        for event in events
    )


def _gap_dict(gap: TraceGap) -> dict[str, object]:
    return {
        "kind": gap.kind,
        "source": gap.source,
        "line": gap.line,
        "sourceNodeId": gap.source_node_id,
        "target": gap.target,
        "relation": gap.relation,
        "detail": gap.detail,
    }


def _relationship_summary_current(
    root: Path,
    feature_id: str,
    selected: tuple[AuditEvent, ...],
) -> tuple[dict[str, object], bool]:
    selected_ids = _selected_node_ids(selected)
    try:
        build = build_feature_trace_graph(root, feature_id)
    except TraceBuildError as exc:
        return (
            {
                "scope": "canonical-trace",
                "available": False,
                "linkedReferences": 0,
                "gaps": [],
                "errorCode": "SDAI-AUDIT-REPORT-005",
                "errorType": type(exc).__name__,
            },
            True,
        )

    linked = 0
    for edge in build.graph.edges:
        if edge.relation is not TraceRelation.REFERENCES or edge.source not in selected_ids:
            continue
        if edge.metadata.get("audit_trace_role") == "binding-reference":
            linked += 1
    gaps = [
        gap
        for gap in build.gaps
        if gap.kind in _AUDIT_GAP_KINDS
        and (gap.source_node_id is None or gap.source_node_id in selected_ids)
    ]
    return (
        {
            "scope": "canonical-trace",
            "available": True,
            "linkedReferences": linked,
            "gaps": [_gap_dict(gap) for gap in gaps],
            "errorCode": None,
            "errorType": None,
        },
        bool(gaps),
    )


def _relationship_summary_legacy(
    root: Path,
    feature_id: str,
    selected: tuple[AuditEvent, ...],
) -> tuple[dict[str, object], bool]:
    selected_ids = _selected_node_ids(selected)
    try:
        index = build_audit_trace_index(root, feature_id, ())
    except AuditTraceError as exc:
        return (
            {
                "scope": "legacy-audit-projection",
                "available": False,
                "linkedReferences": 0,
                "gaps": [],
                "errorCode": "SDAI-AUDIT-REPORT-005",
                "errorType": type(exc).__name__,
            },
            True,
        )
    linked = sum(
        1
        for edge in index.edges
        if edge.relation is TraceRelation.REFERENCES
        and edge.source in selected_ids
        and edge.metadata.get("audit_trace_role") == "binding-reference"
    )
    # Typed evidence nodes are owned by the modern canonical trace graph. Legacy
    # audit querying still validates exact repository bytes and synthetic hashes,
    # but does not claim typed-evidence subject linkage when that graph is absent.
    gaps = [
        gap
        for gap in index.gaps
        if gap.kind != "missing-audit-evidence-node"
        and (gap.source_node_id is None or gap.source_node_id in selected_ids)
    ]
    payload = {
        "scope": "legacy-audit-projection",
        "available": True,
        "linkedReferences": linked,
        "gaps": [
            {
                "kind": gap.kind,
                "source": gap.source,
                "line": gap.line,
                "sourceNodeId": gap.source_node_id,
                "target": gap.target,
                "relation": gap.relation,
                "detail": gap.detail,
            }
            for gap in gaps
        ],
        "errorCode": None,
        "errorType": None,
    }
    return payload, bool(gaps)


def _relationship_summary(
    root: Path,
    workspace: Path,
    feature_id: str,
    selected: tuple[AuditEvent, ...],
) -> tuple[dict[str, object], bool]:
    modern = root / "specs" / "changes" / feature_id
    if workspace == modern:
        return _relationship_summary_current(root, feature_id, selected)
    return _relationship_summary_legacy(root, feature_id, selected)


@dataclass(frozen=True, slots=True)
class AuditReport:
    body: dict[str, object]
    exit_code: int

    def to_dict(self) -> dict[str, object]:
        return dict(self.body)

    def to_json(self) -> str:
        return _canonical_bytes(self.body).decode("utf-8") + "\n"


def build_audit_report(
    project_root: Path,
    feature_id: str,
    *,
    selectors: AuditSelectors | None = None,
) -> AuditReport:
    root = project_root.resolve()
    selectors = selectors or AuditSelectors()
    try:
        workspace = _feature_workspace(root, feature_id)
        feature = workspace.name
        events_path = _safe_component_chain(
            root,
            workspace / AUDIT_EVENTS_RELATIVE_PATH,
            label="audit report events path",
        )
    except AuditProvenanceError as exc:
        raise _fail("SDAI-AUDIT-REPORT-002", str(exc)) from exc

    source = _audit_source(root, events_path)
    if not events_path.exists():
        body: dict[str, object] = {
            "apiVersion": AUDIT_REPORT_API_VERSION,
            "featureId": feature,
            "status": "no-events",
            "auditSource": source,
            "eventCount": 0,
            "selectedCount": 0,
            "returnedCount": 0,
            "truncated": False,
            "ledgerHeadSha256": _ZERO_HASH,
            "exportSha256": _EMPTY_EXPORT_SHA,
            "selectors": selectors.as_dict(),
            "events": [],
            "relationships": {
                "scope": "none",
                "available": True,
                "linkedReferences": 0,
                "gaps": [],
                "errorCode": None,
                "errorType": None,
            },
        }
        body["reportSha256"] = _hash_json(body)
        return AuditReport(body, 3)

    try:
        ledger = AuditLedger(root, feature)
        snapshot = ledger.verify()
        events = ledger.read()
    except AuditProvenanceError:
        raise

    if snapshot.event_count != len(events):
        raise _fail(
            "SDAI-AUDIT-REPORT-004",
            "audit ledger changed while the report was being read",
        )
    expected_head = events[-1].sha256 if events else _ZERO_HASH
    if snapshot.head_sha256 != expected_head:
        raise _fail(
            "SDAI-AUDIT-REPORT-004",
            "audit ledger head changed while the report was being read",
        )

    selected = tuple(event for event in events if _matches(event, selectors))
    returned = selected[:AUDIT_REPORT_MAX_EVENTS]
    relationships, has_linkage_gaps = _relationship_summary(
        root,
        workspace,
        feature,
        selected,
    )
    body = {
        "apiVersion": AUDIT_REPORT_API_VERSION,
        "featureId": feature,
        "status": "verified" if events else "no-events",
        "auditSource": source,
        "eventCount": len(events),
        "selectedCount": len(selected),
        "returnedCount": len(returned),
        "truncated": len(selected) > len(returned),
        "ledgerHeadSha256": snapshot.head_sha256,
        "exportSha256": snapshot.export_sha256,
        "selectors": selectors.as_dict(),
        "events": [_event_summary(event) for event in returned],
        "relationships": relationships,
    }
    body["reportSha256"] = _hash_json(body)
    if not events:
        exit_code = 3
    elif has_linkage_gaps:
        exit_code = 2
    else:
        exit_code = 0
    return AuditReport(body, exit_code)


__all__ = [
    "AUDIT_REPORT_API_VERSION",
    "AUDIT_REPORT_MAX_EVENTS",
    "AuditReport",
    "AuditReportError",
    "AuditSelectors",
    "build_audit_report",
]
