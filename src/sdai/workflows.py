from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
import re
from typing import Any, Iterator

import yaml

from sdai.agent_platform import Capability, ExecutionMode
from sdai.artifacts import write_text
from sdai.config import load_yaml
from sdai.governance import evaluate_approval, record_approval
from sdai.models import FeatureContext, LifecycleMode


class WorkflowConfigError(RuntimeError):
    pass


class StepKind(StrEnum):
    DETERMINISTIC = "deterministic"
    AGENT = "agent"
    APPROVAL = "approval"
    VALIDATE = "validate"
    QUALITY_GATE = "quality-gate"
    PARALLEL = "parallel"


class FailureMode(StrEnum):
    STOP = "stop"
    CONTINUE = "continue"


_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _safe_name(value: str, label: str) -> str:
    value = value.strip()
    if not _SAFE_NAME.fullmatch(value):
        raise WorkflowConfigError(
            f"{label} must use only letters, numbers, dot, underscore, or hyphen"
        )
    return value


def _safe_relative_path(value: str, label: str) -> str:
    candidate = Path(value)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise WorkflowConfigError(f"{label} must stay inside the feature workspace")
    normalized = candidate.as_posix().lstrip("./")
    if not normalized:
        raise WorkflowConfigError(f"{label} cannot be empty")
    return normalized


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 1
    delay_seconds: float = 0.0
    backoff_multiplier: float = 1.0


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
    quality_gate: str | None = None
    description: str | None = None
    condition: str = "always"
    retry: RetryPolicy = RetryPolicy()
    on_failure: FailureMode = FailureMode.STOP
    children: tuple["WorkflowStep", ...] = ()


@dataclass(frozen=True)
class WorkflowDefinition:
    name: str
    validation_mode: LifecycleMode
    steps: tuple[WorkflowStep, ...]

    def iter_steps(self) -> Iterator[tuple[WorkflowStep, str | None]]:
        """Yield every addressable step, including parallel children.

        Parallel groups are currently one level deep and contain only agent children,
        so a child can be addressed manually by its unique id.
        """
        for item in self.steps:
            yield item, None
            for child in item.children:
                yield child, item.id

    def step(self, step_id: str) -> WorkflowStep:
        step_id = _safe_name(step_id, "step id")
        for item, _ in self.iter_steps():
            if item.id == step_id:
                return item
        raise WorkflowConfigError(f"Unknown step '{step_id}' in workflow '{self.name}'")

    def parent_id(self, step_id: str) -> str | None:
        step_id = _safe_name(step_id, "step id")
        for item, parent in self.iter_steps():
            if item.id == step_id:
                return parent
        raise WorkflowConfigError(f"Unknown step '{step_id}' in workflow '{self.name}'")

    def top_level_id(self, step_id: str) -> str:
        return self.parent_id(step_id) or _safe_name(step_id, "step id")


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
    role: str = ""
    note: str = ""
    satisfied: bool = False
    detail: str = ""


def _parse_retry(raw: object, step_id: str) -> RetryPolicy:
    if raw is None:
        return RetryPolicy()
    if isinstance(raw, int):
        attempts = raw
        delay = 0.0
        multiplier = 1.0
    elif isinstance(raw, dict):
        attempts = int(raw.get("max_attempts", 1))
        delay = float(raw.get("delay_seconds", 0.0))
        multiplier = float(raw.get("backoff_multiplier", 1.0))
    else:
        raise WorkflowConfigError(f"Step '{step_id}' retry must be an integer or mapping")
    if attempts < 1 or attempts > 20:
        raise WorkflowConfigError(f"Step '{step_id}' retry max_attempts must be between 1 and 20")
    if delay < 0 or delay > 3600:
        raise WorkflowConfigError(f"Step '{step_id}' retry delay_seconds must be between 0 and 3600")
    if multiplier < 1 or multiplier > 10:
        raise WorkflowConfigError(f"Step '{step_id}' retry backoff_multiplier must be between 1 and 10")
    return RetryPolicy(attempts, delay, multiplier)


def _common(raw: dict[str, Any], step_id: str) -> dict[str, Any]:
    condition = str(raw.get("if") or raw.get("condition") or "always").strip() or "always"
    try:
        on_failure = FailureMode(str(raw.get("on_failure") or FailureMode.STOP.value))
    except ValueError as exc:
        raise WorkflowConfigError(f"Step '{step_id}' on_failure must be stop or continue") from exc
    return {
        "description": str(raw["description"]) if raw.get("description") else None,
        "condition": condition,
        "retry": _parse_retry(raw.get("retry"), step_id),
        "on_failure": on_failure,
    }


