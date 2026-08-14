from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
import json
import math
from pathlib import PurePosixPath
import re
from typing import Mapping

from sdai.integration_execution import (
    IntegrationExecutionError,
    IntegrationExecutionPlan,
    IntegrationExecutionRequest,
    IntegrationExecutionResult,
    execute_integration_plan,
)
from sdai.integration_manifest import IntegrationInputMode, IntegrationOutputMode
from sdai.policy import EffectiveConfiguration
from sdai.workflows import FailureMode, StepKind, WorkflowConfigError, WorkflowStep, _parse_retry, _parse_step


WORKFLOW_OPERATIONAL_STEP_API_VERSION = "sdai.workflow-operational-step/v2"
WORKFLOW_LEAF_PLAN_API_VERSION = "sdai.workflow-leaf-plan/v2"
WORKFLOW_LEAF_RESULT_API_VERSION = "sdai.workflow-leaf-result/v2"
MAX_SAFE_COMMAND_ARGS = 128
MAX_ARG_BYTES = 8192
MAX_TIMEOUT_SECONDS = 3600


class WorkflowOperationalStepError(RuntimeError):
    pass


class OperationalStepKind(StrEnum):
    DETERMINISTIC = "deterministic"
    AGENT = "agent"
    APPROVAL = "approval"
    VALIDATOR = "validator"
    QUALITY_GATE = "quality-gate"
    PLUGIN = "plugin"
    SAFE_COMMAND = "safe-command"


_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_EXECUTABLE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,255}$")
_SAFE_COMMAND_FIELDS = frozenset(
    {
        "id", "type", "kind", "executable", "args_before_input", "args_after_input",
        "input_mode", "input_path", "output_mode", "output_path", "cwd",
        "timeout_seconds", "environment", "workspace_write", "description", "if",
        "condition", "retry", "on_failure",
    }
)


def _fail(code: str, message: str) -> WorkflowOperationalStepError:
    return WorkflowOperationalStepError(f"{code}: {message}")


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise _fail("SDAI-WF2-LEAF-001", "operational step data is not canonical finite JSON") from exc


def _hash_bytes(data: bytes) -> str:
    return "sha256:" + sha256(data).hexdigest()


def _hash_json(value: object) -> str:
    return _hash_bytes(_canonical_json(value).encode("utf-8"))


def _finite_json(value: object, *, label: str) -> object:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise _fail("SDAI-WF2-LEAF-001", f"{label} contains a non-finite number")
        return value
    if isinstance(value, (list, tuple)):
        return [_finite_json(item, label=label) for item in value]
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise _fail("SDAI-WF2-LEAF-001", f"{label} keys must be strings")
        return {key: _finite_json(value[key], label=label) for key in sorted(value)}
    raise _fail("SDAI-WF2-LEAF-001", f"{label} must be finite JSON data")


def _safe_id(value: object, *, label: str) -> str:
    if not isinstance(value, str) or value != value.strip() or not _SAFE_ID.fullmatch(value):
        raise _fail("SDAI-WF2-LEAF-002", f"{label} is invalid")
    return value


def _portable_relative(value: object, *, label: str, allow_dot: bool = False) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or "\\" in value or "\x00" in value:
        raise _fail("SDAI-WF2-LEAF-002", f"{label} must be a portable project-relative path")
    if value == "." and allow_dot:
        return value
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in value.split("/")):
        raise _fail("SDAI-WF2-LEAF-002", f"{label} must be a portable project-relative path")
    return path.as_posix()


