from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Iterable, Mapping

from sdai.audit_ledger import AuditLedger
from sdai.audit_provenance import (
    AuditAction,
    AuditActor,
    AuditBinding,
    AuditEvent,
    AuditExecution,
)
from sdai.models import FeatureContext, validate_feature_id
from sdai.path_safety import PathSafetyError, ensure_within_project
from sdai.policy import EffectiveConfiguration
from sdai.workflows import WorkflowDefinition, WorkflowState, WorkflowStep, approval_path


_MAX_SOURCE_BYTES = 8 * 1024 * 1024
_GOVERNANCE_SOURCES = (
    ".sdai/constitution.yaml",
    ".sdai/policies.yaml",
    ".sdai/governance.yaml",
    ".sdai/approval-policies.yaml",
    ".sdai/quality-gates.yaml",
    ".sdai/integrations.yaml",
    ".sdai/policy.yaml",
)


class WorkflowAuditError(RuntimeError):
    """Raised when workflow/authority provenance cannot be recorded safely."""


def _fail(code: str, message: str) -> WorkflowAuditError:
    return WorkflowAuditError(f"{code}: {message}")


def _hash_bytes(value: bytes) -> str:
    return "sha256:" + sha256(value).hexdigest()


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
        raise _fail("SDAI-WORKFLOW-AUDIT-001", "workflow provenance is not canonical JSON") from exc


def _canonical_binding(kind: str, source: str, value: object) -> AuditBinding:
    return AuditBinding(kind, source, _hash_bytes(_canonical_bytes(value)))


def _internal_file_binding(root: Path, relative: str, *, kind: str) -> AuditBinding:
    candidate = root / relative
    try:
        safe = ensure_within_project(root, candidate, label=f"audit source {relative}")
    except PathSafetyError as exc:
        raise _fail("SDAI-WORKFLOW-AUDIT-002", f"audit source escapes project: {relative}") from exc
    current = root
    for part in safe.relative_to(root).parts:
        current = current / part
        if current.is_symlink():
            raise _fail("SDAI-WORKFLOW-AUDIT-002", f"audit source contains a symlink: {relative}")
    if not safe.is_file():
        raise _fail("SDAI-WORKFLOW-AUDIT-002", f"audit source is not a regular file: {relative}")
    try:
        with safe.open("rb") as stream:
            content = stream.read(_MAX_SOURCE_BYTES + 1)
    except OSError as exc:
        raise _fail("SDAI-WORKFLOW-AUDIT-002", f"unable to read audit source: {relative}") from exc
    if len(content) > _MAX_SOURCE_BYTES:
        raise _fail("SDAI-WORKFLOW-AUDIT-002", f"audit source exceeds size limit: {relative}")
    return AuditBinding(kind, relative, _hash_bytes(content))


def _external_file_binding(path: Path, *, kind: str, source: str) -> AuditBinding:
    if path.is_symlink() or not path.is_file():
        raise _fail("SDAI-WORKFLOW-AUDIT-002", f"external {source} must be a regular non-symlink file")
    try:
        with path.open("rb") as stream:
            content = stream.read(_MAX_SOURCE_BYTES + 1)
    except OSError as exc:
        raise _fail("SDAI-WORKFLOW-AUDIT-002", f"unable to read external {source}") from exc
    if len(content) > _MAX_SOURCE_BYTES:
        raise _fail("SDAI-WORKFLOW-AUDIT-002", f"external {source} exceeds size limit")
    return AuditBinding(kind, source, _hash_bytes(content))


def _safe_feature(value: object) -> str:
    if not isinstance(value, str):
        raise _fail("SDAI-WORKFLOW-AUDIT-001", "feature id must be a string")
    try:
        return validate_feature_id(value)
    except ValueError as exc:
        raise _fail("SDAI-WORKFLOW-AUDIT-001", f"invalid feature id: {value!r}") from exc


def _workflow_exists(root: Path, feature: str) -> bool:
    modern = root / "specs" / "changes" / feature
    legacy = root / "specs" / feature
    return modern.exists() or legacy.exists() or modern.is_symlink() or legacy.is_symlink()