def _parse_step(raw: object, index: int, *, parent: str | None = None) -> WorkflowStep:
    # Backward compatibility with v0.1/v0.2 workflows that used a list of strings.
    if isinstance(raw, str):
        step_id = _safe_name(raw, f"workflow step #{index + 1}")
        if step_id == "validate":
            return WorkflowStep(id="validate", kind=StepKind.VALIDATE)
        return WorkflowStep(id=step_id, kind=StepKind.DETERMINISTIC, action=step_id)

    if not isinstance(raw, dict):
        raise WorkflowConfigError(f"Workflow step #{index + 1} must be a string or mapping")

    raw_step_id = str(raw.get("id") or "").strip()
    kind_value = str(raw.get("type") or raw.get("kind") or "").strip()
    if not raw_step_id:
        raise WorkflowConfigError(f"Workflow step #{index + 1} is missing id")
    step_id = _safe_name(raw_step_id, f"workflow step #{index + 1} id")
    try:
        kind = StepKind(kind_value)
    except ValueError as exc:
        raise WorkflowConfigError(f"Step '{step_id}' has unsupported type '{kind_value}'") from exc
    common = _common(raw, step_id)

    if kind == StepKind.DETERMINISTIC:
        action = _safe_name(str(raw.get("action") or step_id), f"action for step '{step_id}'")
        return WorkflowStep(id=step_id, kind=kind, action=action, **common)

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
        save_as = _safe_relative_path(
            str(raw["save_as"]) if raw.get("save_as") else f"ai/{step_id}.md",
            f"save_as for step '{step_id}'",
        )
        return WorkflowStep(
            id=step_id,
            kind=kind,
            capability=capability,
            profile=str(raw["profile"]) if raw.get("profile") else None,
            mode=mode,
            save_as=save_as,
            **common,
        )

    if kind == StepKind.APPROVAL:
        gate = _safe_name(str(raw.get("gate") or step_id), f"approval gate for step '{step_id}'")
        return WorkflowStep(id=step_id, kind=kind, gate=gate, **common)

    if kind == StepKind.QUALITY_GATE:
        quality_gate = _safe_name(
            str(raw.get("gate") or raw.get("quality_gate") or step_id),
            f"quality gate for step '{step_id}'",
        )
        return WorkflowStep(id=step_id, kind=kind, quality_gate=quality_gate, **common)

    if kind == StepKind.PARALLEL:
        raw_children = raw.get("steps") or []
        if not isinstance(raw_children, list) or not raw_children:
            raise WorkflowConfigError(f"Parallel step '{step_id}' must define a non-empty steps list")
        children = tuple(
            _parse_step(item, child_index, parent=step_id)
            for child_index, item in enumerate(raw_children)
        )
        child_ids = [child.id for child in children]
        if len(child_ids) != len(set(child_ids)):
            raise WorkflowConfigError(f"Parallel step '{step_id}' contains duplicate child ids")
        for child in children:
            if child.kind != StepKind.AGENT:
                raise WorkflowConfigError(
                    f"Parallel step '{step_id}' currently supports only agent children"
                )
            if child.mode != ExecutionMode.ADVISORY:
                raise WorkflowConfigError(
                    f"Parallel child '{child.id}' must use advisory mode to prevent concurrent workspace writes"
                )
        return WorkflowStep(id=step_id, kind=kind, children=children, **common)

    return WorkflowStep(id=step_id, kind=StepKind.VALIDATE, **common)


def load_workflow(project_root: Path, name: str) -> WorkflowDefinition:
    name = _safe_name(name, "workflow name")
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
    definition_name = _safe_name(str(data.get("name") or name), "workflow definition name")
    definition = WorkflowDefinition(
        name=definition_name,
        validation_mode=validation_mode,
        steps=steps,
    )
    all_ids = [step.id for step, _ in definition.iter_steps()]
    if len(all_ids) != len(set(all_ids)):
        raise WorkflowConfigError(
            f"Workflow '{name}' contains duplicate step ids across top-level and parallel child steps"
        )
    return definition


def _state_path(context: FeatureContext, workflow: str) -> Path:
    workflow = _safe_name(workflow, "workflow name")
    return context.artifact(f".sdai/workflows/{workflow}.yaml")


def load_workflow_state(context: FeatureContext, workflow: str) -> WorkflowState:
    workflow = _safe_name(workflow, "workflow name")
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
    gate = _safe_name(gate, "approval gate")
    return context.artifact(f"approvals/{gate}.yaml")


def grant_approval(
    context: FeatureContext,
    gate: str,
    *,
    approved_by: str,
    role: str = "",
    note: str = "",
) -> ApprovalRecord:
    gate = _safe_name(gate, "approval gate")
    now = datetime.now(timezone.utc).isoformat()
    decision = record_approval(
        context,
        gate,
        approved_by=approved_by,
        role=role,
        note=note,
    )
    return ApprovalRecord(
        gate=gate,
        approved_by=approved_by.strip(),
        approved_at=now,
        role=role.strip(),
        note=note.strip(),
        satisfied=decision.satisfied,
        detail=decision.detail,
    )


def is_approved(context: FeatureContext, gate: str) -> bool:
    gate = _safe_name(gate, "approval gate")
    return evaluate_approval(context, gate).satisfied
