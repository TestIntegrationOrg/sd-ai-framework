from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
import re

from sdai.agent_platform.models import Capability, ExecutionMode
from sdai.agent_platform.routing_diagnostics import load_routing_diagnostic
from sdai.audit_readonly import read_verified_audit
from sdai.context_explain import build_context_explanation
from sdai.diagnostic_evidence import (
    DiagnosticEvidenceError,
    read_provider_attempts,
    read_retry_executions,
    selected_attempt_ids,
)
from sdai.models import FeatureContext, validate_feature_id
from sdai.path_safety import PathSafetyError, ensure_within_project


DIAGNOSTICS_API_VERSION = "sdai.diagnostics/v1"
_MAX_AUDIT_IDENTIFIERS = 50
_SELECTOR_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class DiagnosticsError(RuntimeError):
    """Raised when unified diagnostics are invalid, corrupt, or unsafe."""


def _fail(code: str, message: str) -> DiagnosticsError:
    return DiagnosticsError(f"{code}: {message}")


def _canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise _fail("SDAI-DIAGNOSTICS-001", "diagnostics value is not canonical JSON") from exc


def _sha(value: object) -> str:
    return "sha256:" + sha256(_canonical_bytes(value)).hexdigest()


def _safe(root: Path, candidate: Path, *, label: str) -> Path:
    try:
        safe = ensure_within_project(root, candidate, label=label)
    except PathSafetyError as exc:
        raise _fail("SDAI-DIAGNOSTICS-002", f"{label} escapes project root") from exc
    resolved_root = root.resolve()
    current = resolved_root
    try:
        relative = safe.relative_to(resolved_root)
    except ValueError:
        relative = safe.resolve(strict=False).relative_to(resolved_root)
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise _fail("SDAI-DIAGNOSTICS-002", f"{label} contains a symlink component")
    return safe