def _safe_executable(value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or "\\" in value or "\x00" in value:
        raise _fail("SDAI-WF2-LEAF-002", "safe-command executable is invalid")
    if "/" in value:
        return _portable_relative(value, label="safe-command executable")
    if not _EXECUTABLE.fullmatch(value):
        raise _fail("SDAI-WF2-LEAF-002", "safe-command executable must be a bare name or project-relative path")
    return value


def _string_args(value: object, *, step_id: str, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise _fail("SDAI-WF2-LEAF-002", f"safe-command step '{step_id}' {field} must be a string list")
    if len(value) > MAX_SAFE_COMMAND_ARGS:
        raise _fail("SDAI-WF2-LEAF-002", f"safe-command step '{step_id}' has too many arguments")
    result: list[str] = []
    for item in value:
        if "\x00" in item or len(item.encode("utf-8", errors="strict")) > MAX_ARG_BYTES:
            raise _fail("SDAI-WF2-LEAF-002", f"safe-command step '{step_id}' contains an invalid argument")
        result.append(item)
    return tuple(result)


@dataclass(frozen=True)
class SafeCommandSpec:
    executable: str
    args_before_input: tuple[str, ...]
    input_mode: IntegrationInputMode
    input_path: str | None
    args_after_input: tuple[str, ...]
    output_mode: IntegrationOutputMode
    output_path: str | None
    cwd: str
    timeout_seconds: int
    environment_names: tuple[str, ...]
    requires_workspace_write: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "argsAfterInput": list(self.args_after_input),
            "argsBeforeInput": list(self.args_before_input),
            "cwd": self.cwd,
            "environmentNames": list(self.environment_names),
            "executable": self.executable,
            "inputMode": self.input_mode.value,
            "inputPath": self.input_path,
            "outputMode": self.output_mode.value,
            "outputPath": self.output_path,
            "requiresWorkspaceWrite": self.requires_workspace_write,
            "timeoutSeconds": self.timeout_seconds,
        }


@dataclass(frozen=True)
class WorkflowOperationalStep:
    id: str
    kind: OperationalStepKind
    config_json: str
    description: str | None
    condition: str
    retry_max_attempts: int
    retry_delay_seconds: float
    retry_backoff_multiplier: float
    on_failure: FailureMode
    safe_command: SafeCommandSpec | None = None

    @property
    def config(self) -> dict[str, object]:
        value = json.loads(self.config_json)
        assert isinstance(value, dict)
        return value

    def as_dict(self) -> dict[str, object]:
        return {
            "apiVersion": WORKFLOW_OPERATIONAL_STEP_API_VERSION,
            "condition": self.condition,
            "config": self.config,
            "description": self.description,
            "id": self.id,
            "kind": self.kind.value,
            "onFailure": self.on_failure.value,
            "retry": {
                "backoffMultiplier": self.retry_backoff_multiplier,
                "delaySeconds": self.retry_delay_seconds,
                "maxAttempts": self.retry_max_attempts,
            },
        }

    def to_json(self) -> str:
        return _canonical_json(self.as_dict())

    @property
    def sha256(self) -> str:
        return _hash_bytes(self.to_json().encode("utf-8"))


def _parse_safe_command(raw: Mapping[str, object], index: int) -> WorkflowOperationalStep:
    unknown = sorted(set(raw) - _SAFE_COMMAND_FIELDS)
    if unknown:
        raise _fail("SDAI-WF2-LEAF-002", "safe-command has unsupported field(s): " + ", ".join(map(str, unknown)))
    step_id = _safe_id(raw.get("id"), label=f"workflow step #{index + 1} id")
    executable = _safe_executable(raw.get("executable"))
    args_before = _string_args(raw.get("args_before_input"), step_id=step_id, field="args_before_input")
    args_after = _string_args(raw.get("args_after_input"), step_id=step_id, field="args_after_input")
    try:
        input_mode = IntegrationInputMode(str(raw.get("input_mode", IntegrationInputMode.NONE.value)))
        output_mode = IntegrationOutputMode(str(raw.get("output_mode", IntegrationOutputMode.STDOUT.value)))
    except ValueError as exc:
        raise _fail("SDAI-WF2-LEAF-002", f"safe-command step '{step_id}' has unsupported input/output mode") from exc
    input_path = raw.get("input_path")
    output_path = raw.get("output_path")
    if input_path is not None:
        input_path = _portable_relative(input_path, label=f"safe-command step '{step_id}' input_path")
    if output_path is not None:
        output_path = _portable_relative(output_path, label=f"safe-command step '{step_id}' output_path")
    if input_mode == IntegrationInputMode.FILE and input_path is None:
        raise _fail("SDAI-WF2-LEAF-002", f"safe-command step '{step_id}' file input requires input_path")
    if input_mode != IntegrationInputMode.FILE and input_path is not None:
        raise _fail("SDAI-WF2-LEAF-002", f"safe-command step '{step_id}' input_path requires file input mode")
    if output_mode == IntegrationOutputMode.FILE and output_path is None:
        raise _fail("SDAI-WF2-LEAF-002", f"safe-command step '{step_id}' file output requires output_path")
    if output_mode != IntegrationOutputMode.FILE and output_path is not None:
        raise _fail("SDAI-WF2-LEAF-002", f"safe-command step '{step_id}' output_path requires file output mode")
    cwd = _portable_relative(raw.get("cwd", "."), label=f"safe-command step '{step_id}' cwd", allow_dot=True)
    if cwd != ".":
        raise _fail("SDAI-WF2-LEAF-002", "safe-command v2 currently requires cwd '.' so the reviewed execution boundary owns project containment")
    timeout = raw.get("timeout_seconds", 600)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout < 1 or timeout > MAX_TIMEOUT_SECONDS:
        raise _fail("SDAI-WF2-LEAF-002", f"safe-command step '{step_id}' timeout_seconds is out of range")
    environment = raw.get("environment") or []
    if not isinstance(environment, list) or not all(isinstance(item, str) for item in environment):
        raise _fail("SDAI-WF2-LEAF-002", f"safe-command step '{step_id}' environment must be a string list")
    if any(item != item.strip() or not _ENV_NAME.fullmatch(item) for item in environment) or len(environment) != len(set(environment)):
        raise _fail("SDAI-WF2-LEAF-002", f"safe-command step '{step_id}' environment names are invalid or duplicated")
    workspace_write = raw.get("workspace_write", False)
    if not isinstance(workspace_write, bool):
        raise _fail("SDAI-WF2-LEAF-002", f"safe-command step '{step_id}' workspace_write must be true or false")
    if (input_mode == IntegrationInputMode.FILE or output_mode == IntegrationOutputMode.FILE) and not workspace_write:
        raise _fail("SDAI-WF2-LEAF-002", f"safe-command step '{step_id}' file I/O requires workspace_write: true")
    retry = _parse_retry(raw.get("retry"), step_id)
    try:
        on_failure = FailureMode(str(raw.get("on_failure") or FailureMode.STOP.value))
    except ValueError as exc:
        raise _fail("SDAI-WF2-LEAF-002", f"safe-command step '{step_id}' on_failure must be stop or continue") from exc
    spec = SafeCommandSpec(
        executable=executable,
        args_before_input=args_before,
        input_mode=input_mode,
        input_path=input_path,
        args_after_input=args_after,
        output_mode=output_mode,
        output_path=output_path,
        cwd=cwd,
        timeout_seconds=timeout,
        environment_names=tuple(sorted(environment)),
        requires_workspace_write=workspace_write,
    )
    return WorkflowOperationalStep(
        id=step_id,
        kind=OperationalStepKind.SAFE_COMMAND,
        config_json=_canonical_json(spec.as_dict()),
        description=str(raw["description"]) if raw.get("description") else None,
        condition=str(raw.get("if") or raw.get("condition") or "always").strip() or "always",
        retry_max_attempts=retry.max_attempts,
        retry_delay_seconds=retry.delay_seconds,
        retry_backoff_multiplier=retry.backoff_multiplier,
        on_failure=on_failure,
        safe_command=spec,
    )


def _legacy_config(step: WorkflowStep) -> tuple[OperationalStepKind, dict[str, object]]:
    if step.kind == StepKind.DETERMINISTIC:
        return OperationalStepKind.DETERMINISTIC, {"action": step.action}
    if step.kind == StepKind.AGENT:
        return OperationalStepKind.AGENT, {
            "agent": step.agent_name,
            "capability": step.capability.value if step.capability else None,
            "mode": step.mode.value,
            "profile": step.profile,
            "saveAs": step.save_as,
        }
    if step.kind == StepKind.APPROVAL:
        return OperationalStepKind.APPROVAL, {"gate": step.gate}
    if step.kind == StepKind.VALIDATE:
        return OperationalStepKind.VALIDATOR, {"validator": "workflow-validation"}
    if step.kind == StepKind.QUALITY_GATE:
        return OperationalStepKind.QUALITY_GATE, {"gate": step.quality_gate}
    if step.kind == StepKind.PLUGIN:
        inputs = _finite_json(step.plugin_input_values, label=f"plugin step '{step.id}' inputs")
        return OperationalStepKind.PLUGIN, {
            "inputKeys": sorted(step.plugin_input_values),
            "inputsSha256": _hash_json(inputs),
            "plugin": step.plugin_id,
        }
    raise _fail("SDAI-WF2-LEAF-002", f"step '{step.id}' is control flow, not an operational leaf")


def normalize_workflow_operational_step(raw: object, *, index: int = 0) -> WorkflowOperationalStep:
    candidate = raw
    if isinstance(raw, Mapping):
        kind_value = str(raw.get("type") or raw.get("kind") or "").strip()
        if kind_value == OperationalStepKind.SAFE_COMMAND.value:
            return _parse_safe_command(raw, index)
        if kind_value == OperationalStepKind.VALIDATOR.value:
            candidate = dict(raw)
            candidate["type"] = StepKind.VALIDATE.value
            candidate.pop("kind", None)
    try:
        step = _parse_step(candidate, index)
    except WorkflowConfigError as exc:
        raise _fail("SDAI-WF2-LEAF-002", str(exc)) from exc
    kind, config = _legacy_config(step)
    return WorkflowOperationalStep(
        id=step.id,
        kind=kind,
        config_json=_canonical_json(config),
        description=step.description,
        condition=step.condition,
        retry_max_attempts=step.retry.max_attempts,
        retry_delay_seconds=step.retry.delay_seconds,
        retry_backoff_multiplier=step.retry.backoff_multiplier,
        on_failure=step.on_failure,
    )


@dataclass(frozen=True)
class WorkflowLeafPlan:
    step: WorkflowOperationalStep
    input_sha256: str
    input_bytes: int
    policy_sources: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "apiVersion": WORKFLOW_LEAF_PLAN_API_VERSION,
            "inputBytes": self.input_bytes,
            "inputSha256": self.input_sha256,
            "policySources": list(self.policy_sources),
            "step": self.step.as_dict(),
            "stepSha256": self.step.sha256,
        }

    def to_json(self) -> str:
        return _canonical_json(self.as_dict())

    @property
    def sha256(self) -> str:
        return _hash_bytes(self.to_json().encode("utf-8"))