def _step_projection(step: WorkflowStep) -> dict[str, object]:
    return {
        "id": step.id,
        "kind": step.kind.value,
        "action": step.action,
        "capability": step.capability.value if step.capability else None,
        "agent": step.agent_name,
        "profile": step.profile,
        "mode": step.mode.value,
        "saveAs": step.save_as,
        "gate": step.gate,
        "qualityGate": step.quality_gate,
        "plugin": step.plugin_id,
        "pluginInputKeys": sorted(key for key, _ in step.plugin_inputs),
        "condition": step.condition,
        "retry": {
            "maxAttempts": step.retry.max_attempts,
            "delaySeconds": step.retry.delay_seconds,
            "backoffMultiplier": step.retry.backoff_multiplier,
        },
        "onFailure": step.on_failure.value,
        "children": [_step_projection(child) for child in step.children],
    }


def _input_definition_projection(item: object) -> dict[str, object]:
    payload = item.as_dict()
    if getattr(item, "sensitive", False):
        payload.pop("default", None)
        payload.pop("enum", None)
        payload["sensitive"] = True
        payload["secretValuesPersisted"] = False
    return payload


def _workflow_projection(definition: WorkflowDefinition) -> dict[str, object]:
    definitions = {item.name: item for item in definition.input_definitions}
    resolved: dict[str, object] = {}
    sensitive_names: list[str] = []
    for name, value in definition.resolved_inputs:
        spec = definitions.get(name)
        if spec is not None and spec.sensitive:
            resolved[name] = {"sensitive": True, "present": True}
            sensitive_names.append(name)
        else:
            resolved[name] = value
    return {
        "name": definition.name,
        "version": definition.workflow_version,
        "validationMode": definition.validation_mode.value,
        "inputs": [_input_definition_projection(item) for item in definition.input_definitions],
        "resolvedInputs": resolved,
        "sensitiveInputNames": sorted(sensitive_names),
        "steps": [_step_projection(step) for step in definition.steps],
        "components": [item.as_dict() for item in definition.components],
        "inheritance": list(definition.inheritance),
        "overlays": [item.as_dict() for item in definition.overlays],
        "lifecycleHooks": [item.as_dict() for item in definition.lifecycle_hooks],
        "mandatorySteps": list(definition.mandatory_steps),
    }


def _policy_projection(policy: EffectiveConfiguration) -> dict[str, object]:
    def values(value):
        return None if value is None else sorted(value)

    def keyed(mapping):
        return {key: sorted(value) for key, value in sorted(mapping.items())}

    return {
        "operatingMode": policy.operating_mode.value,
        "allowedProfiles": values(policy.allowed_profiles),
        "allowedProviders": values(policy.allowed_providers),
        "allowedModels": keyed(policy.allowed_models),
        "capabilityProfiles": keyed(policy.capability_profiles),
        "capabilityProviders": keyed(policy.capability_providers),
        "workspaceWrite": policy.workspace_write,
        "requirePriorApprovalForWorkspaceWrite": policy.require_prior_approval_for_workspace_write,
        "allowForceApprovalBypass": policy.allow_force_approval_bypass,
        "protectedPaths": list(policy.protected_paths),
        "environmentAllowlist": values(policy.environment_allowlist),
        "requiredSkills": {key: list(value) for key, value in sorted(policy.required_skills_map.items())},
        "requiredArchitectureArtifacts": {
            key: list(value) for key, value in sorted(policy.required_architecture_artifacts_map.items())
        },
        "architectureAllowWaivers": policy.architecture_allow_waivers,
    }


def _state_projection(state: WorkflowState) -> dict[str, object]:
    return {
        "featureId": state.feature_id,
        "workflow": state.workflow,
        "completedSteps": list(state.completed_steps),
        "lastStatus": state.last_status,
        "pausedAt": state.paused_at,
    }


def _dedupe(bindings: Iterable[AuditBinding]) -> tuple[AuditBinding, ...]:
    by_identity: dict[tuple[str, str], AuditBinding] = {}
    for item in bindings:
        identity = (item.kind, item.source)
        previous = by_identity.get(identity)
        if previous is not None and previous.sha256 != item.sha256:
            raise _fail(
                "SDAI-WORKFLOW-AUDIT-003",
                f"conflicting audit binding for {item.kind}:{item.source}",
            )
        by_identity[identity] = item
    return tuple(sorted(by_identity.values(), key=lambda item: (item.kind, item.source, item.sha256)))


