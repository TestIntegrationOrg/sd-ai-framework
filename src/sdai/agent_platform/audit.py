from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
from typing import Iterable, Mapping

from sdai.agent_platform.definitions import AgentDefinition
from sdai.agent_platform.models import AgentInvocation, Capability
from sdai.agent_platform.skills import load_skill
from sdai.audit_ledger import AuditLedger
from sdai.audit_provenance import (
    AuditAction,
    AuditActor,
    AuditBinding,
    AuditEvent,
    AuditExecution,
    AuditProvenanceError,
)
from sdai.models import validate_feature_id
from sdai.pack_lifecycle import load_install_state
from sdai.path_safety import PathSafetyError, ensure_within_project
from sdai.providers.base import ProviderUsage


_MODEL_ROUTING_API_VERSION = "sdai.model-routing/v1"
_ROUTING_KEYS = frozenset(
    {
        "apiVersion",
        "request",
        "policy_sources",
        "default_profile",
        "selected_profile",
        "selection_reason",
        "candidates",
        "sha256",
    }
)
_SHA256_PREFIX = "sha256:"
_MAX_SOURCE_BYTES = 8 * 1024 * 1024


class AgentAuditError(AuditProvenanceError):
    """Raised when governed agent provenance cannot be recorded safely."""


def _fail(code: str, message: str) -> AgentAuditError:
    return AgentAuditError(f"{code}: {message}")


def _safe_feature_id(value: object) -> str:
    if not isinstance(value, str):
        raise _fail("SDAI-AGENT-AUDIT-001", "feature id must be a string")
    try:
        return validate_feature_id(value)
    except ValueError as exc:
        raise _fail("SDAI-AGENT-AUDIT-001", f"invalid feature id: {value!r}") from exc


def _hash_bytes(value: bytes) -> str:
    return _SHA256_PREFIX + sha256(value).hexdigest()


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
        raise _fail("SDAI-AGENT-AUDIT-001", "agent provenance is not canonical JSON") from exc


def _relative(root: Path, path: Path, *, label: str) -> str:
    try:
        safe = ensure_within_project(root, path, label=label)
    except PathSafetyError as exc:
        raise _fail("SDAI-AGENT-AUDIT-002", f"{label} escapes the project workspace") from exc
    current = root
    relative = safe.relative_to(root)
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise _fail("SDAI-AGENT-AUDIT-002", f"{label} contains a symlink component")
    return relative.as_posix()


def _file_binding(root: Path, path: Path, *, kind: str, label: str) -> AuditBinding:
    relative = _relative(root, path, label=label)
    if not path.exists() or path.is_symlink() or not path.is_file():
        raise _fail("SDAI-AGENT-AUDIT-002", f"{label} must be a regular non-symlink file: {relative}")
    try:
        with path.open("rb") as stream:
            data = stream.read(_MAX_SOURCE_BYTES + 1)
    except OSError as exc:
        raise _fail("SDAI-AGENT-AUDIT-002", f"unable to read {label}: {relative}") from exc
    if len(data) > _MAX_SOURCE_BYTES:
        raise _fail("SDAI-AGENT-AUDIT-002", f"{label} exceeds the source size limit: {relative}")
    return AuditBinding(kind, relative, _hash_bytes(data))


def _value_binding(kind: str, source: str, value: str) -> AuditBinding:
    if not isinstance(value, str):
        raise _fail("SDAI-AGENT-AUDIT-001", f"{source} must be text before hashing")
    return AuditBinding(kind, source, _hash_bytes(value.encode("utf-8")))


def _routing_binding(serialized: str | None, *, expected_profile: str) -> AuditBinding | None:
    if serialized is None:
        return None
    if not isinstance(serialized, str) or not serialized.strip():
        raise _fail("SDAI-AGENT-AUDIT-003", "routing decision must be non-empty JSON text")
    try:
        raw = json.loads(serialized)
    except json.JSONDecodeError as exc:
        raise _fail("SDAI-AGENT-AUDIT-003", "routing decision is malformed JSON") from exc
    if not isinstance(raw, Mapping) or set(raw) != _ROUTING_KEYS:
        raise _fail("SDAI-AGENT-AUDIT-003", "routing decision fields do not match sdai.model-routing/v1")
    if raw.get("apiVersion") != _MODEL_ROUTING_API_VERSION:
        raise _fail("SDAI-AGENT-AUDIT-003", "routing decision does not use sdai.model-routing/v1")
    if raw.get("selected_profile") != expected_profile:
        raise _fail("SDAI-AGENT-AUDIT-003", "routing decision selected profile differs from invocation profile")
    if not isinstance(raw.get("request"), Mapping):
        raise _fail("SDAI-AGENT-AUDIT-003", "routing decision request must be a mapping")
    if not isinstance(raw.get("policy_sources"), list) or not all(
        isinstance(item, str) for item in raw["policy_sources"]
    ):
        raise _fail("SDAI-AGENT-AUDIT-003", "routing decision policy_sources must be a string list")
    if not isinstance(raw.get("candidates"), list) or not isinstance(raw.get("selection_reason"), str):
        raise _fail("SDAI-AGENT-AUDIT-003", "routing decision candidates/selection_reason are invalid")
    digest = raw.get("sha256")
    if not isinstance(digest, str) or not digest.startswith(_SHA256_PREFIX) or len(digest) != 71:
        raise _fail("SDAI-AGENT-AUDIT-003", "routing decision SHA-256 is invalid")
    if any(char not in "0123456789abcdef" for char in digest[7:]):
        raise _fail("SDAI-AGENT-AUDIT-003", "routing decision SHA-256 is invalid")
    body = dict(raw)
    body.pop("sha256", None)
    expected = _hash_bytes(_canonical_bytes(body))
    if digest != expected:
        raise _fail("SDAI-AGENT-AUDIT-003", "routing decision SHA-256 does not match its canonical body")
    return AuditBinding("context", "model-routing/decision", digest)


