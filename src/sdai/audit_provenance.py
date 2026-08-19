from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

from sdai.audit_contracts import (
    AUDIT_EVENT_API_VERSION,
    AUDIT_LEDGER_API_VERSION,
    AUDIT_EVENTS_RELATIVE_PATH,
    AuditProvenanceError,
    _BINDING_KINDS,
    _EVENT_CATEGORIES,
    _EVENT_KEYS,
    _SHA256,
    _ZERO_HASH,
    _canonical_bytes,
    _fail,
    _freeze_json,
    _git_commit,
    _reference,
    _sha256_bytes,
    _simple_id,
    _text,
    _thaw_json,
    _timestamp,
    _ACTOR_KINDS,
    _ACTION_KIND,
)
from sdai.models import validate_feature_id


@dataclass(frozen=True, slots=True)
class AuditActor:
    kind: str
    subject: str
    semantic_role: str | None = None
    provider: str | None = None
    model: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or self.kind not in _ACTOR_KINDS:
            raise _fail("SDAI-AUDIT-002", f"unsupported actor kind: {self.kind!r}")
        object.__setattr__(self, "subject", _text(self.subject, label="actor.subject", maximum=512))
        object.__setattr__(self, "semantic_role", _simple_id(self.semantic_role, label="actor.semanticRole", optional=True))
        object.__setattr__(self, "provider", _simple_id(self.provider, label="actor.provider", optional=True))
        object.__setattr__(self, "model", _text(self.model, label="actor.model", maximum=256, optional=True))
        if self.kind != "ai" and (self.provider is not None or self.model is not None):
            raise _fail("SDAI-AUDIT-002", "provider/model metadata is only valid for an ai actor")

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "subject": self.subject,
            "semanticRole": self.semantic_role,
            "provider": self.provider,
            "model": self.model,
        }

    @classmethod
    def from_mapping(cls, raw: object) -> "AuditActor":
        expected = {"kind", "subject", "semanticRole", "provider", "model"}
        if not isinstance(raw, Mapping) or set(raw) != expected:
            raise _fail("SDAI-AUDIT-005", "actor fields do not match sdai.audit-event/v1")
        if not isinstance(raw["kind"], str) or not isinstance(raw["subject"], str):
            raise _fail("SDAI-AUDIT-005", "actor kind/subject must be strings")
        for key in ("semanticRole", "provider", "model"):
            if raw[key] is not None and not isinstance(raw[key], str):
                raise _fail("SDAI-AUDIT-005", f"actor field {key} must be string or null")
        return cls(
            kind=raw["kind"],
            subject=raw["subject"],
            semantic_role=raw["semanticRole"],
            provider=raw["provider"],
            model=raw["model"],
        )


@dataclass(frozen=True, slots=True)
class AuditAction:
    kind: str
    subject: str
    reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or _ACTION_KIND.fullmatch(self.kind) is None:
            raise _fail("SDAI-AUDIT-002", f"invalid action kind: {self.kind!r}")
        object.__setattr__(self, "subject", _text(self.subject, label="action.subject", maximum=1024))
        object.__setattr__(self, "reason", _text(self.reason, label="action.reason", maximum=4096, optional=True))

    def to_dict(self) -> dict[str, object]:
        return {"kind": self.kind, "subject": self.subject, "reason": self.reason}

    @classmethod
    def from_mapping(cls, raw: object) -> "AuditAction":
        if not isinstance(raw, Mapping) or set(raw) != {"kind", "subject", "reason"}:
            raise _fail("SDAI-AUDIT-005", "action fields do not match sdai.audit-event/v1")
        if not isinstance(raw["kind"], str) or not isinstance(raw["subject"], str):
            raise _fail("SDAI-AUDIT-005", "action kind/subject must be strings")
        reason = raw["reason"]
        if reason is not None and not isinstance(reason, str):
            raise _fail("SDAI-AUDIT-005", "action reason must be string or null")
        return cls(raw["kind"], raw["subject"], reason)


