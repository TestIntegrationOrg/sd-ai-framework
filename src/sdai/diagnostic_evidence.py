from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path, PurePosixPath
import re
from typing import Mapping

from sdai.agent_platform.provider_diagnostics import PROVIDER_DIAGNOSTIC_API_VERSION
from sdai.agent_platform.provider_retry import PROVIDER_RETRY_API_VERSION
from sdai.path_safety import PathSafetyError, ensure_within_project


_MAX_PROVIDER_ATTEMPTS = 1_000
_MAX_EVENTS_PER_ATTEMPT = 100
_MAX_RETRY_EXECUTIONS = 1_000
_ATTEMPT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_TERMINAL_PHASES = frozenset({"completed", "failed", "cancelled"})
_ALLOWED_PHASES = frozenset(
    {"started", "provider-ready", "first-output", "heartbeat", "completed", "failed", "cancelled"}
)


class DiagnosticEvidenceError(RuntimeError):
    pass


def _fail(code: str, message: str) -> DiagnosticEvidenceError:
    return DiagnosticEvidenceError(f"{code}: {message}")


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _sha(value: object) -> str:
    return "sha256:" + sha256(_canonical_bytes(value)).hexdigest()


def _safe(root: Path, candidate: Path, *, label: str) -> Path:
    try:
        safe = ensure_within_project(root, candidate, label=label)
    except PathSafetyError as exc:
        raise _fail("SDAI-DIAG-EVIDENCE-001", f"{label} escapes project root") from exc
    resolved_root = root.resolve()
    current = resolved_root
    try:
        relative = safe.relative_to(resolved_root)
    except ValueError:
        relative = safe.resolve(strict=False).relative_to(resolved_root)
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise _fail("SDAI-DIAG-EVIDENCE-001", f"{label} contains a symlink component")
    return safe