def _skill_files(root: Path, name: str, capability: Capability) -> tuple[AuditBinding, ...]:
    skill = load_skill(root, name)
    if skill.capabilities and capability not in skill.capabilities:
        return ()
    behavior = skill.root / "SKILL.md"
    bindings = [_file_binding(root, behavior, kind="artifact", label=f"skill {name} behavior")]
    canonical_sidecar = skill.root / "sdai.yaml"
    legacy_manifest = skill.root / "skill.yaml"
    if canonical_sidecar.exists():
        bindings.append(
            _file_binding(root, canonical_sidecar, kind="artifact", label=f"skill {name} sidecar")
        )
    elif legacy_manifest.exists():
        bindings.append(
            _file_binding(root, legacy_manifest, kind="artifact", label=f"skill {name} manifest")
        )
    return tuple(bindings)


def _pack_metadata(root: Path, source_paths: Iterable[str]) -> tuple[dict[str, object], ...]:
    selected = {
        source
        for source in source_paths
        if source.startswith(".sdai/installed-packs/")
    }
    if not selected:
        return ()
    state = load_install_state(root)
    rows: list[dict[str, object]] = []
    for pack in state.packs:
        managed = {item.path for item in pack.files}
        if not selected.intersection(managed):
            continue
        rows.append(
            {
                "identity": pack.identity,
                "manifestSha256": pack.manifest_sha256,
                "contentSha256": pack.content_sha256,
                "lockSha256": pack.lock_sha256,
                "mode": pack.mode,
            }
        )
    return tuple(sorted(rows, key=lambda row: str(row["identity"])))


def _feature_workspace_exists(root: Path, feature_id: str) -> bool:
    modern = root / "specs" / "changes" / feature_id
    legacy = root / "specs" / feature_id
    return modern.exists() or legacy.exists() or modern.is_symlink() or legacy.is_symlink()


@dataclass(frozen=True, slots=True)
class AgentInvocationProvenance:
    bindings: tuple[AuditBinding, ...]
    metadata: Mapping[str, object]