def _selector(value: str | None, *, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or _SELECTOR_ID.fullmatch(value) is None:
        raise _fail("SDAI-DIAGNOSTICS-001", f"{label} must be a safe portable identifier")
    return value


def _normalize_provider_attempts(
    attempts: tuple[dict[str, object], ...],
) -> tuple[dict[str, object], ...]:
    """Expose the established provider field using its precise document-hash name."""
    normalized: list[dict[str, object]] = []
    for attempt in attempts:
        row = dict(attempt)
        # diagnostic_evidence predates the unified report naming and currently calls
        # this value routingDecisionSha256. ProviderDiagnosticEvent's canonical field
        # is routingDecisionDocumentSha256: hash of the exact serialized routing JSON.
        document_sha = row.pop("routingDecisionSha256", None)
        row["routingDecisionDocumentSha256"] = document_sha
        normalized.append(row)
    return tuple(normalized)


def _routing_summary(
    root: Path,
    feature_id: str,
    attempts: tuple[dict[str, object], ...],
) -> dict[str, object]:
    routed = [
        item
        for item in attempts
        if isinstance(item.get("routingDecisionDocumentSha256"), str)
    ]
    if not routed:
        return {
            "available": False,
            "routingDecisionDocumentSha256": None,
            "decisionSha256": None,
            "reason": "no-routed-provider-attempt",
        }
    latest = routed[-1]
    routing_document_sha = latest.get("routingDecisionDocumentSha256")
    assert isinstance(routing_document_sha, str)
    document = load_routing_diagnostic(root, feature_id, routing_document_sha)
    if document is None:
        return {
            "available": False,
            "routingDecisionDocumentSha256": routing_document_sha,
            "decisionSha256": None,
            "reason": "historical-routing-document-not-persisted",
        }
    decision = document.get("decision")
    if not isinstance(decision, dict):
        raise _fail("SDAI-DIAGNOSTICS-003", "routing diagnostic decision is invalid")
    selected_profile = decision.get("selected_profile")
    if selected_profile != latest.get("profile"):
        raise _fail("SDAI-DIAGNOSTICS-003", "routing decision/profile correlation mismatch")

    selected_candidate: dict[str, object] | None = None
    candidates = decision.get("candidates")
    if isinstance(candidates, list):
        for candidate in candidates:
            if isinstance(candidate, dict) and candidate.get("profile") == selected_profile:
                selected_candidate = {
                    "profile": candidate.get("profile"),
                    "provider": candidate.get("provider"),
                    "model": candidate.get("model"),
                    "reasons": candidate.get("reasons"),
                    "rank": candidate.get("rank"),
                    "healthState": candidate.get("health_state"),
                    "observedLatencyNs": candidate.get("observed_latency_ns"),
                }
                break

    request = decision.get("request")
    request_summary: dict[str, object] | None = None
    if isinstance(request, dict):
        request_summary = {
            key: request.get(key)
            for key in (
                "semantic_role",
                "capability",
                "risk",
                "complexity",
                "context_chars",
                "max_cost_class",
                "requested_profile",
                "requested_provider",
                "requested_model",
                "stage",
                "optimization",
                "fallback_profiles",
            )
            if key in request
        }

    decision_sha = document.get("routingDecisionSha256")
    if decision_sha != decision.get("sha256"):
        raise _fail("SDAI-DIAGNOSTICS-003", "routing diagnostic decision identity mismatch")
    return {
        "available": True,
        "routingDecisionDocumentSha256": routing_document_sha,
        "decisionSha256": decision_sha,
        "selectionReason": decision.get("selection_reason"),
        "selectedProfile": selected_profile,
        "defaultProfile": decision.get("default_profile"),
        "request": request_summary,
        "selectedCandidate": selected_candidate,
        "documentSha256": document.get("documentSha256"),
    }


def _context_summary(
    root: Path,
    feature_id: str,
    attempts: tuple[dict[str, object], ...],
) -> dict[str, object]:
    basis = "current-repository-state"
    capability = Capability.CODING
    profile_name: str | None = None
    agent_name: str | None = None
    mode = ExecutionMode.ADVISORY
    source = "default-coding"
    if attempts:
        latest = attempts[-1]
        try:
            capability = Capability(str(latest.get("capability")))
            mode = ExecutionMode(str(latest.get("mode")))
        except ValueError as exc:
            raise _fail("SDAI-DIAGNOSTICS-003", "provider diagnostic capability/mode is invalid") from exc
        profile = latest.get("profile")
        agent = latest.get("semanticAgent")
        profile_name = profile if isinstance(profile, str) and profile else None
        agent_name = agent if isinstance(agent, str) and agent else None
        source = "latest-provider-attempt"
    try:
        explanation = build_context_explanation(
            root,
            feature_id,
            capability,
            profile_name=profile_name,
            agent_name=agent_name,
            mode=mode,
        )
    except Exception as exc:
        return {
            "available": False,
            "basis": basis,
            "capabilitySource": source,
            "capability": capability.value,
            "profile": profile_name,
            "agent": agent_name,
            "reason": "current-context-could-not-be-explained",
            "errorType": type(exc).__name__,
        }
    payload = explanation.as_dict()
    return {
        "available": True,
        "basis": basis,
        "capabilitySource": source,
        "capability": payload.get("capability"),
        "mode": payload.get("mode"),
        "workspace": payload.get("workspace"),
        "profile": payload.get("profile"),
        "provider": payload.get("provider"),
        "agent": payload.get("agent"),
        "contextPlan": payload.get("contextPlan"),
        "metrics": payload.get("metrics"),
        "reportSha256": payload.get("reportSha256"),
    }


def _audit_summary(body: dict[str, object]) -> dict[str, object]:
    events = body.get("events")
    identifiers: list[dict[str, object]] = []
    if isinstance(events, list):
        for event in events[-_MAX_AUDIT_IDENTIFIERS:]:
            if not isinstance(event, dict):
                continue
            identifiers.append(
                {
                    "sequence": event.get("sequence"),
                    "eventId": event.get("eventId"),
                    "eventSha256": event.get("eventSha256"),
                    "occurredAt": event.get("occurredAt"),
                    "action": event.get("action"),
                    "status": event.get("status"),
                    "execution": event.get("execution"),
                    "bindings": event.get("bindings"),
                }
            )
    return {
        "status": body.get("status"),
        "eventCount": body.get("eventCount"),
        "selectedCount": body.get("selectedCount"),
        "returnedCount": body.get("returnedCount"),
        "truncated": body.get("truncated"),
        "recoverableCrashTailBytes": body.get("recoverableCrashTailBytes"),
        "ledgerHeadSha256": body.get("ledgerHeadSha256"),
        "exportSha256": body.get("exportSha256"),
        "reportSha256": body.get("reportSha256"),
        "events": identifiers,
    }


@dataclass(frozen=True)
class DiagnosticsReport:
    body: dict[str, object]
    exit_code: int

    def to_dict(self) -> dict[str, object]:
        return dict(self.body)

    def to_json(self) -> str:
        return _canonical_bytes(self.body).decode("utf-8") + "\n"


def build_diagnostics_report(
    project_root: Path,
    feature_id: str,
    *,
    run_id: str | None = None,
    task_id: str | None = None,
) -> DiagnosticsReport:
    """Build one deterministic read-only operator report without provider execution."""
    root = project_root.resolve()
    feature = validate_feature_id(feature_id)
    run = _selector(run_id, label="run_id")
    task = _selector(task_id, label="task_id")
    workspace = FeatureContext(root, feature).feature_dir
    if not workspace.exists() or workspace.is_symlink() or not workspace.is_dir():
        raise _fail("SDAI-DIAGNOSTICS-002", "feature workspace is missing or unsafe")
    _safe(root, workspace, label="diagnostics feature workspace")

    # This reader deliberately bypasses AuditLedger because AuditLedger constructs
    # directories/lock files. It validates the exact 0.19 event contract and hash
    # chain directly, recognizing recoverable crash tails without truncating them.
    audit_body = read_verified_audit(root, feature, run_id=run, task_id=task)
    filters_active = run is not None or task is not None
    selected_attempts: tuple[str, ...] | None = None
    correlation_truncated = False
    if filters_active:
        selected_attempts = selected_attempt_ids(audit_body)
        correlation_truncated = bool(audit_body.get("truncated"))

    try:
        raw_attempts = read_provider_attempts(
            root,
            feature,
            workspace,
            selected=selected_attempts,
        )
        retry = read_retry_executions(
            root,
            workspace,
            selected_attempts=selected_attempts,
        )
    except DiagnosticEvidenceError as exc:
        raise _fail("SDAI-DIAGNOSTICS-003", str(exc)) from exc

    attempts = _normalize_provider_attempts(raw_attempts)
    routing = _routing_summary(root, feature, attempts)
    context = _context_summary(root, feature, attempts)
    audit_summary = _audit_summary(audit_body)

    partial_reasons: list[str] = []
    if int(audit_body.get("recoverableCrashTailBytes") or 0) > 0:
        partial_reasons.append("recoverable-audit-crash-tail")
    if correlation_truncated:
        partial_reasons.append("audit-selector-correlation-truncated")
    if (
        attempts
        and not routing.get("available")
        and routing.get("routingDecisionDocumentSha256") is not None
    ):
        partial_reasons.append("routing-document-hash-only")
    if not context.get("available"):
        partial_reasons.append("current-context-unavailable")
    if any(not bool(item.get("complete")) for item in retry):
        partial_reasons.append("retry-evidence-in-progress")

    has_data = bool(attempts or retry or int(audit_body.get("selectedCount") or 0))
    status = "partial" if partial_reasons else ("available" if has_data else "no-data")
    body: dict[str, object] = {
        "apiVersion": DIAGNOSTICS_API_VERSION,
        "featureId": feature,
        "workspace": workspace.relative_to(root).as_posix(),
        "status": status,
        "selectors": {"runId": run, "taskId": task},
        "context": context,
        "routing": routing,
        "providerAttempts": list(attempts),
        "retryExecutions": list(retry),
        "audit": audit_summary,
        "partialReasons": partial_reasons,
    }
    body["reportSha256"] = _sha(body)
    return DiagnosticsReport(body, 3 if status == "no-data" else 0)


__all__ = [
    "DIAGNOSTICS_API_VERSION",
    "DiagnosticsError",
    "DiagnosticsReport",
    "build_diagnostics_report",
]
