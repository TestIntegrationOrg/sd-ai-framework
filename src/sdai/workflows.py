from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml

from sdai.agent_platform import Capability, ExecutionMode
from sdai.artifacts import write_text
from sdai.config import load_yaml
from sdai.models import FeatureContext, LifecycleMode


class WorkflowConfigError(RuntimeError):
    pass


class StepKind(StrEnum):
    DETERMINISTIC = "deterministic"
    AGENT = "agent"
    APPROVAL = "approval"
    VALIDATE = "validate"


@dataclass(frozen=True)
class WorkflowStep:
    id: str
    kind: StepKind
    action: str | None = None
    capability: Capability | None = None
    profile: str | None = None
    mode: ExecutionMode = ExecutionMode.ADVISORY
    save_as: str | None = None
    gate: str | None = None
    description: str | None = None


@dataclass(frozen=True)
class WorkflowDefinition:
    name: str
    validation_mode: LifecycleMode
    steps: tuple[WorkflowStep, ...]

    def step(self, step_id: str) -> WorkflowStep:
        for item in self.steps:
            if item.id == step_id:
                return item
        raise WorkflowConfigError(f"Unknown step '{step_id}' in workflow '{self.name}'")


@dataclass
class WorkflowState:
    feature_id: str
    workflow: str
    completed_steps: list[str] = field(default_factory=list)
    last_status: str = "new"
    paused_at: str | None = None

    def is_complete(self, step_id: str) -> bool:
        return step_id in self.completed_steps

    def mark_complete(self, step_id: str) -> None:
        if step_id not in self.completed_steps:
            self.completed_steps.append(step_id)
        self.last_status = "running"
        self.paused_at = None


@dataclass(frozen=True)
class ApprovalRecord:
    gate: str
    approved_by: str
    approved_at: str
    note: str = ""


def _parse_step(raw: object, index: int) -> WorkflowStep:
    # Backward compatibility with v0.1/v0.2 workflows that used a list of strings.
    if isinstance(raw, str):
        if raw == "validate":
            return WorkflowStep(id="validate", kind=StepKind.VALIDATE)
        return WorkflowStep(id=raw, kind=StepKind.DETERMINISTIC, action=raw)

    if not isinstance(raw, dict):
        raise WorkflowConfigError(f"Workflow step #{index + 1} must be a string or mapping")

    step_id = str(raw.get("id") or "").strip()
    kind_value = str(raw.get("type") or raw.get("kind") or "").strip()
    if not step_id:
        raise WorkflowConfigError(f"Workflow step #{index + 1} is missing id")
    try:
        kind = StepKind(kind_value)
    except ValueError as exc:
        raise WorkflowConfigError(f"Step '{step_id}' has unsupported type '{kind_value}'") from exc

    if kind == StepKind.DETERMINISTIC:
        action = str(raw.get("action") or step_id).strip()
        return WorkflowStep(id=step_id, kind=kind, action=action, description=raw.get("description"))

    if kind == StepKind.AGENT:
        capability_value = str(raw.get("capability") or "").strip()
        if not capability_value:
            raise WorkflowConfigError(f"Agent step '{step_id}' is missing capability")
        try:
            capability = Capability(capability_value)
        except ValueError as exc:
            raise WorkflowConfigError(
                f"Agent step '{step_id}' has unsupported capability '{capability_value}'"
            ) from exc
        try:
            mode = ExecutionMode(str(raw.get("mode") or ExecutionMode.ADVISORY.value))
        except ValueError as exc:
            raise WorkflowConfigError(f"Agent step '{step_id}' has invalid execution mode") from exc
        return WorkflowStep(
            id=step_id,
            kind=kind,
            capability=capability,
            profile=str(raw["profile"]) if raw.get("profile") else None,
            mode=mode,
            save_as=str(raw["save_as"]) if raw.get("save_as") else f"ai/{step_id}.md",
            description=str(raw["description"]) if raw.get("description") else None,
        )

    if kind == StepKind.APPROVAL:
        gate = str(raw.get("gate") or step_id).strip()
        return WorkflowStep(id=step_id, kind=kind, gate=gate, description=raw.get("description"))

    return WorkflowStep(id=step_id, kind=StepKind.VALIDATE, description=raw.get("description"))


def load_workflow(project_root: Path, name: str) -> WorkflowDefinition:
    path = project_root / ".sdai" / "workflows" / f"{name}.yaml"
    data = load_yaml(path)
    raw_steps = data.get("steps") or []
    if not isinstance(raw_steps, list) or not raw_steps:
        raise WorkflowConfigError(f"Workflow '{name}' must define at least one step")
    try:
        validation_mode = LifecycleMode(str(data.get("validation_mode") or name))
    except ValueError as exc:
        raise WorkflowConfigError(
            f"Workflow '{name}' must define validation_mode as light, standard, or critical"
        ) from exc
    steps = tuple(_parse_step(raw, index) for index, raw in enumerate(raw_steps))
    ids = [step.id for step in steps]
    if len(ids) != len(set(ids)):
        raise WorkflowConfigError(f"Workflow '{name}' contains duplicate step ids")
    return WorkflowDefinition(
        name=str(data.get("name") or name),
        validation_mode=validation_mode,
        steps=steps,
    )


def _state_path(context: FeatureContext, workflow: str) -> Path:
    return context.artifact(f".sdai/workflows/{workflow}.yaml")


def load_workflow_state(context: FeatureContext, workflow: str) -> WorkflowState:
    path = _state_path(context, workflow)
    if not path.exists():
        return WorkflowState(feature_id=context.feature_id, workflow=workflow)
    data = load_yaml(path)
    return WorkflowState(
        feature_id=context.feature_id,
        workflow=workflow,
        completed_steps=[str(value) for value in (data.get("completed_steps") or [])],
        last_status=str(data.get("last_status") or "new"),
        paused_at=str(data["paused_at"]) if data.get("paused_at") else None,
    )


def save_workflow_state(context: FeatureContext, state: WorkflowState) -> Path:
    payload = {
        "version": 1,
        "feature_id": state.feature_id,
        "workflow": state.workflow,
        "completed_steps": state.completed_steps,
        "last_status": state.last_status,
        "paused_at": state.paused_at,
    }
    return write_text(_state_path(context, state.workflow), yaml.safe_dump(payload, sort_keys=False))


def approval_path(context: FeatureContext, gate: str) -> Path:
    return context.artifact(f"approvals/{gate}.yaml")


def grant_approval(context: FeatureContext, gate: str, *, approved_by: str, note: str = "") -> ApprovalRecord:
    if not approved_by.strip():
        raise ValueError("approved_by is required")
    record = ApprovalRecord(
        gate=gate,
        approved_by=approved_by.strip(),
        approved_at=datetime.now(timezone.utc).isoformat(),
        note=note.strip(),
    )
    payload: dict[str, Any] = {
        "version": 1,
        "gate": record.gate,
        "status": "approved",
        "approved_by": record.approved_by,
        "approved_at": record.approved_at,
    }
    if record.note:
        payload["note"] = record.note
    write_text(approval_path(context, gate), yaml.safe_dump(payload, sort_keys=False))
    return record


def is_approved(context: FeatureContext, gate: str) -> bool:
    path = approval_path(context, gate)
    if not path.exists():
        return False
    return str(load_yaml(path).get("status") or "").lower() == "approved"
