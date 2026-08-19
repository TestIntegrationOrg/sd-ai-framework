from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
from typing import Mapping

from sdai.audit_contracts import AUDIT_MAX_EVENT_BYTES, AUDIT_MAX_EVENTS, AUDIT_MAX_LEDGER_BYTES
from sdai.audit_provenance import AuditEvent
from sdai.models import FeatureContext, validate_feature_id
from sdai.path_safety import PathSafetyError, ensure_within_project


READ_ONLY_AUDIT_API_VERSION = "sdai.audit-readonly/v1"
_MAX_RETURNED_EVENTS = 500
_ZERO_HASH = "sha256:" + ("0" * 64)


class ReadOnlyAuditError(RuntimeError):
    """Raised when audit evidence cannot be verified without mutating the workspace."""


def _fail(code: str, message: str) -> ReadOnlyAuditError:
    return ReadOnlyAuditError(f"{code}: {message}")


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
        raise _fail("SDAI-AUDIT-READONLY-004", "audit value is not canonical JSON") from exc


def _sha_bytes(value: bytes) -> str:
    return "sha256:" + sha256(value).hexdigest()


def _safe(root: Path, candidate: Path, *, label: str) -> Path:
    try:
        safe = ensure_within_project(root, candidate, label=label)
    except PathSafetyError as exc:
        raise _fail("SDAI-AUDIT-READONLY-001", f"{label} escapes project root") from exc
    resolved_root = root.resolve()
    current = resolved_root
    try:
        relative = safe.relative_to(resolved_root)
    except ValueError:
        relative = safe.resolve(strict=False).relative_to(resolved_root)
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise _fail("SDAI-AUDIT-READONLY-001", f"{label} contains a symlink component")
    return safe


def _event_status(event: AuditEvent) -> str | None:
    value = event.metadata.get("status")
    return value if isinstance(value, str) else None


def _event_summary(event: AuditEvent) -> dict[str, object]:
    return {
        "sequence": event.sequence,
        "eventId": event.event_id,
        "eventSha256": event.sha256,
        "occurredAt": event.occurred_at,
        "category": event.category,
        "actorKind": event.actor.kind,
        "action": event.action.kind,
        "status": _event_status(event),
        "execution": event.execution.to_dict(),
        "bindings": [item.to_dict() for item in event.bindings],
    }


def _matches(event: AuditEvent, *, run_id: str | None, task_id: str | None) -> bool:
    if run_id is not None and event.execution.run_id != run_id:
        return False
    if task_id is not None and event.execution.task_id != task_id:
        return False
    return True