def _workflow_source_bindings(root: Path, definition: WorkflowDefinition) -> tuple[AuditBinding, ...]:
    result: list[AuditBinding] = []
    names = list(definition.inheritance)
    if definition.name not in names:
        names.append(definition.name)
    for name in names:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", name):
            raise _fail("SDAI-WORKFLOW-AUDIT-003", f"unsafe workflow provenance name: {name!r}")
        relative = f".sdai/workflows/{name}.yaml"
        if (root / relative).is_file():
            result.append(_internal_file_binding(root, relative, kind="workflow"))

    for component in definition.components:
        source = component.source
        if Path(source).is_absolute():
            result.append(
                _external_file_binding(
                    Path(source),
                    kind="workflow",
                    source=f"workflow-component/{component.component_id}/{component.version}",
                )
            )
        else:
            result.append(_internal_file_binding(root, source, kind="workflow"))

    for overlay in definition.overlays:
        source = overlay.source
        if Path(source).is_absolute():
            result.append(
                _external_file_binding(
                    Path(source),
                    kind="workflow",
                    source=f"workflow-overlay/{overlay.layer.value}/{overlay.overlay_id}",
                )
            )
        else:
            result.append(_internal_file_binding(root, source, kind="workflow"))
    return _dedupe(result)


def _policy_source_bindings(root: Path, policy: EffectiveConfiguration) -> tuple[AuditBinding, ...]:
    result: list[AuditBinding] = []
    config = root / ".sdai" / "config.yaml"
    if config.is_file():
        result.append(_internal_file_binding(root, ".sdai/config.yaml", kind="policy"))
    for raw in policy.sources:
        if raw.startswith("repository:"):
            relative = raw.split(":", 1)[1]
            result.append(_internal_file_binding(root, relative, kind="policy"))
        elif raw.startswith("organization:"):
            path = Path(raw.split(":", 1)[1])
            result.append(_external_file_binding(path, kind="policy", source="policy-source/organization"))
        elif raw.startswith("user:"):
            path = Path(raw.split(":", 1)[1])
            result.append(_external_file_binding(path, kind="policy", source="policy-source/user"))
        else:
            raise _fail("SDAI-WORKFLOW-AUDIT-003", f"unsupported policy source label: {raw!r}")
    result.append(_canonical_binding("policy", "policy/effective", _policy_projection(policy)))
    return _dedupe(result)


def _governance_bindings(root: Path) -> tuple[AuditBinding, ...]:
    result: list[AuditBinding] = []
    for relative in _GOVERNANCE_SOURCES:
        if not (root / relative).is_file():
            continue
        kind = "constitution" if relative == ".sdai/constitution.yaml" else "policy"
        result.append(_internal_file_binding(root, relative, kind=kind))
    return _dedupe(result)


def _file_output_binding(root: Path, feature_dir: Path, path: Path) -> AuditBinding | None:
    try:
        safe = ensure_within_project(feature_dir, path, label="workflow output artifact")
    except PathSafetyError:
        return None
    if safe.is_symlink() or not safe.is_file():
        return None
    relative = safe.relative_to(root).as_posix()
    return _internal_file_binding(root, relative, kind="output")


def _execution_output_bindings(root: Path, context: FeatureContext, execution: object) -> tuple[AuditBinding, ...]:
    result: list[AuditBinding] = []
    message = getattr(execution, "message", "")
    if isinstance(message, str) and message:
        candidate_text = message.split("evidence=", 1)[1] if "evidence=" in message else message
        if ";" not in candidate_text and "\n" not in candidate_text:
            candidate = root / candidate_text
            binding = _file_output_binding(root, context.feature_dir, candidate)
            if binding is not None:
                result.append(binding)
    payload = getattr(execution, "result", None)
    artifact = getattr(payload, "artifact", None)
    if isinstance(artifact, Path):
        binding = _file_output_binding(root, context.feature_dir, artifact)
        if binding is not None:
            result.append(binding)
    artifacts = getattr(payload, "artifacts", None)
    if isinstance(artifacts, (list, tuple)):
        for path in artifacts:
            if not isinstance(path, Path):
                continue
            binding = _file_output_binding(root, context.feature_dir, path)
            if binding is not None:
                result.append(binding)
    if isinstance(payload, list):
        for child in payload:
            result.extend(_execution_output_bindings(root, context, child))
    return _dedupe(result)


