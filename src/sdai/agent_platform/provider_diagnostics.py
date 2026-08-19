from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import time
from typing import Callable, Mapping, Protocol
from uuid import uuid4

from sdai.agent_platform.models import AgentInvocation
from sdai.models import FeatureContext, validate_feature_id
from sdai.path_safety import PathSafetyError, ensure_within_project
from sdai.providers.base import ProviderCapabilities
from sdai.providers.control import ProviderCancelledError


PROVIDER_DIAGNOSTIC_API_VERSION = "sdai.provider-diagnostic/v1"
_ATTEMPT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")


class ProviderDiagnosticError(RuntimeError):
    """Raised when provider diagnostics cannot be recorded safely."""


def _fail(code: str, message: str) -> ProviderDiagnosticError:
    return ProviderDiagnosticError(f"{code}: {message}")


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
        raise _fail("SDAI-PROVIDER-DIAG-001", "diagnostic value is not canonical JSON") from exc


def _sha256_bytes(value: bytes) -> str:
    return "sha256:" + sha256(value).hexdigest()


def _timestamp(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise _fail("SDAI-PROVIDER-DIAG-001", "diagnostic clock must return timezone-aware datetime")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _safe_attempt_id(value: str) -> str:
    if not isinstance(value, str) or _ATTEMPT_ID.fullmatch(value) is None:
        raise _fail("SDAI-PROVIDER-DIAG-001", "attempt id must be a safe portable identifier")
    return value


def _safe_reason(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 128:
        raise _fail("SDAI-PROVIDER-DIAG-001", "progress reason must contain 1..128 characters")
    if any(ord(ch) < 0x20 or ord(ch) > 0x7E for ch in value):
        raise _fail("SDAI-PROVIDER-DIAG-001", "progress reason must be printable ASCII")
    return value


def _safe_chain(root: Path, candidate: Path, *, label: str) -> Path:
    try:
        safe = ensure_within_project(root, candidate, label=label)
    except PathSafetyError as exc:
        raise _fail("SDAI-PROVIDER-DIAG-002", f"{label} escapes the project workspace") from exc
    resolved_root = root.resolve()
    current = resolved_root
    try:
        relative = safe.relative_to(resolved_root)
    except ValueError:
        relative = safe.resolve(strict=False).relative_to(resolved_root)
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise _fail("SDAI-PROVIDER-DIAG-002", f"{label} contains a symlink component")
    return safe


def _routing_document_sha256(serialized: str | None) -> str | None:
    if serialized is None:
        return None
    if not isinstance(serialized, str) or not serialized:
        raise _fail("SDAI-PROVIDER-DIAG-001", "routing decision must be text or null")
    return _sha256_bytes(serialized.encode("utf-8"))


def _failure_classification(error: BaseException, *, stage: str) -> Mapping[str, str]:
    type_name = type(error).__name__[:128] or "Exception"
    if isinstance(error, ProviderCancelledError):
        category = "cancelled"
    elif isinstance(error, TimeoutError) or type_name == "TimeoutExpired":
        category = "timeout"
    elif isinstance(error, FileNotFoundError):
        category = "provider-not-found"
    elif isinstance(error, PermissionError):
        category = "permission"
    elif type_name == "ProviderExecutionError":
        category = "provider-execution"
    elif type_name == "PolicyError":
        category = "policy"
    elif stage == "startup":
        category = "provider-startup"
    else:
        category = "provider-failure"
    return {"category": category, "type": type_name}


class ProviderDiagnosticClock(Protocol):
    def monotonic_ns(self) -> int: ...

    def utc_now(self) -> datetime: ...


@dataclass(frozen=True)
class SystemProviderDiagnosticClock:
    def monotonic_ns(self) -> int:
        return time.monotonic_ns()

    def utc_now(self) -> datetime:
        return datetime.now(timezone.utc)


@dataclass(frozen=True)
class ProviderDiagnosticEvent:
    attempt_id: str
    sequence: int
    phase: str
    occurred_at: str
    feature_id: str
    capability: str
    mode: str
    profile: str
    provider: str
    model: str | None
    cost_class: str
    semantic_agent: str | None
    routing_document_sha256: str | None
    audit_start_sha256: str | None
    provider_capabilities: ProviderCapabilities | None
    startup_ns: int | None
    invocation_ns: int | None
    total_ns: int
    first_output: Mapping[str, object]
    status: str
    progress_reason: str | None = None
    failure: Mapping[str, str] | None = None

    def _body(self) -> dict[str, object]:
        result: dict[str, object] = {
            "apiVersion": PROVIDER_DIAGNOSTIC_API_VERSION,
            "attemptId": self.attempt_id,
            "sequence": self.sequence,
            "phase": self.phase,
            "occurredAt": self.occurred_at,
            "featureId": self.feature_id,
            "capability": self.capability,
            "mode": self.mode,
            "profile": self.profile,
            "provider": self.provider,
            "model": self.model,
            "costClass": self.cost_class,
            "semanticAgent": self.semantic_agent,
            "routingDecisionDocumentSha256": self.routing_document_sha256,
            "auditStartSha256": self.audit_start_sha256,
            "providerCapabilities": (
                self.provider_capabilities.as_dict()
                if self.provider_capabilities is not None
                else None
            ),
            "timing": {
                "startupNs": self.startup_ns,
                "invocationNs": self.invocation_ns,
                "totalNs": self.total_ns,
                "firstOutput": dict(self.first_output),
            },
            "status": self.status,
            "failure": dict(self.failure) if self.failure is not None else None,
        }
        if self.progress_reason is not None:
            result["progressReason"] = self.progress_reason
        return result

    @property
    def sha256(self) -> str:
        return _sha256_bytes(_canonical_bytes(self._body()))

    def as_dict(self) -> dict[str, object]:
        result = self._body()
        result["sha256"] = self.sha256
        return result

    def to_json(self) -> str:
        return _canonical_bytes(self.as_dict()).decode("utf-8") + "\n"


@dataclass(frozen=True)
class PersistedProviderDiagnostic:
    event: ProviderDiagnosticEvent
    source: str
    file_sha256: str


@dataclass
class ProviderDiagnosticRecorder:
    root: Path
    feature_id: str
    invocation: AgentInvocation
    clock: ProviderDiagnosticClock
    id_factory: Callable[[], str]
    attempt_id: str | None = None
    attempt_dir: Path | None = None
    started_ns: int | None = None
    ready_ns: int | None = None
    first_output_ns: int | None = None
    audit_start_sha256: str | None = None
    capabilities: ProviderCapabilities | None = None
    next_sequence: int = 0
    terminal: bool = False

    @classmethod
    def optional_for(
        cls,
        project_root: Path,
        invocation: AgentInvocation,
        *,
        clock: ProviderDiagnosticClock | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> "ProviderDiagnosticRecorder | None":
        root = project_root.resolve()
        feature = validate_feature_id(invocation.feature_id)
        context = FeatureContext(root, feature)
        workspace = context.feature_dir
        if not workspace.exists() and not workspace.is_symlink():
            return None
        if workspace.is_symlink() or not workspace.is_dir():
            raise _fail("SDAI-PROVIDER-DIAG-002", "feature workspace is missing or unsafe")
        _safe_chain(root, workspace, label="provider diagnostic feature workspace")
        return cls(
            root=root,
            feature_id=feature,
            invocation=invocation,
            clock=clock or SystemProviderDiagnosticClock(),
            id_factory=id_factory or (lambda: uuid4().hex),
        )

    def _diagnostic_root(self) -> Path:
        workspace = FeatureContext(self.root, self.feature_id).feature_dir
        candidate = workspace / ".sdai" / "diagnostics" / "provider"
        safe = _safe_chain(self.root, candidate, label="provider diagnostic directory")
        safe.mkdir(parents=True, exist_ok=True)
        _safe_chain(self.root, safe, label="provider diagnostic directory")
        return safe

    def _persist(self, event: ProviderDiagnosticEvent, filename: str) -> PersistedProviderDiagnostic:
        if self.attempt_dir is None:
            raise _fail("SDAI-PROVIDER-DIAG-003", "provider diagnostic attempt was not started")
        path = _safe_chain(
            self.root,
            self.attempt_dir / filename,
            label="provider diagnostic event",
        )
        data = event.to_json().encode("utf-8")
        try:
            with path.open("xb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError as exc:
            raise _fail("SDAI-PROVIDER-DIAG-003", f"diagnostic event already exists: {filename}") from exc
        except OSError as exc:
            raise _fail("SDAI-PROVIDER-DIAG-003", f"unable to persist diagnostic event: {filename}") from exc
        source = path.relative_to(self.root).as_posix()
        return PersistedProviderDiagnostic(event, source, _sha256_bytes(data))

    def _first_output(self, reason: str) -> Mapping[str, object]:
        if self.first_output_ns is not None and self.ready_ns is not None:
            return {
                "available": True,
                "elapsedNs": self.first_output_ns - self.ready_ns,
                "reason": "provider-reported",
            }
        return {"available": False, "elapsedNs": None, "reason": reason}

    def _event(
        self,
        *,
        sequence: int,
        phase: str,
        status: str,
        now_ns: int,
        failure: Mapping[str, str] | None = None,
        first_output_reason: str,
        progress_reason: str | None = None,
    ) -> ProviderDiagnosticEvent:
        if self.started_ns is None or self.attempt_id is None:
            raise _fail("SDAI-PROVIDER-DIAG-003", "provider diagnostic attempt was not started")
        if now_ns < self.started_ns:
            raise _fail("SDAI-PROVIDER-DIAG-001", "diagnostic monotonic clock moved backwards")
        if self.ready_ns is None:
            startup_ns = now_ns - self.started_ns if phase in {"failed", "cancelled"} else None
            invocation_ns = None
        else:
            startup_ns = self.ready_ns - self.started_ns
            invocation_ns = now_ns - self.ready_ns
        return ProviderDiagnosticEvent(
            attempt_id=self.attempt_id,
            sequence=sequence,
            phase=phase,
            occurred_at=_timestamp(self.clock.utc_now()),
            feature_id=self.feature_id,
            capability=self.invocation.capability.value,
            mode=self.invocation.mode.value,
            profile=self.invocation.profile.name,
            provider=self.invocation.profile.provider,
            model=self.invocation.profile.model,
            cost_class=self.invocation.profile.cost_class,
            semantic_agent=self.invocation.agent_name,
            routing_document_sha256=_routing_document_sha256(self.invocation.routing_decision),
            audit_start_sha256=self.audit_start_sha256,
            provider_capabilities=self.capabilities,
            startup_ns=startup_ns,
            invocation_ns=invocation_ns,
            total_ns=now_ns - self.started_ns,
            first_output=self._first_output(first_output_reason),
            status=status,
            progress_reason=progress_reason,
            failure=failure,
        )

    def start(self, *, audit_start_sha256: str | None) -> PersistedProviderDiagnostic:
        if self.started_ns is not None:
            raise _fail("SDAI-PROVIDER-DIAG-003", "provider diagnostic attempt already started")
        if audit_start_sha256 is not None and _SHA256.fullmatch(audit_start_sha256) is None:
            raise _fail("SDAI-PROVIDER-DIAG-001", "audit start SHA-256 is invalid")
        attempt_id = _safe_attempt_id(self.id_factory())
        root = self._diagnostic_root()
        attempt_dir = _safe_chain(
            self.root,
            root / attempt_id,
            label="provider diagnostic attempt directory",
        )
        try:
            attempt_dir.mkdir()
        except FileExistsError as exc:
            raise _fail("SDAI-PROVIDER-DIAG-003", "provider diagnostic attempt id already exists") from exc
        except OSError as exc:
            raise _fail("SDAI-PROVIDER-DIAG-003", "unable to create provider diagnostic attempt") from exc
        self.attempt_id = attempt_id
        self.attempt_dir = attempt_dir
        self.audit_start_sha256 = audit_start_sha256
        self.started_ns = self.clock.monotonic_ns()
        self.next_sequence = 1
        event = self._event(
            sequence=0,
            phase="started",
            status="started",
            now_ns=self.started_ns,
            first_output_reason="provider-not-created",
        )
        return self._persist(event, "000-started.json")

    def provider_ready(self, capabilities: ProviderCapabilities) -> PersistedProviderDiagnostic:
        if self.terminal:
            raise _fail("SDAI-PROVIDER-DIAG-003", "provider diagnostic attempt is terminal")
        if self.ready_ns is not None:
            raise _fail("SDAI-PROVIDER-DIAG-003", "provider ready event already recorded")
        if not isinstance(capabilities, ProviderCapabilities):
            raise _fail("SDAI-PROVIDER-DIAG-001", "provider capabilities are invalid")
        self.capabilities = capabilities
        self.ready_ns = self.clock.monotonic_ns()
        sequence = self.next_sequence
        self.next_sequence += 1
        event = self._event(
            sequence=sequence,
            phase="provider-ready",
            status="ready",
            now_ns=self.ready_ns,
            first_output_reason=(
                "provider-hook-not-reported"
                if capabilities.first_output_timing
                else "provider-complete-interface"
            ),
        )
        return self._persist(event, f"{sequence:03d}-provider-ready.json")

    def first_output(self, *, reason: str) -> PersistedProviderDiagnostic:
        if self.terminal or self.ready_ns is None:
            raise _fail("SDAI-PROVIDER-DIAG-003", "provider is not in a running state")
        if self.capabilities is None or not self.capabilities.first_output_timing:
            raise _fail("SDAI-PROVIDER-DIAG-004", "provider reported unsupported first-output timing")
        if self.first_output_ns is not None:
            raise _fail("SDAI-PROVIDER-DIAG-004", "provider first-output was already reported")
        now_ns = self.clock.monotonic_ns()
        if now_ns < self.ready_ns:
            raise _fail("SDAI-PROVIDER-DIAG-001", "diagnostic monotonic clock moved backwards")
        previous = self.first_output_ns
        self.first_output_ns = now_ns
        sequence = self.next_sequence
        event = self._event(
            sequence=sequence,
            phase="first-output",
            status="running",
            now_ns=now_ns,
            first_output_reason="provider-reported",
            progress_reason=_safe_reason(reason),
        )
        try:
            persisted = self._persist(event, f"{sequence:03d}-first-output.json")
        except BaseException:
            self.first_output_ns = previous
            raise
        self.next_sequence += 1
        return persisted

    def heartbeat(self, *, reason: str) -> PersistedProviderDiagnostic:
        if self.terminal or self.ready_ns is None:
            raise _fail("SDAI-PROVIDER-DIAG-003", "provider is not in a running state")
        if self.capabilities is None or not self.capabilities.heartbeat:
            raise _fail("SDAI-PROVIDER-DIAG-004", "provider reported unsupported heartbeat")
        now_ns = self.clock.monotonic_ns()
        sequence = self.next_sequence
        event = self._event(
            sequence=sequence,
            phase="heartbeat",
            status="running",
            now_ns=now_ns,
            first_output_reason=(
                "provider-hook-not-reported"
                if self.capabilities.first_output_timing
                else "provider-complete-interface"
            ),
            progress_reason=_safe_reason(reason),
        )
        persisted = self._persist(event, f"{sequence:03d}-heartbeat.json")
        self.next_sequence += 1
        return persisted

    def completed(self) -> PersistedProviderDiagnostic:
        if self.terminal:
            raise _fail("SDAI-PROVIDER-DIAG-003", "provider diagnostic attempt is terminal")
        if self.ready_ns is None:
            raise _fail("SDAI-PROVIDER-DIAG-003", "provider was not marked ready")
        now_ns = self.clock.monotonic_ns()
        self.terminal = True
        sequence = self.next_sequence
        event = self._event(
            sequence=sequence,
            phase="completed",
            status="succeeded",
            now_ns=now_ns,
            first_output_reason=(
                "provider-hook-not-reported"
                if self.capabilities is not None and self.capabilities.first_output_timing
                else "provider-complete-interface"
            ),
        )
        return self._persist(event, f"{sequence:03d}-completed.json")

    def cancelled(self, error: ProviderCancelledError) -> PersistedProviderDiagnostic:
        if self.terminal:
            raise _fail("SDAI-PROVIDER-DIAG-003", "provider diagnostic attempt is terminal")
        now_ns = self.clock.monotonic_ns()
        self.terminal = True
        sequence = 1 if self.ready_ns is None else self.next_sequence
        event = self._event(
            sequence=sequence,
            phase="cancelled",
            status="cancelled",
            now_ns=now_ns,
            failure=_failure_classification(error, stage="invocation"),
            first_output_reason=(
                "provider-not-created"
                if self.ready_ns is None
                else (
                    "provider-hook-not-reported"
                    if self.capabilities is not None and self.capabilities.first_output_timing
                    else "provider-complete-interface"
                )
            ),
            progress_reason="cancelled-by-request",
        )
        return self._persist(event, f"{sequence:03d}-cancelled.json")

    def failed(self, error: BaseException, *, stage: str) -> PersistedProviderDiagnostic:
        if isinstance(error, ProviderCancelledError):
            return self.cancelled(error)
        if self.terminal:
            raise _fail("SDAI-PROVIDER-DIAG-003", "provider diagnostic attempt is terminal")
        if stage not in {"startup", "invocation"}:
            raise _fail("SDAI-PROVIDER-DIAG-001", "diagnostic failure stage is invalid")
        now_ns = self.clock.monotonic_ns()
        self.terminal = True
        sequence = 1 if self.ready_ns is None else self.next_sequence
        event = self._event(
            sequence=sequence,
            phase="failed",
            status="failed",
            now_ns=now_ns,
            failure=_failure_classification(error, stage=stage),
            first_output_reason=(
                "provider-not-created"
                if self.ready_ns is None
                else (
                    "provider-hook-not-reported"
                    if self.capabilities is not None and self.capabilities.first_output_timing
                    else "provider-complete-interface"
                )
            ),
        )
        return self._persist(event, f"{sequence:03d}-failed.json")


__all__ = [
    "PROVIDER_DIAGNOSTIC_API_VERSION",
    "PersistedProviderDiagnostic",
    "ProviderDiagnosticClock",
    "ProviderDiagnosticError",
    "ProviderDiagnosticEvent",
    "ProviderDiagnosticRecorder",
    "SystemProviderDiagnosticClock",
]