def build_workflow_leaf_plan(step: WorkflowOperationalStep, *, input_text: str = "", policy: EffectiveConfiguration) -> WorkflowLeafPlan:
    payload = input_text.encode("utf-8", errors="strict")
    if step.kind == OperationalStepKind.SAFE_COMMAND:
        assert step.safe_command is not None
        spec = step.safe_command
        if spec.input_mode == IntegrationInputMode.NONE and payload:
            raise _fail("SDAI-WF2-LEAF-003", f"safe-command step '{step.id}' does not accept runtime input")
        if spec.requires_workspace_write and not policy.workspace_write:
            raise _fail("SDAI-WF2-LEAF-003", "safe-command requires workspace-write but effective policy disables it")
        if policy.environment_allowlist is not None:
            blocked = sorted(set(spec.environment_names) - set(policy.environment_allowlist))
            if blocked:
                raise _fail("SDAI-WF2-LEAF-003", "safe-command environment denied by effective policy: " + ", ".join(blocked))
    return WorkflowLeafPlan(step, _hash_bytes(payload), len(payload), tuple(policy.sources))


def _integration_request_and_plan(plan: WorkflowLeafPlan, input_text: str) -> tuple[IntegrationExecutionRequest, IntegrationExecutionPlan]:
    step = plan.step
    if step.kind != OperationalStepKind.SAFE_COMMAND or step.safe_command is None:
        raise _fail("SDAI-WF2-LEAF-004", "only safe-command plans can be executed through the reviewed Integration boundary")
    payload = input_text.encode("utf-8", errors="strict")
    if _hash_bytes(payload) != plan.input_sha256 or len(payload) != plan.input_bytes:
        raise _fail("SDAI-WF2-LEAF-004", "runtime input does not match the planned input hash")
    spec = step.safe_command
    identity = f"workflow-safe-command:{step.id}"
    request = IntegrationExecutionRequest(
        integration_identity=identity,
        manifest_sha256=step.sha256,
        input_sha256=plan.input_sha256,
        input_bytes=plan.input_bytes,
        _input_text=input_text,
    )
    execution = IntegrationExecutionPlan(
        integration_identity=identity,
        integration_version="2",
        manifest_sha256=step.sha256,
        executable=spec.executable,
        args_before_input=spec.args_before_input,
        input_mode=spec.input_mode,
        input_path=spec.input_path,
        input_sha256=plan.input_sha256,
        input_bytes=plan.input_bytes,
        args_after_input=spec.args_after_input,
        output_mode=spec.output_mode,
        output_path=spec.output_path,
        timeout_seconds=spec.timeout_seconds,
        environment_names=spec.environment_names,
        requires_network=False,
        requires_workspace_write=spec.requires_workspace_write,
    )
    return request, execution