def _agent_event_bindings(events: Iterable[AuditEvent], *, after_sequence: int) -> tuple[AuditBinding, ...]:
    result: list[AuditBinding] = []
    for event in events:
        if event.sequence <= after_sequence:
            continue
        if not event.action.kind.startswith("agent.execution."):
            continue
        result.append(
            AuditBinding(
                "evidence",
                f"workflow/agent-event/{event.event_id}",
                event.sha256,
            )
        )
    return _dedupe(result)


@dataclass(frozen=True, slots=True)
class WorkflowAuditProvenance:
    bindings: tuple[AuditBinding, ...]
    metadata: Mapping[str, object]


@dataclass(slots=True)
class WorkflowAuditRecorder:
    root: Path
    feature_id: str
    ledger: AuditLedger
    policy: EffectiveConfiguration

    @classmethod
    def optional_for(
        cls,
        project_root: Path,
        feature_id: str,
        policy: EffectiveConfiguration,
    ) -> "WorkflowAuditRecorder | None":
        root = project_root.resolve()
        feature = _safe_feature(feature_id)
        if not _workflow_exists(root, feature):
            return None
        return cls(root, feature, AuditLedger(root, feature), policy)

    def prepare(self, definition: WorkflowDefinition) -> WorkflowAuditProvenance:
        bindings = _dedupe(
            (
                *_workflow_source_bindings(self.root, definition),
                *_policy_source_bindings(self.root, self.policy),
                *_governance_bindings(self.root),
                _canonical_binding("workflow", f"workflow/resolved/{definition.name}", _workflow_projection(definition)),
            )
        )
        sensitive = sorted(
            item.name for item in definition.input_definitions if item.sensitive and item.name in definition.input_values
        )
        metadata = {
            "workflow": definition.name,
            "workflowVersion": definition.workflow_version,
            "validationMode": definition.validation_mode.value,
            "operatingMode": self.policy.operating_mode.value,
            "sensitiveInputNames": sensitive,
            "componentCount": len(definition.components),
            "overlayCount": len(definition.overlays),
            "hookCount": len(definition.lifecycle_hooks),
        }
        return WorkflowAuditProvenance(bindings, metadata)

    def _actor(self, definition: WorkflowDefinition) -> AuditActor:
        return AuditActor(
            "workflow",
            f"workflow:{definition.name}",
            semantic_role="orchestrator",
        )

    def workflow_started(
        self,
        definition: WorkflowDefinition,
        provenance: WorkflowAuditProvenance,
        state: WorkflowState,
    ) -> AuditEvent:
        return self.ledger.append(
            category="workflow",
            actor=self._actor(definition),
            action=AuditAction("workflow.execution.started", f"feature:{self.feature_id}"),
            execution=AuditExecution(workflow=definition.name),
            bindings=_dedupe(
                (*provenance.bindings, _canonical_binding("context", "workflow/state/before", _state_projection(state)))
            ),
            metadata={**dict(provenance.metadata), "status": "started"},
        )

    def workflow_terminal(
        self,
        definition: WorkflowDefinition,
        provenance: WorkflowAuditProvenance,
        state: WorkflowState,
        *,
        started_event: AuditEvent,
        status: str,
        failure: BaseException | None = None,
    ) -> AuditEvent:
        events = self.ledger.read()
        bindings = _dedupe(
            (
                *provenance.bindings,
                AuditBinding("evidence", "workflow/start-event", started_event.sha256),
                _canonical_binding("context", "workflow/state/after", _state_projection(state)),
                *_agent_event_bindings(events, after_sequence=started_event.sequence),
            )
        )
        metadata: dict[str, object] = {**dict(provenance.metadata), "status": status}
        if failure is not None:
            metadata["failureType"] = type(failure).__name__[:128] or "Exception"
        action_by_status = {
            "completed": "workflow.execution.completed",
            "paused": "workflow.execution.paused",
            "cancelled": "workflow.execution.cancelled",
            "failed": "workflow.execution.failed",
        }
        action = action_by_status.get(status, "workflow.execution.failed")
        return self.ledger.append(
            category="workflow",
            actor=self._actor(definition),
            action=AuditAction(action, f"feature:{self.feature_id}"),
            execution=AuditExecution(workflow=definition.name),
            bindings=bindings,
            metadata=metadata,
        )

    def step_started(
        self,
        definition: WorkflowDefinition,
        step: WorkflowStep,
        provenance: WorkflowAuditProvenance,
        state: WorkflowState,
        *,
        force: bool,
        dry_run: bool,
        effective_mode: str | None = None,
    ) -> AuditEvent:
        metadata = {
            **dict(provenance.metadata),
            "status": "started",
            "stepId": step.id,
            "stepKind": step.kind.value,
            "force": force,
            "dryRun": dry_run,
            "effectiveMode": effective_mode,
            "parentStepId": definition.parent_id(step.id),
        }
        return self.ledger.append(
            category="workflow",
            actor=self._actor(definition),
            action=AuditAction("workflow.step.started", f"step:{step.id}"),
            execution=AuditExecution(workflow=definition.name, step_id=step.id),
            bindings=_dedupe(
                (*provenance.bindings, _canonical_binding("context", "workflow/state/before", _state_projection(state)))
            ),
            metadata=metadata,
        )

    def step_terminal(
        self,
        context: FeatureContext,
        definition: WorkflowDefinition,
        step: WorkflowStep,
        provenance: WorkflowAuditProvenance,
        state: WorkflowState,
        execution_result: object,
        *,
        started_event: AuditEvent,
        failure: BaseException | None = None,
    ) -> AuditEvent:
        events = self.ledger.read()
        status = getattr(execution_result, "status", "failed" if failure else "unknown")
        attempts = getattr(execution_result, "attempts", 0)
        bindings: list[AuditBinding] = [
            *provenance.bindings,
            AuditBinding("evidence", f"workflow/step-start/{step.id}", started_event.sha256),
            _canonical_binding("context", "workflow/state/after", _state_projection(state)),
            *_agent_event_bindings(events, after_sequence=started_event.sequence),
            *_execution_output_bindings(self.root, context, execution_result),
        ]
        if step.kind.value == "approval":
            gate = step.gate or step.id
            approval = approval_path(context, gate)
            if approval.is_file() and not approval.is_symlink():
                relative = approval.relative_to(self.root).as_posix()
                bindings.append(_internal_file_binding(self.root, relative, kind="evidence"))
        metadata: dict[str, object] = {
            **dict(provenance.metadata),
            "status": str(status),
            "stepId": step.id,
            "stepKind": step.kind.value,
            "attempts": int(attempts) if isinstance(attempts, int) else 0,
            "parentStepId": definition.parent_id(step.id),
        }
        payload = getattr(execution_result, "result", None)
        if isinstance(payload, list):
            metadata["children"] = [
                {
                    "stepId": getattr(child, "step_id", "unknown"),
                    "status": getattr(child, "status", "unknown"),
                    "attempts": getattr(child, "attempts", 0),
                }
                for child in payload
            ]
        if failure is not None:
            metadata["failureType"] = type(failure).__name__[:128] or "Exception"
        action_by_status = {
            "completed": "workflow.step.completed",
            "skipped": "workflow.step.skipped",
            "condition-skipped": "workflow.step.skipped",
            "dry-run": "workflow.step.dry-run",
            "paused": "workflow.step.paused",
            "cancelled": "workflow.step.cancelled",
            "failed": "workflow.step.failed",
        }
        action = action_by_status.get(str(status), "workflow.step.failed")
        return self.ledger.append(
            category="workflow",
            actor=self._actor(definition),
            action=AuditAction(action, f"step:{step.id}"),
            execution=AuditExecution(workflow=definition.name, step_id=step.id),
            bindings=_dedupe(bindings),
            metadata=metadata,
        )


__all__ = ["WorkflowAuditError", "WorkflowAuditProvenance", "WorkflowAuditRecorder"]