def _verified_prefix(raw: bytes) -> tuple[bytes, int]:
    """Return canonical complete-record bytes plus recoverable incomplete-tail size.

    The normal AuditLedger may truncate an incomplete crash tail while holding its
    write lock. Unified diagnostics is intentionally read-only, so it recognizes the
    same recoverable condition but never mutates the file. A complete JSON record
    missing its canonical newline remains corruption and fails closed.
    """
    if not raw or raw.endswith(b"\n"):
        return raw, 0
    boundary = raw.rfind(b"\n")
    tail = raw[boundary + 1 :]
    try:
        json.loads(tail.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        prefix = raw[: boundary + 1] if boundary >= 0 else b""
        return prefix, len(tail)
    raise _fail(
        "SDAI-AUDIT-READONLY-004",
        "audit ledger contains a complete noncanonical record missing the canonical newline",
    )


def read_verified_audit(
    project_root: Path,
    feature_id: str,
    *,
    run_id: str | None = None,
    task_id: str | None = None,
) -> dict[str, object]:
    """Verify/read canonical audit JSONL without creating directories or lock files."""
    root = project_root.resolve()
    feature = validate_feature_id(feature_id)
    workspace = FeatureContext(root, feature).feature_dir
    if not workspace.exists() or workspace.is_symlink() or not workspace.is_dir():
        raise _fail("SDAI-AUDIT-READONLY-001", "feature workspace is missing or unsafe")
    _safe(root, workspace, label="audit feature workspace")
    events_path = _safe(
        root,
        workspace / ".sdai" / "audit" / "events.jsonl",
        label="audit ledger",
    )
    if not events_path.exists():
        body: dict[str, object] = {
            "apiVersion": READ_ONLY_AUDIT_API_VERSION,
            "featureId": feature,
            "status": "no-events",
            "eventCount": 0,
            "selectedCount": 0,
            "returnedCount": 0,
            "truncated": False,
            "recoverableCrashTailBytes": 0,
            "ledgerHeadSha256": _ZERO_HASH,
            "exportSha256": _sha_bytes(b""),
            "events": [],
        }
        body["reportSha256"] = _sha_bytes(_canonical_bytes(body))
        return body
    if not events_path.is_file():
        raise _fail("SDAI-AUDIT-READONLY-001", "audit ledger is not a regular file")
    try:
        raw = events_path.read_bytes()
    except OSError as exc:
        raise _fail("SDAI-AUDIT-READONLY-002", "unable to read audit ledger") from exc
    if len(raw) > AUDIT_MAX_LEDGER_BYTES:
        raise _fail("SDAI-AUDIT-READONLY-003", "audit ledger exceeds bounded size")

    verified, crash_tail_bytes = _verified_prefix(raw)
    raw_lines = verified[:-1].split(b"\n") if verified else []
    if len(raw_lines) > AUDIT_MAX_EVENTS:
        raise _fail("SDAI-AUDIT-READONLY-003", "audit ledger exceeds event limit")

    events: list[AuditEvent] = []
    expected_previous = _ZERO_HASH
    for index, raw_line in enumerate(raw_lines, start=1):
        if not raw_line:
            raise _fail("SDAI-AUDIT-READONLY-004", f"audit ledger line {index} is empty")
        if len(raw_line) > AUDIT_MAX_EVENT_BYTES:
            raise _fail("SDAI-AUDIT-READONLY-004", f"audit ledger line {index} exceeds event limit")
        try:
            payload = json.loads(raw_line.decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise _fail("SDAI-AUDIT-READONLY-004", f"audit ledger line {index} is invalid JSON") from exc
        if not isinstance(payload, Mapping):
            raise _fail("SDAI-AUDIT-READONLY-004", f"audit ledger line {index} must be a JSON object")
        try:
            event = AuditEvent.from_mapping(payload)
        except Exception as exc:
            raise _fail("SDAI-AUDIT-READONLY-004", f"audit ledger line {index} contract/hash is invalid") from exc
        if raw_line != _canonical_bytes(event.to_dict()):
            raise _fail("SDAI-AUDIT-READONLY-004", f"audit ledger line {index} is not canonical JSON")
        if event.feature_id != feature:
            raise _fail("SDAI-AUDIT-READONLY-004", f"audit ledger line {index} belongs to another feature")
        if event.sequence != index:
            raise _fail("SDAI-AUDIT-READONLY-004", f"audit ledger sequence gap at line {index}")
        if event.previous_sha256 != expected_previous:
            raise _fail("SDAI-AUDIT-READONLY-004", f"audit hash chain mismatch at line {index}")
        expected_previous = event.sha256
        events.append(event)

    selected = [event for event in events if _matches(event, run_id=run_id, task_id=task_id)]
    returned = selected[:_MAX_RETURNED_EVENTS]
    body = {
        "apiVersion": READ_ONLY_AUDIT_API_VERSION,
        "featureId": feature,
        "status": "partial" if crash_tail_bytes else ("ok" if events else "no-events"),
        "eventCount": len(events),
        "selectedCount": len(selected),
        "returnedCount": len(returned),
        "truncated": len(selected) > len(returned),
        "recoverableCrashTailBytes": crash_tail_bytes,
        "ledgerHeadSha256": events[-1].sha256 if events else _ZERO_HASH,
        "exportSha256": _sha_bytes(verified),
        "events": [_event_summary(event) for event in returned],
    }
    body["reportSha256"] = _sha_bytes(_canonical_bytes(body))
    return body


__all__ = [
    "READ_ONLY_AUDIT_API_VERSION",
    "ReadOnlyAuditError",
    "read_verified_audit",
]