@dataclass(frozen=True)
class WorkflowLeafResult:
    step_id: str
    kind: OperationalStepKind
    plan_sha256: str
    status: str
    exit_code: int | None
    output: object | None
    error: dict[str, object] | None

    def as_dict(self) -> dict[str, object]:
        return {
            "apiVersion": WORKFLOW_LEAF_RESULT_API_VERSION,
            "error": self.error,
            "exitCode": self.exit_code,
            "kind": self.kind.value,
            "output": self.output,
            "planSha256": self.plan_sha256,
            "status": self.status,
            "stepId": self.step_id,
        }

    def to_json(self) -> str:
        return _canonical_json(self.as_dict())


def execute_safe_command_leaf(
    plan: WorkflowLeafPlan,
    *,
    input_text: str = "",
    project_root,
    policy: EffectiveConfiguration,
    environment: Mapping[str, str] | None = None,
) -> WorkflowLeafResult:
    request, execution = _integration_request_and_plan(plan, input_text)
    try:
        result: IntegrationExecutionResult = execute_integration_plan(
            execution,
            request,
            project_root=project_root,
            policy=policy,
            environment=environment,
        )
    except IntegrationExecutionError as exc:
        return WorkflowLeafResult(
            plan.step.id,
            plan.step.kind,
            plan.sha256,
            "policy-violation",
            None,
            None,
            {"category": "policy", "code": "SDAI-WF2-LEAF-003", "message": str(exc).split(": ", 1)[-1]},
        )
    error = None if result.error is None else {
        "category": result.error.category,
        "code": result.error.code,
        "message": result.error.message,
    }
    return WorkflowLeafResult(
        plan.step.id,
        plan.step.kind,
        plan.sha256,
        result.status.value,
        result.exit_code,
        result.output,
        error,
    )