@dataclass(frozen=True, slots=True)
class AuditExecution:
    run_id: str | None = None
    workflow: str | None = None
    step_id: str | None = None
    task_id: str | None = None
    git_commit: str | None = None
    workspace: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_id", _simple_id(self.run_id, label="execution.runId", optional=True))
        object.__setattr__(self, "workflow", _simple_id(self.workflow, label="execution.workflow", optional=True))
        object.__setattr__(self, "step_id", _simple_id(self.step_id, label="execution.stepId", optional=True))
        object.__setattr__(self, "task_id", _simple_id(self.task_id, label="execution.taskId", optional=True))
        object.__setattr__(self, "git_commit", _git_commit(self.git_commit))
        if self.workspace is not None:
            object.__setattr__(self, "workspace", _reference(self.workspace, label="execution.workspace"))

    def to_dict(self) -> dict[str, object]:
        return {
            "runId": self.run_id,
            "workflow": self.workflow,
            "stepId": self.step_id,
            "taskId": self.task_id,
            "gitCommit": self.git_commit,
            "workspace": self.workspace,
        }

    @classmethod
    def from_mapping(cls, raw: object) -> "AuditExecution":
        expected = {"runId", "workflow", "stepId", "taskId", "gitCommit", "workspace"}
        if not isinstance(raw, Mapping) or set(raw) != expected:
            raise _fail("SDAI-AUDIT-005", "execution fields do not match sdai.audit-event/v1")
        for key, value in raw.items():
            if value is not None and not isinstance(value, str):
                raise _fail("SDAI-AUDIT-005", f"execution field {key} must be string or null")
        return cls(
            run_id=raw["runId"],
            workflow=raw["workflow"],
            step_id=raw["stepId"],
            task_id=raw["taskId"],
            git_commit=raw["gitCommit"],
            workspace=raw["workspace"],
        )


@dataclass(frozen=True, slots=True)
class AuditBinding:
    kind: str
    source: str
    sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or self.kind not in _BINDING_KINDS:
            raise _fail("SDAI-AUDIT-002", f"unsupported audit binding kind: {self.kind!r}")
        object.__setattr__(self, "source", _reference(self.source, label="binding.source"))
        if not isinstance(self.sha256, str) or _SHA256.fullmatch(self.sha256) is None:
            raise _fail("SDAI-AUDIT-002", f"invalid SHA-256 audit binding: {self.sha256!r}")

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "source": self.source, "sha256": self.sha256}

    @classmethod
    def from_mapping(cls, raw: object) -> "AuditBinding":
        if not isinstance(raw, Mapping) or set(raw) != {"kind", "source", "sha256"}:
            raise _fail("SDAI-AUDIT-005", "audit binding must contain kind/source/sha256")
        if not all(isinstance(raw[key], str) for key in ("kind", "source", "sha256")):
            raise _fail("SDAI-AUDIT-005", "audit binding values must be strings")
        return cls(raw["kind"], raw["source"], raw["sha256"])