@dataclass(slots=True)
class AgentAuditRecorder:
    root: Path
    feature_id: str
    ledger: AuditLedger
    terminal_usage: ProviderUsage | None = None
    effective_profile: str | None = None
    effective_provider: str | None = None
    host_reused: bool = False

    @classmethod
    def optional_for(cls, project_root: Path, feature_id: str) -> "AgentAuditRecorder | None":
        root = project_root.resolve()
        feature = _safe_feature_id(feature_id)
        if not _feature_workspace_exists(root, feature):
            return None
        return cls(root=root, feature_id=feature, ledger=AuditLedger(root, feature))

    def prepare(
        self,
        invocation: AgentInvocation,
        *,
        prompt_name: str,
        definition: AgentDefinition | None,
        effective_skill_names: tuple[str, ...],
    ) -> AgentInvocationProvenance:
        if invocation.feature_id != self.feature_id:
            raise _fail("SDAI-AGENT-AUDIT-001", "invocation feature does not match audit ledger feature")
        bindings: list[AuditBinding] = []
        source_paths: list[str] = []

        profile_path = self.root / ".sdai" / "agents.yaml"
        profile_binding = _file_binding(self.root, profile_path, kind="context", label="agent profile configuration")
        bindings.append(profile_binding)
        source_paths.append(profile_binding.source)

        prompt_path = self.root / ".sdai" / "prompts" / prompt_name
        prompt_binding = _file_binding(self.root, prompt_path, kind="artifact", label="agent prompt template")
        bindings.append(prompt_binding)
        source_paths.append(prompt_binding.source)

        if definition is not None:
            agent_binding = _file_binding(
                self.root,
                definition.path,
                kind="artifact",
                label=f"semantic agent {definition.name}",
            )
            bindings.append(agent_binding)
            source_paths.append(agent_binding.source)

        selected_skills: list[str] = []
        for name in effective_skill_names:
            skill_bindings = _skill_files(self.root, name, invocation.capability)
            if not skill_bindings:
                continue
            selected_skills.append(name)
            bindings.extend(skill_bindings)
            source_paths.extend(item.source for item in skill_bindings)

        bindings.append(_value_binding("context", "agent-invocation/system", invocation.system))
        bindings.append(_value_binding("input", "agent-invocation/prompt", invocation.prompt))
        routing = _routing_binding(
            invocation.routing_decision,
            expected_profile=invocation.profile.name,
        )
        if routing is not None:
            bindings.append(routing)

        packs = _pack_metadata(self.root, source_paths)
        metadata: dict[str, object] = {
            "profile": invocation.profile.name,
            "capability": invocation.capability.value,
            "executionMode": invocation.mode.value,
            "semanticAgent": invocation.agent_name,
            "skills": selected_skills,
            "packs": list(packs),
        }
        if routing is not None:
            metadata["routingDecisionSha256"] = routing.sha256
        return AgentInvocationProvenance(tuple(bindings), metadata)

    def _actor(self, invocation: AgentInvocation) -> AuditActor:
        subject = (
            f"agent:{invocation.agent_name}"
            if invocation.agent_name
            else f"profile:{invocation.profile.name}"
        )
        return AuditActor(
            "ai",
            subject,
            semantic_role=invocation.agent_name or invocation.capability.value,
            provider=invocation.profile.provider,
            model=invocation.profile.model,
        )

    def started(
        self,
        invocation: AgentInvocation,
        provenance: AgentInvocationProvenance,
    ) -> AuditEvent:
        return self.ledger.append(
            category="ai",
            actor=self._actor(invocation),
            action=AuditAction("agent.execution.started", f"feature:{self.feature_id}"),
            execution=AuditExecution(workflow="agent-runtime"),
            bindings=provenance.bindings,
            metadata={**dict(provenance.metadata), "status": "started"},
        )

    def succeeded(
        self,
        invocation: AgentInvocation,
        provenance: AgentInvocationProvenance,
        *,
        output: str,
        started_event: AuditEvent,
    ) -> AuditEvent:
        terminal_bindings = (
            *provenance.bindings,
            AuditBinding("evidence", "agent-execution/start-event", started_event.sha256),
            _value_binding("output", "agent-invocation/output", output),
        )
        return self.ledger.append(
            category="ai",
            actor=self._actor(invocation),
            action=AuditAction("agent.execution.succeeded", f"feature:{self.feature_id}"),
            execution=AuditExecution(workflow="agent-runtime"),
            bindings=terminal_bindings,
            metadata={
                **dict(provenance.metadata),
                "status": "succeeded",
                "usage": (self.terminal_usage or ProviderUsage.unavailable()).as_dict(),
                **(
                    {
                        "providerSelection": {
                            "requestedProfile": invocation.profile.name,
                            "requestedProvider": invocation.profile.provider,
                            "effectiveProfile": self.effective_profile,
                            "effectiveProvider": self.effective_provider,
                            "hostReused": True,
                        }
                    }
                    if self.host_reused
                    else {}
                ),
            },
        )

    def failed(
        self,
        invocation: AgentInvocation,
        provenance: AgentInvocationProvenance,
        *,
        error: BaseException,
        started_event: AuditEvent,
    ) -> AuditEvent:
        terminal_bindings = (
            *provenance.bindings,
            AuditBinding("evidence", "agent-execution/start-event", started_event.sha256),
        )
        return self.ledger.append(
            category="ai",
            actor=self._actor(invocation),
            action=AuditAction("agent.execution.failed", f"feature:{self.feature_id}"),
            execution=AuditExecution(workflow="agent-runtime"),
            bindings=terminal_bindings,
            metadata={
                **dict(provenance.metadata),
                "status": "failed",
                "failureType": type(error).__name__[:128] or "Exception",
                "usage": (
                    self.terminal_usage
                    or ProviderUsage.unavailable("failed-before-usage-reported")
                ).as_dict(),
                **(
                    {
                        "providerSelection": {
                            "requestedProfile": invocation.profile.name,
                            "requestedProvider": invocation.profile.provider,
                            "effectiveProfile": self.effective_profile,
                            "effectiveProvider": self.effective_provider,
                            "hostReused": True,
                        }
                    }
                    if self.host_reused
                    else {}
                ),
            },
        )


__all__ = [
    "AgentAuditError",
    "AgentAuditRecorder",
    "AgentInvocationProvenance",
]