def _verified_json_file(
    root: Path,
    path: Path,
    *,
    label: str,
    require_sha: bool = True,
) -> dict[str, object]:
    safe = _safe(root, path, label=label)
    if not safe.is_file():
        raise _fail("SDAI-DIAG-EVIDENCE-002", f"{label} must be a regular file")
    try:
        raw = safe.read_bytes()
        payload = json.loads(raw.decode("utf-8", errors="strict"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise _fail("SDAI-DIAG-EVIDENCE-002", f"{label} is not valid UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise _fail("SDAI-DIAG-EVIDENCE-002", f"{label} must contain a JSON object")
    if raw != _canonical_bytes(payload) + b"\n":
        raise _fail("SDAI-DIAG-EVIDENCE-002", f"{label} is not canonical JSON")
    if require_sha:
        claimed = payload.get("sha256")
        body = dict(payload)
        body.pop("sha256", None)
        if not isinstance(claimed, str) or claimed != _sha(body):
            raise _fail("SDAI-DIAG-EVIDENCE-002", f"{label} failed SHA-256 verification")
    return payload


def attempt_id_from_binding(source: object) -> str | None:
    if not isinstance(source, str) or "\\" in source:
        return None
    parts = PurePosixPath(source).parts
    for index in range(len(parts) - 2):
        if parts[index : index + 2] == ("diagnostics", "provider"):
            attempt = parts[index + 2]
            return attempt if _ATTEMPT_ID.fullmatch(attempt) else None
    return None


def selected_attempt_ids(audit_body: Mapping[str, object]) -> tuple[str, ...]:
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
            attempt = attempt_id_from_binding(binding.get("source"))
            if attempt is not None and attempt not in result:
                result.append(attempt)
    return tuple(result)


def _provider_attempt(root: Path, directory: Path, feature_id: str) -> dict[str, object]:
    safe_dir = _safe(root, directory, label="provider diagnostic attempt")
    if not safe_dir.is_dir() or _ATTEMPT_ID.fullmatch(safe_dir.name) is None:
        raise _fail("SDAI-DIAG-EVIDENCE-002", "provider diagnostic attempt is invalid")
    files = sorted(safe_dir.glob("*.json"), key=lambda item: item.name)
    if not files or len(files) > _MAX_EVENTS_PER_ATTEMPT:
        raise _fail("SDAI-DIAG-EVIDENCE-003", "provider diagnostic event count is invalid")
    events: list[dict[str, object]] = []
    sequences: set[int] = set()
    for path in files:
        payload = _verified_json_file(root, path, label="provider diagnostic event")
        if payload.get("apiVersion") != PROVIDER_DIAGNOSTIC_API_VERSION:
            raise _fail("SDAI-DIAG-EVIDENCE-002", "unsupported provider diagnostic API version")
        if payload.get("featureId") != feature_id or payload.get("attemptId") != safe_dir.name:
            raise _fail("SDAI-DIAG-EVIDENCE-002", "provider diagnostic identity mismatch")
        sequence = payload.get("sequence")
        if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0 or sequence in sequences:
            raise _fail("SDAI-DIAG-EVIDENCE-002", "provider diagnostic sequence is invalid")
        sequences.add(sequence)
        if payload.get("phase") not in _ALLOWED_PHASES:
            raise _fail("SDAI-DIAG-EVIDENCE-002", "provider diagnostic phase is invalid")
        events.append(payload)
    events.sort(key=lambda item: int(item["sequence"]))
    if [int(item["sequence"]) for item in events] != list(range(len(events))):
        raise _fail("SDAI-DIAG-EVIDENCE-002", "provider diagnostic sequence is not contiguous")
    if events[0].get("phase") != "started":
        raise _fail("SDAI-DIAG-EVIDENCE-002", "provider diagnostic does not start with started")
    terminal = [item for item in events if item.get("phase") in _TERMINAL_PHASES]
    if len(terminal) > 1 or (terminal and terminal[0] is not events[-1]):
        raise _fail("SDAI-DIAG-EVIDENCE-002", "provider diagnostic terminal sequence is invalid")
    latest = events[-1]
    phase = str(latest.get("phase"))
    status = (
        "starting"
        if phase == "started"
        else ("running" if phase in {"provider-ready", "first-output", "heartbeat"} else str(latest.get("status")))
    )
    heartbeats = [item for item in events if item.get("phase") == "heartbeat"]
    timing = latest.get("timing")
    if not isinstance(timing, dict):
        raise _fail("SDAI-DIAG-EVIDENCE-002", "provider diagnostic timing is invalid")
    routing_sha = latest.get("routingDecisionDocumentSha256")
    if routing_sha is not None and (
        not isinstance(routing_sha, str) or _SHA256.fullmatch(routing_sha) is None
    ):
        raise _fail("SDAI-DIAG-EVIDENCE-002", "provider routing SHA-256 is invalid")
    failure = latest.get("failure")
    if failure is not None and not isinstance(failure, dict):
        raise _fail("SDAI-DIAG-EVIDENCE-002", "provider failure metadata is invalid")
    return {
        "attemptId": safe_dir.name,
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


def read_provider_attempts(
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
        raise _fail("SDAI-DIAG-EVIDENCE-001", "provider diagnostics path is not a directory")
    directories = sorted(
        (item for item in provider_root.iterdir() if item.is_dir() or item.is_symlink()),
        key=lambda item: item.name,
    )
    if len(directories) > _MAX_PROVIDER_ATTEMPTS:
        raise _fail("SDAI-DIAG-EVIDENCE-003", "too many provider diagnostic attempts")
    selected_set = set(selected) if selected is not None else None
    rows = [
        _provider_attempt(root, item, feature_id)
        for item in directories
        if selected_set is None or item.name in selected_set
    ]
    rows.sort(key=lambda item: (str(item.get("latestAt") or ""), str(item["attemptId"])))
    return tuple(rows)


def _policy_sha(policy: object) -> str:
    if not isinstance(policy, dict) or policy.get("apiVersion") != PROVIDER_RETRY_API_VERSION:
        raise _fail("SDAI-DIAG-EVIDENCE-002", "retry policy is invalid")
    return _sha(policy)


def _retry_execution(root: Path, directory: Path) -> dict[str, object]:
    safe_dir = _safe(root, directory, label="provider retry diagnostic")
    if not safe_dir.is_dir() or _ATTEMPT_ID.fullmatch(safe_dir.name) is None:
        raise _fail("SDAI-DIAG-EVIDENCE-002", "provider retry directory is invalid")
    policy_doc = _verified_json_file(
        root, safe_dir / "000-policy.json", label="provider retry policy", require_sha=False
    )
    if policy_doc.get("apiVersion") != PROVIDER_RETRY_API_VERSION or policy_doc.get("retryId") != safe_dir.name:
        raise _fail("SDAI-DIAG-EVIDENCE-002", "provider retry policy identity mismatch")
    policy_sha = _policy_sha(policy_doc.get("policy"))
    if policy_doc.get("policySha256") != policy_sha:
        raise _fail("SDAI-DIAG-EVIDENCE-002", "provider retry policy SHA-256 mismatch")
    decisions: list[dict[str, object]] = []
    for path in sorted(safe_dir.glob("*-decision.json"), key=lambda item: item.name):
        payload = _verified_json_file(root, path, label="provider retry decision", require_sha=False)
        if payload.get("apiVersion") != PROVIDER_RETRY_API_VERSION or payload.get("retryId") != safe_dir.name:
            raise _fail("SDAI-DIAG-EVIDENCE-002", "provider retry decision identity mismatch")
        claimed = payload.get("sha256")
        body = dict(payload)
        body.pop("sha256", None)
        body.pop("retryId", None)
        if not isinstance(claimed, str) or claimed != _sha(body):
            raise _fail("SDAI-DIAG-EVIDENCE-002", "provider retry decision SHA-256 mismatch")
        if payload.get("policySha256") != policy_sha or not isinstance(payload.get("classification"), dict):
            raise _fail("SDAI-DIAG-EVIDENCE-002", "provider retry decision metadata mismatch")
        decisions.append(
            {
                "failedAttempt": payload.get("failedAttempt"),
                "action": payload.get("action"),
                "delayMs": payload.get("delayMs"),
                "reasonCode": payload.get("reasonCode"),
                "classification": dict(payload["classification"]),
                "diagnosticAttemptId": payload.get("diagnosticAttemptId"),
                "decisionSha256": claimed,
            }
        )
    summary_path = safe_dir / "summary.json"
    summary: dict[str, object] | None = None
    if summary_path.exists():
        payload = _verified_json_file(root, summary_path, label="provider retry summary", require_sha=False)
        if payload.get("apiVersion") != PROVIDER_RETRY_API_VERSION or payload.get("retryId") != safe_dir.name:
            raise _fail("SDAI-DIAG-EVIDENCE-002", "provider retry summary identity mismatch")
        if payload.get("policySha256") != policy_sha:
            raise _fail("SDAI-DIAG-EVIDENCE-002", "provider retry summary policy mismatch")
        final = payload.get("finalClassification")
        if final is not None and not isinstance(final, dict):
            raise _fail("SDAI-DIAG-EVIDENCE-002", "provider retry final classification is invalid")
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


def read_retry_executions(
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
        raise _fail("SDAI-DIAG-EVIDENCE-001", "provider retry path is not a directory")
    directories = sorted(
        (item for item in retry_root.iterdir() if item.is_dir() or item.is_symlink()),
        key=lambda item: item.name,
    )
    if len(directories) > _MAX_RETRY_EXECUTIONS:
        raise _fail("SDAI-DIAG-EVIDENCE-003", "too many provider retry executions")
    if selected_attempts is not None:
        directories = [
            item
            for item in directories
            if any(attempt.startswith(item.name + "-a") for attempt in selected_attempts)
        ]
    return tuple(_retry_execution(root, item) for item in directories)


__all__ = [
    "DiagnosticEvidenceError",
    "attempt_id_from_binding",
    "read_provider_attempts",
    "read_retry_executions",
    "selected_attempt_ids",
]