@dataclass(frozen=True, slots=True)
class AuditEvent:
    sequence: int
    event_id: str
    feature_id: str
    category: str
    occurred_at: str
    actor: AuditActor
    action: AuditAction
    execution: AuditExecution
    bindings: tuple[AuditBinding, ...]
    metadata: Mapping[str, object]
    previous_sha256: str
    sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.sequence, int) or isinstance(self.sequence, bool) or self.sequence < 1:
            raise _fail("SDAI-AUDIT-002", "audit sequence must be a positive integer")
        feature = validate_feature_id(self.feature_id)
        object.__setattr__(self, "feature_id", feature)
        expected_event_id = f"{feature}:{self.sequence:08d}"
        if self.event_id != expected_event_id:
            raise _fail("SDAI-AUDIT-002", f"eventId mismatch; expected {expected_event_id!r}")
        if not isinstance(self.category, str) or self.category not in _EVENT_CATEGORIES:
            raise _fail("SDAI-AUDIT-002", f"unsupported audit category: {self.category!r}")
        object.__setattr__(self, "occurred_at", _timestamp(self.occurred_at))
        if not isinstance(self.actor, AuditActor) or not isinstance(self.action, AuditAction):
            raise _fail("SDAI-AUDIT-002", "audit actor/action must be validated objects")
        if not isinstance(self.execution, AuditExecution):
            raise _fail("SDAI-AUDIT-002", "audit execution must be a validated object")
        try:
            raw_bindings = tuple(self.bindings)
        except TypeError as exc:
            raise _fail("SDAI-AUDIT-002", "audit bindings must be an iterable of validated objects") from exc
        if not all(isinstance(item, AuditBinding) for item in raw_bindings):
            raise _fail("SDAI-AUDIT-002", "audit bindings must be validated objects")
        ordered = tuple(sorted(raw_bindings, key=lambda item: (item.kind, item.source, item.sha256)))
        identities = [(item.kind, item.source) for item in ordered]
        if len(identities) != len(set(identities)):
            raise _fail("SDAI-AUDIT-002", "duplicate audit binding kind/source")
        object.__setattr__(self, "bindings", ordered)
        if not isinstance(self.metadata, Mapping):
            raise _fail("SDAI-AUDIT-002", "audit metadata must be a mapping")
        object.__setattr__(self, "metadata", _freeze_json(self.metadata, label="audit metadata"))
        if not isinstance(self.previous_sha256, str) or _SHA256.fullmatch(self.previous_sha256) is None:
            raise _fail("SDAI-AUDIT-002", "previousSha256 is invalid")
        if not isinstance(self.sha256, str) or _SHA256.fullmatch(self.sha256) is None:
            raise _fail("SDAI-AUDIT-002", "sha256 is invalid")

    def body_dict(self) -> dict[str, object]:
        return {
            "apiVersion": AUDIT_EVENT_API_VERSION,
            "sequence": self.sequence,
            "eventId": self.event_id,
            "featureId": self.feature_id,
            "category": self.category,
            "occurredAt": self.occurred_at,
            "actor": self.actor.to_dict(),
            "action": self.action.to_dict(),
            "execution": self.execution.to_dict(),
            "bindings": [item.to_dict() for item in self.bindings],
            "metadata": _thaw_json(self.metadata),
            "previousSha256": self.previous_sha256,
        }

    def to_dict(self) -> dict[str, object]:
        result = self.body_dict()
        result["sha256"] = self.sha256
        return result

    def to_json(self) -> str:
        return _canonical_bytes(self.to_dict()).decode("utf-8") + "\n"

    @classmethod
    def create(
        cls,
        *,
        sequence: int,
        feature_id: str,
        category: str,
        occurred_at: str,
        actor: AuditActor,
        action: AuditAction,
        execution: AuditExecution | None = None,
        bindings: Iterable[AuditBinding] = (),
        metadata: Mapping[str, object] | None = None,
        previous_sha256: str = _ZERO_HASH,
    ) -> "AuditEvent":
        if not isinstance(actor, AuditActor) or not isinstance(action, AuditAction):
            raise _fail("SDAI-AUDIT-002", "audit actor/action must be validated objects")
        if execution is not None and not isinstance(execution, AuditExecution):
            raise _fail("SDAI-AUDIT-002", "audit execution must be a validated object or null")
        feature = validate_feature_id(feature_id)
        normalized_time = _timestamp(occurred_at)
        try:
            raw_bindings = tuple(bindings)
        except TypeError as exc:
            raise _fail("SDAI-AUDIT-002", "audit bindings must be an iterable of validated objects") from exc
        if not all(isinstance(item, AuditBinding) for item in raw_bindings):
            raise _fail("SDAI-AUDIT-002", "audit bindings must be validated objects")
        ordered_bindings = tuple(sorted(raw_bindings, key=lambda item: (item.kind, item.source, item.sha256)))
        if metadata is not None and not isinstance(metadata, Mapping):
            raise _fail("SDAI-AUDIT-002", "audit metadata must be a mapping or null")
        frozen_metadata = _freeze_json(metadata or {}, label="audit metadata")
        body = {
            "apiVersion": AUDIT_EVENT_API_VERSION,
            "sequence": sequence,
            "eventId": f"{feature}:{sequence:08d}",
            "featureId": feature,
            "category": category,
            "occurredAt": normalized_time,
            "actor": actor.to_dict(),
            "action": action.to_dict(),
            "execution": (execution or AuditExecution()).to_dict(),
            "bindings": [item.to_dict() for item in ordered_bindings],
            "metadata": _thaw_json(frozen_metadata),
            "previousSha256": previous_sha256,
        }
        digest = _sha256_bytes(_canonical_bytes(body))
        return cls(
            sequence=sequence,
            event_id=body["eventId"],
            feature_id=feature,
            category=category,
            occurred_at=normalized_time,
            actor=actor,
            action=action,
            execution=execution or AuditExecution(),
            bindings=ordered_bindings,
            metadata=frozen_metadata,
            previous_sha256=previous_sha256,
            sha256=digest,
        )

    @classmethod
    def from_mapping(cls, raw: object) -> "AuditEvent":
        if not isinstance(raw, Mapping) or set(raw) != _EVENT_KEYS:
            raise _fail("SDAI-AUDIT-005", "event fields do not match sdai.audit-event/v1")
        if raw.get("apiVersion") != AUDIT_EVENT_API_VERSION:
            raise _fail("SDAI-AUDIT-005", "unsupported audit event apiVersion")
        sequence = raw.get("sequence")
        if not isinstance(sequence, int) or isinstance(sequence, bool):
            raise _fail("SDAI-AUDIT-005", "audit sequence must be an integer")
        for key in ("eventId", "featureId", "category", "occurredAt", "previousSha256", "sha256"):
            if not isinstance(raw.get(key), str):
                raise _fail("SDAI-AUDIT-005", f"event field {key} must be a string")
        raw_bindings = raw.get("bindings")
        if not isinstance(raw_bindings, list):
            raise _fail("SDAI-AUDIT-005", "event bindings must be a list")
        raw_metadata = raw.get("metadata")
        if not isinstance(raw_metadata, Mapping):
            raise _fail("SDAI-AUDIT-005", "event metadata must be a mapping")
        event = cls(
            sequence=sequence,
            event_id=raw["eventId"],
            feature_id=raw["featureId"],
            category=raw["category"],
            occurred_at=raw["occurredAt"],
            actor=AuditActor.from_mapping(raw.get("actor")),
            action=AuditAction.from_mapping(raw.get("action")),
            execution=AuditExecution.from_mapping(raw.get("execution")),
            bindings=tuple(AuditBinding.from_mapping(item) for item in raw_bindings),
            metadata=raw_metadata,
            previous_sha256=raw["previousSha256"],
            sha256=raw["sha256"],
        )
        expected = _sha256_bytes(_canonical_bytes(event.body_dict()))
        if event.sha256 != expected:
            raise _fail("SDAI-AUDIT-005", f"audit event {event.event_id} hash mismatch")
        return event


@dataclass(frozen=True, slots=True)
class AuditLedgerSnapshot:
    feature_id: str
    event_count: int
    head_sha256: str
    export_sha256: str

    def to_dict(self) -> dict[str, object]:
        return {
            "apiVersion": AUDIT_LEDGER_API_VERSION,
            "featureId": self.feature_id,
            "eventCount": self.event_count,
            "headSha256": self.head_sha256,
            "exportSha256": self.export_sha256,
        }


__all__ = [
    "AUDIT_EVENT_API_VERSION",
    "AUDIT_LEDGER_API_VERSION",
    "AUDIT_EVENTS_RELATIVE_PATH",
    "AuditAction",
    "AuditActor",
    "AuditBinding",
    "AuditEvent",
    "AuditExecution",
    "AuditLedgerSnapshot",
    "AuditProvenanceError",
]
