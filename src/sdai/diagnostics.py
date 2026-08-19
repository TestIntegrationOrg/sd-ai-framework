from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path, PurePosixPath
import re
from typing import Mapping

from sdai.agent_platform.models import Capability, ExecutionMode
from sdai.agent_platform.provider_diagnostics import PROVIDER_DIAGNOSTIC_API_VERSION
from sdai.agent_platform.provider_retry import PROVIDER_RETRY_API_VERSION
from sdai.agent_platform.routing_diagnostics import load_routing_diagnostic
from sdai.audit_report import AuditSelectors, build_audit_report
from sdai.context_explain import build_context_explanation
from sdai.models import FeatureContext, validate_feature_id
from sdai.path_safety import PathSafetyError, ensure_within_project


DIAGNOSTICS_API_VERSION = "sdai.diagnostics/v1"
_MAX_PROVIDER_ATTEMPTS = 1_000
_MAX_EVENTS_PER_ATTEMPT = 100
_MAX_RETRY_EXECUTIONS = 1_000
_MAX_AUDIT_IDENTIFIERS = 50
_ATTEMPT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SELECTOR_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_TERMINAL_PHASES = frozenset({"completed", "failed", "cancelled"})
_ALLOWED_PHASES = frozenset(
    {"started", "provider-ready", "first-output", "heartbeat", "completed", "failed", "cancelled"}
)


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


def _verified_json_file(
    root: Path,
    path: Path,
    *,
    label: str,
    require_sha: bool = True,
) -> dict[str, object]:
    safe = _safe(root, path, label=label)
    if not safe.is_file():
        raise _fail("SDAI-DIAGNOSTICS-003", f"{label} must be a regular file")
    try:
        raw = safe.read_bytes()
        payload = json.loads(raw.decode("utf-8", errors="strict"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise _fail("SDAI-DIAGNOSTICS-003", f"{label} is not valid UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise _fail("SDAI-DIAGNOSTICS-003", f"{label} must contain a JSON object")
    canonical = _canonical_bytes(payload) + b"\n"
    if raw != canonical:
        raise _fail("SDAI-DIAGNOSTICS-003", f"{label} is not canonical JSON")
    if require_sha:
        claimed = payload.get("sha256")
        body = dict(payload)
        body.pop("sha256", None)
        if not isinstance(claimed, str) or claimed != _sha(body):
            raise _fail("SDAI-DIAGNOSTICS-003", f"{label} failed SHA-256 verification")
    return payload


def _attempt_from_binding(source: object) -> str | None:
    if not isinstance(source, str) or "\\" in source:
        return None
    path = PurePosixPath(source)
    parts = path.parts
    for index in range(len(parts) - 2):
        if parts[index : index + 2] == ("diagnostics", "provider"):
            attempt = parts[index + 2]
            return attempt if _ATTEMPT_ID.fullmatch(attempt) else None
    return None


def _selected_attempt_ids(audit_body: Mapping[str, object]) -> tuple[str, ...]:
    result: list[str] = []
    events = audit_body.get("events")
    if not isinstance(events, list):
        return ()
    for event in events:
        if not isinstance(event, dict):
            continue
        bindings = event.get("bindings")
        if not isinstance(bindings, list):
            continue
        for binding in bindings:
            if not isinstance(binding, dict):
                continue
            attempt = _attempt_from_binding(binding.get("source"))
            if attempt is not None and attempt not in result:
                result.append(attempt)
    return tuple(result)


def _provider_attempt(root: Path, directory: Path, feature_id: str) -> dict[str, object]:
    safe_dir = _safe(root, directory, label="provider diagnostic attempt")
    if not safe_dir.is_dir():
        raise _fail("SDAI-DIAGNOSTICS-003", "provider diagnostic attempt is not a directory")
    attempt_id = safe_dir.name
    if _ATTEMPT_ID.fullmatch(attempt_id) is None:
        raise _fail("SDAI-DIAGNOSTICS-003", "provider diagnostic attempt id is invalid")
    files = sorted(safe_dir.glob("*.json"), key=lambda item: item.name)
    if not files or len(files) > _MAX_EVENTS_PER_ATTEMPT:
        raise _fail("SDAI-DIAGNOSTICS-004", "provider diagnostic attempt event count is invalid")
    events: list[dict[str, object]] = []
    seen_sequences: set[int] = set()
    for path in files:
        payload = _verified_json_file(root, path, label="provider diagnostic event")
        if payload.get("apiVersion") != PROVIDER_DIAGNOSTIC_API_VERSION:
            raise _fail("SDAI-DIAGNOSTICS-003", "unsupported provider diagnostic API version")
        if payload.get("featureId") != feature_id or payload.get("attemptId") != attempt_id:
            raise _fail("SDAI-DIAGNOSTICS-003", "provider diagnostic identity mismatch")
        sequence = payload.get("sequence")
        if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0:
            raise _fail("SDAI-DIAGNOSTICS-003", "provider diagnostic sequence is invalid")
        if sequence in seen_sequences:
            raise _fail("SDAI-DIAGNOSTICS-003", "provider diagnostic sequence is duplicated")
        seen_sequences.add(sequence)
        phase = payload.get("phase")
        if phase not in _ALLOWED_PHASES:
            raise _fail("SDAI-DIAGNOSTICS-003", "provider diagnostic phase is invalid")
        events.append(payload)
    events.sort(key=lambda item: int(item["sequence"]))
    if [int(item["sequence"]) for item in events] != list(range(len(events))):
        raise _fail("SDAI-DIAGNOSTICS-003", "provider diagnostic sequence is not contiguous")
    if events[0].get("phase") != "started":
        raise _fail("SDAI-DIAGNOSTICS-003", "provider diagnostic attempt does not start with started")
    terminal = [item for item in events if item.get("phase") in _TERMINAL_PHASES]
    if len(terminal) > 1 or (terminal and terminal[0] is not events[-1]):
        raise _fail("SDAI-DIAGNOSTICS-003", "provider diagnostic terminal sequence is invalid")
    latest = events[-1]
    phase = str(latest.get("phase"))
    if phase == "started":
        status = "starting"
    elif phase in {"provider-ready", "first-output", "heartbeat"}:
        status = "running"
    else:
        status = str(latest.get("status"))
    heartbeats = [item for item in events if item.get("phase") == "heartbeat"]
    timing = latest.get("timing")
    if not isinstance(timing, dict):
        raise _fail("SDAI-DIAGNOSTICS-003", "provider diagnostic timing is invalid")
    routing_sha = latest.get("routingDecisionDocumentSha256")
    if routing_sha is not None and (
        not isinstance(routing_sha, str) or _SHA256.fullmatch(routing_sha) is None
    ):
        raise _fail("SDAI-DIAGNOSTICS-003", "provider diagnostic routing SHA-256 is invalid")
    failure = latest.get("failure")
    if failure is not None and not isinstance(failure, dict):
        raise _fail("SDAI-DIAGNOSTICS-003", "provider diagnostic failure metadata is invalid")
    return {
        "attemptId": attempt_id,
        "status": status,
        "latestPhase": phase,
        "eventCount": len(events),
        "startedAt": events[0].get("occurredAt"),
        "latestAt": latest.get("occurredAt"),
        "capability": latest.get("capability"),
        "mode": latest.get("mode"),
        "profile": latest.get("profile"),
        "provider": latest.get("provider"),
        "model": latest.get("model"),
        "costClass": latest.get("costClass"),
        "semanticAgent": latest.get("semanticAgent"),
        "timing": dict(timing),
        "heartbeatCount": len(heartbeats),
        "lastHeartbeatAt": heartbeats[-1].get("occurredAt") if heartbeats else None,
        "cancelled": phase == "cancelled",
        "failure": dict(failure) if isinstance(failure, dict) else None,
        "routingDecisionSha256": routing_sha,
        "auditStartSha256": latest.get("auditStartSha256"),
        "providerCapabilities": latest.get("providerCapabilities"),
    }


def _provider_attempts(
    root: Path,
    feature_id: str,
    workspace: Path,
    *,
    selected: tuple[str, ...] | None,
) -> tuple[dict[str, object], ...]:
    provider_root = _safe(
        root,
        workspace / ".sdai" / "diagnostics" / "provider",
        label="provider diagnostics directory",
    )
    if not provider_root.exists():
        return ()
    if not provider_root.is_dir():
        raise _fail("SDAI-DIAGNOSTICS-002", "provider diagnostics path is not a directory")
    directories = sorted(
        (item for item in provider_root.iterdir() if item.is_dir() or item.is_symlink()),
        key=lambda item: item.name,
    )
    if len(directories) > _MAX_PROVIDER_ATTEMPTS:
        raise _fail("SDAI-DIAGNOSTICS-004", "too many provider diagnostic attempts")
    selected_set = set(selected) if selected is not None else None
    rows = [
        _provider_attempt(root, directory, feature_id)
        for directory in directories
        if selected_set is None or directory.name in selected_set
    ]
    rows.sort(key=lambda item: (str(item.get("latestAt") or ""), str(item["attemptId"])))
    return tuple(rows)


def _retry_policy_sha(policy: object) -> str:
    if not isinstance(policy, dict) or policy.get("apiVersion") != PROVIDER_RETRY_API_VERSION:
        raise _fail("SDAI-DIAGNOSTICS-003", "retry policy is invalid")
    return _sha(policy)


def _retry_execution(root: Path, directory: Path) -> dict[str, object]:
    safe_dir = _safe(root, directory, label="provider retry diagnostic")
    if not safe_dir.is_dir() or _ATTEMPT_ID.fullmatch(safe_dir.name) is None:
        raise _fail("SDAI-DIAGNOSTICS-003", "provider retry diagnostic directory is invalid")
    policy_path = safe_dir / "000-policy.json"
    if not policy_path.exists():
        raise _fail("SDAI-DIAGNOSTICS-003", "provider retry diagnostic is missing policy")
    policy_doc = _verified_json_file(
        root, policy_path, label="provider retry policy", require_sha=False
    )
    if policy_doc.get("apiVersion") != PROVIDER_RETRY_API_VERSION or policy_doc.get("retryId") != safe_dir.name:
        raise _fail("SDAI-DIAGNOSTICS-003", "provider retry policy identity mismatch")
    policy = policy_doc.get("policy")
    policy_sha = _retry_policy_sha(policy)
    if policy_doc.get("policySha256") != policy_sha:
        raise _fail("SDAI-DIAGNOSTICS-003", "provider retry policy SHA-256 mismatch")

    decisions: list[dict[str, object]] = []
    for path in sorted(safe_dir.glob("*-decision.json"), key=lambda item: item.name):
        payload = _verified_json_file(
            root, path, label="provider retry decision", require_sha=False
        )
        if payload.get("apiVersion") != PROVIDER_RETRY_API_VERSION or payload.get("retryId") != safe_dir.name:
            raise _fail("SDAI-DIAGNOSTICS-003", "provider retry decision identity mismatch")
        claimed = payload.get("sha256")
        body = dict(payload)
        body.pop("sha256", None)
        body.pop("retryId", None)
        if not isinstance(claimed, str) or claimed != _sha(body):
            raise _fail("SDAI-DIAGNOSTICS-003", "provider retry decision SHA-256 mismatch")
        if payload.get("policySha256") != policy_sha:
            raise _fail("SDAI-DIAGNOSTICS-003", "provider retry decision policy mismatch")
        classification = payload.get("classification")
        if not isinstance(classification, dict):
            raise _fail("SDAI-DIAGNOSTICS-003", "provider retry classification is invalid")
        decisions.append(
            {
                "failedAttempt": payload.get("failedAttempt"),
                "action": payload.get("action"),
                "delayMs": payload.get("delayMs"),
                "reasonCode": payload.get("reasonCode"),
                "classification": dict(classification),
                "diagnosticAttemptId": payload.get("diagnosticAttemptId"),
                "decisionSha256": claimed,
            }
        )
    summary_path = safe_dir / "summary.json"
    summary: dict[str, object] | None = None
    if summary_path.exists():
        payload = _verified_json_file(
            root, summary_path, label="provider retry summary", require_sha=False
        )
        if payload.get("apiVersion") != PROVIDER_RETRY_API_VERSION or payload.get("retryId") != safe_dir.name:
            raise _fail("SDAI-DIAGNOSTICS-003", "provider retry summary identity mismatch")
        if payload.get("policySha256") != policy_sha:
            raise _fail("SDAI-DIAGNOSTICS-003", "provider retry summary policy mismatch")
        final = payload.get("finalClassification")
        if final is not None and not isinstance(final, dict):
            raise _fail("SDAI-DIAGNOSTICS-003", "provider retry final classification is invalid")
        summary = {
            "status": payload.get("status"),
            "attempts": payload.get("attempts"),
            "finalClassification": dict(final) if isinstance(final, dict) else None,
        }
    return {
        "retryId": safe_dir.name,
        "status": summary.get("status") if summary is not None else "in-progress",
        "attempts": summary.get("attempts") if summary is not None else len(decisions),
        "policySha256": policy_sha,
        "decisions": decisions,
        "finalClassification": summary.get("finalClassification") if summary is not None else None,
        "complete": summary is not None,
    }


def _retry_executions(
    root: Path,
    workspace: Path,
    *,
    selected_attempts: tuple[str, ...] | None,
) -> tuple[dict[str, object], ...]:
    retry_root = _safe(
        root,
        workspace / ".sdai" / "diagnostics" / "retry",
        label="provider retry diagnostics directory",
    )
    if not retry_root.exists():
        return ()
    if not retry_root.is_dir():
        raise _fail("SDAI-DIAGNOSTICS-002", "provider retry diagnostics path is not a directory")
    directories = sorted(
        (item for item in retry_root.iterdir() if item.is_dir() or item.is_symlink()),
        key=lambda item: item.name,
    )
    if len(directories) > _MAX_RETRY_EXECUTIONS:
        raise _fail("SDAI-DIAGNOSTICS-004", "too many provider retry executions")
    if selected_attempts is not None:
        directories = [
            directory
            for directory in directories
            if any(
                attempt.startswith(directory.name + "-a")
                for attempt in selected_attempts
            )
        ]
    return tuple(_retry_execution(root, directory) for directory in directories)


def _routing_summary(
    root: Path,
    feature_id: str,
    attempts: tuple[dict[str, object], ...],
) -> dict[str, object]:
    routed = [item for item in attempts if item.get("routingDecisionSha256") is not None]
    if not routed:
        return {
            "available": False,
            "decisionSha256": None,
            "reason": "no-routed-provider-attempt",
        }
    latest = routed[-1]
    decision_sha = latest.get("routingDecisionSha256")
    assert isinstance(decision_sha, str)
    document = load_routing_diagnostic(root, feature_id, decision_sha)
    if document is None:
        return {
            "available": False,
            "decisionSha256": decision_sha,
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
    return {
        "available": True,
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


def _audit_summary(body: Mapping[str, object]) -> dict[str, object]:
    identifiers: list[dict[str, object]] = []
    events = body.get("events")
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
                }
            )
    relationships = body.get("relationships")
    relationship_summary = None
    if isinstance(relationships, dict):
        gaps = relationships.get("gaps")
        relationship_summary = {
            "available": relationships.get("available"),
            "linkedReferences": relationships.get("linkedReferences"),
            "gapCount": len(gaps) if isinstance(gaps, list) else None,
            "errorCode": relationships.get("errorCode"),
            "errorType": relationships.get("errorType"),
        }
    return {
        "status": body.get("status"),
        "eventCount": body.get("eventCount"),
        "selectedCount": body.get("selectedCount"),
        "returnedCount": body.get("returnedCount"),
        "truncated": body.get("truncated"),
        "ledgerHeadSha256": body.get("ledgerHeadSha256"),
        "exportSha256": body.get("exportSha256"),
        "reportSha256": body.get("reportSha256"),
        "events": identifiers,
        "relationships": relationship_summary,
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

    audit = build_audit_report(
        root,
        feature,
        selectors=AuditSelectors(run_id=run, task_id=task),
    )
    audit_body = audit.to_dict()
    filters_active = run is not None or task is not None
    selected_attempts: tuple[str, ...] | None = None
    correlation_truncated = False
    if filters_active:
        selected_attempts = _selected_attempt_ids(audit_body)
        correlation_truncated = bool(audit_body.get("truncated"))

    attempts = _provider_attempts(
        root,
        feature,
        workspace,
        selected=selected_attempts,
    )
    retry = _retry_executions(
        root,
        workspace,
        selected_attempts=selected_attempts,
    )
    routing = _routing_summary(root, feature, attempts)
    context = _context_summary(root, feature, attempts)
    audit_summary = _audit_summary(audit_body)

    partial_reasons: list[str] = []
    if correlation_truncated:
        partial_reasons.append("audit-selector-correlation-truncated")
    if attempts and not routing.get("available") and routing.get("decisionSha256") is not None:
        partial_reasons.append("routing-document-hash-only")
    if not context.get("available"):
        partial_reasons.append("current-context-unavailable")
    if any(not bool(item.get("complete")) for item in retry):
        partial_reasons.append("retry-evidence-in-progress")

    has_data = bool(attempts or retry or int(audit_body.get("selectedCount") or 0))
    status = "partial" if partial_reasons else ("available" if has_data else "no-data")
    selectors = {"runId": run, "taskId": task}
    body: dict[str, object] = {
        "apiVersion": DIAGNOSTICS_API_VERSION,
        "featureId": feature,
        "workspace": workspace.relative_to(root).as_posix(),
        "status": status,
        "selectors": selectors,
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
