from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
import json
import math
from pathlib import Path
import re
from typing import Mapping

from sdai.models import LifecycleMode
from sdai.workflow_components import WorkflowComponentError, compose_workflow
from sdai.workflow_overlays import WorkflowOverlayError, resolve_workflow_data
from sdai.workflows import (
    FailureMode,
    StepKind,
    WorkflowConfigError,
    WorkflowStep,
    _parse_retry,
    _parse_step,
)


WORKFLOW_GRAPH_API_VERSION = "sdai.workflow-graph/v2"
WORKFLOW_RESOLUTION_API_VERSION = "sdai.workflow-resolution/v2"
WORKFLOW_ENGINE2_MIN_VERSION = 9
MAX_CONTROL_CHILDREN = 100
MAX_FAN_ITEMS = 100
MAX_LOOP_ITERATIONS = 100
MAX_CONCURRENCY = 32
MAX_EXPRESSION_DEPTH = 32
MAX_LOGICAL_TERMS = 32


class WorkflowGraphError(RuntimeError):
    """Raised when a Workflow Engine 2 graph cannot be resolved safely."""


class WorkflowNodeKind(StrEnum):
    SEQUENCE = "sequence"
    IF = "if"
    SWITCH = "switch"
    PARALLEL = "parallel"
    FAN_OUT = "fan-out"
    FAN_IN = "fan-in"
    FOREACH = "foreach"
    BOUNDED_WHILE = "bounded-while"
    DETERMINISTIC = "deterministic"
    AGENT = "agent"
    APPROVAL = "approval"
    VALIDATE = "validate"
    QUALITY_GATE = "quality-gate"
    PLUGIN = "plugin"
    SAFE_COMMAND = "safe-command"


_CONTROL_KINDS = frozenset(
    {
        WorkflowNodeKind.SEQUENCE.value,
        WorkflowNodeKind.IF.value,
        WorkflowNodeKind.SWITCH.value,
        WorkflowNodeKind.PARALLEL.value,
        WorkflowNodeKind.FAN_OUT.value,
        WorkflowNodeKind.FAN_IN.value,
        WorkflowNodeKind.FOREACH.value,
        WorkflowNodeKind.BOUNDED_WHILE.value,
    }
)
_NEW_V9_KINDS = _CONTROL_KINDS - {WorkflowNodeKind.PARALLEL.value}
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_VARIABLE_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_REFERENCE = re.compile(
    r"^(?:inputs|steps|item|loop)(?:\.[A-Za-z0-9_-]+)+$"
)
_EXPRESSION_OPERATORS = frozenset(
    {
        "ref",
        "literal",
        "eq",
        "ne",
        "lt",
        "lte",
        "gt",
        "gte",
        "in",
        "not-in",
        "and",
        "or",
        "not",
        "exists",
    }
)
_BINARY_OPERATORS = frozenset({"eq", "ne", "lt", "lte", "gt", "gte", "in", "not-in"})
_CONTROL_COMMON_KEYS = frozenset({"id", "type", "kind", "description", "retry", "on_failure"})
_CONTROL_KEYS = {
    "sequence": _CONTROL_COMMON_KEYS | {"steps"},
    "if": _CONTROL_COMMON_KEYS | {"condition", "then", "else"},
    "switch": _CONTROL_COMMON_KEYS | {"value", "cases", "default"},
    "parallel": _CONTROL_COMMON_KEYS | {"steps", "max_concurrency", "if", "condition"},
    "fan-out": _CONTROL_COMMON_KEYS | {"items", "as", "max_items", "max_concurrency", "steps"},
    "fan-in": _CONTROL_COMMON_KEYS | {"sources", "strategy"},
    "foreach": _CONTROL_COMMON_KEYS | {"items", "as", "max_items", "steps"},
    "bounded-while": _CONTROL_COMMON_KEYS | {"condition", "max_iterations", "steps"},
}
_FAN_IN_STRATEGIES = frozenset({"collect", "all-success", "any-success"})


def _fail(code: str, message: str) -> WorkflowGraphError:
    return WorkflowGraphError(f"{code}: {message}")


def _normalize_json(value: object, *, label: str = "value") -> object:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise _fail("SDAI-WF2-001", f"{label} must contain only finite JSON numbers")
        return value
    if isinstance(value, tuple):
        return [_normalize_json(item, label=f"{label}[]") for item in value]
    if isinstance(value, list):
        return [_normalize_json(item, label=f"{label}[]") for item in value]
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise _fail("SDAI-WF2-001", f"{label} JSON object keys must be strings")
        return {
            key: _normalize_json(value[key], label=f"{label}.{key}")
            for key in sorted(value)
        }
    raise _fail(
        "SDAI-WF2-001",
        f"{label} must be canonical JSON data, got {type(value).__name__}",
    )


def _canonical_json(value: object) -> str:
    normalized = _normalize_json(value)
    try:
        return json.dumps(
            normalized,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise _fail("SDAI-WF2-001", "workflow graph data is not canonical finite JSON") from exc


def _hash_json(value: object) -> str:
    return "sha256:" + sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _safe_name(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not _SAFE_NAME.fullmatch(value.strip()):
        raise _fail(
            "SDAI-WF2-002",
            f"{label} must use only letters, numbers, dot, underscore, or hyphen",
        )
    return value.strip()


def _positive_bound(
    value: object,
    *,
    label: str,
    maximum: int,
    required: bool = True,
    default: int | None = None,
) -> int:
    if value is None and not required and default is not None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise _fail("SDAI-WF2-003", f"{label} must be an integer")
    if value < 1 or value > maximum:
        raise _fail(
            "SDAI-WF2-003",
            f"{label} must be between 1 and {maximum}",
        )
    return value


def _normalize_literal(value: object, *, depth: int, label: str) -> object:
    if depth > MAX_EXPRESSION_DEPTH:
        raise _fail(
            "SDAI-WF2-004",
            f"{label} exceeds maximum expression depth {MAX_EXPRESSION_DEPTH}",
        )
    return _normalize_json(value, label=label)


def normalize_workflow_expression(
    value: object,
    *,
    depth: int = 0,
    label: str = "workflow expression",
) -> object:
    """Validate and normalize a non-code Workflow Engine 2 expression.

    Expressions are finite JSON trees. A mapping is an operator only when it has
    exactly one supported key. Arbitrary Python, Jinja, shell, or attribute access
    is never evaluated by this contract.
    """

    if depth > MAX_EXPRESSION_DEPTH:
        raise _fail(
            "SDAI-WF2-004",
            f"{label} exceeds maximum expression depth {MAX_EXPRESSION_DEPTH}",
        )
    if value is None or isinstance(value, (str, bool, int, float, list, tuple)):
        return _normalize_literal(value, depth=depth, label=label)
    if not isinstance(value, Mapping):
        raise _fail("SDAI-WF2-004", f"{label} must be finite JSON data")
    if len(value) != 1:
        raise _fail(
            "SDAI-WF2-004",
            f"{label} operator mapping must contain exactly one key",
        )
    operator = next(iter(value))
    if not isinstance(operator, str) or operator not in _EXPRESSION_OPERATORS:
        raise _fail(
            "SDAI-WF2-004",
            f"{label} uses unsupported operator '{operator}'",
        )
    operand = value[operator]

    if operator == "ref":
        if not isinstance(operand, str) or not _REFERENCE.fullmatch(operand):
            raise _fail(
                "SDAI-WF2-004",
                f"{label}.ref must use inputs/steps/item/loop dotted references",
            )
        return {"ref": operand}

    if operator == "literal":
        return {
            "literal": _normalize_literal(
                operand,
                depth=depth + 1,
                label=f"{label}.literal",
            )
        }

    if operator in _BINARY_OPERATORS:
        if not isinstance(operand, (list, tuple)) or len(operand) != 2:
            raise _fail(
                "SDAI-WF2-004",
                f"{label}.{operator} must contain exactly two expressions",
            )
        return {
            operator: [
                normalize_workflow_expression(
                    item,
                    depth=depth + 1,
                    label=f"{label}.{operator}[{index}]",
                )
                for index, item in enumerate(operand)
            ]
        }

    if operator in {"and", "or"}:
        if not isinstance(operand, (list, tuple)) or not operand:
            raise _fail(
                "SDAI-WF2-004",
                f"{label}.{operator} must contain at least one expression",
            )
        if len(operand) > MAX_LOGICAL_TERMS:
            raise _fail(
                "SDAI-WF2-004",
                f"{label}.{operator} supports at most {MAX_LOGICAL_TERMS} terms",
            )
        return {
            operator: [
                normalize_workflow_expression(
                    item,
                    depth=depth + 1,
                    label=f"{label}.{operator}[{index}]",
                )
                for index, item in enumerate(operand)
            ]
        }

    if operator == "not":
        return {
            "not": normalize_workflow_expression(
                operand,
                depth=depth + 1,
                label=f"{label}.not",
            )
        }

    if operator == "exists":
        if not isinstance(operand, str) or not _REFERENCE.fullmatch(operand):
            raise _fail(
                "SDAI-WF2-004",
                f"{label}.exists must be a dotted reference",
            )
        return {"exists": operand}

    raise AssertionError(operator)


def _resolve_reference(reference: str, context: Mapping[str, object]) -> object:
    parts = reference.split(".")
    current: object = context
    for part in parts:
        if isinstance(current, Mapping):
            if part not in current:
                raise _fail("SDAI-WF2-004", f"expression reference '{reference}' is unresolved")
            current = current[part]
            continue
        if isinstance(current, (list, tuple)) and part.isdigit():
            index = int(part)
            if index >= len(current):
                raise _fail("SDAI-WF2-004", f"expression reference '{reference}' is unresolved")
            current = current[index]
            continue
        raise _fail("SDAI-WF2-004", f"expression reference '{reference}' is unresolved")
    return current


def evaluate_workflow_expression(
    expression: object,
    context: Mapping[str, object],
) -> object:
    normalized = normalize_workflow_expression(expression)
    if not isinstance(normalized, Mapping):
        return normalized
    operator = next(iter(normalized))
    operand = normalized[operator]
    if operator == "ref":
        return _resolve_reference(str(operand), context)
    if operator == "literal":
        return operand
    if operator == "exists":
        try:
            _resolve_reference(str(operand), context)
            return True
        except WorkflowGraphError:
            return False
    if operator == "not":
        return not bool(evaluate_workflow_expression(operand, context))
    if operator in {"and", "or"}:
        assert isinstance(operand, list)
        values = [bool(evaluate_workflow_expression(item, context)) for item in operand]
        return all(values) if operator == "and" else any(values)

    assert isinstance(operand, list) and len(operand) == 2
    left = evaluate_workflow_expression(operand[0], context)
    right = evaluate_workflow_expression(operand[1], context)
    try:
        if operator == "eq":
            return left == right
        if operator == "ne":
            return left != right
        if operator == "lt":
            return left < right  # type: ignore[operator]
        if operator == "lte":
            return left <= right  # type: ignore[operator]
        if operator == "gt":
            return left > right  # type: ignore[operator]
        if operator == "gte":
            return left >= right  # type: ignore[operator]
        if operator == "in":
            return left in right  # type: ignore[operator]
        if operator == "not-in":
            return left not in right  # type: ignore[operator]
    except (TypeError, ValueError) as exc:
        raise _fail(
            "SDAI-WF2-004",
            f"expression operator '{operator}' received incompatible values",
        ) from exc
    raise AssertionError(operator)


@dataclass(frozen=True)
class WorkflowGraphBranch:
    id: str
    label: str
    children: tuple[str, ...]
    when_json: str | None = None

    @property
    def when(self) -> object | None:
        return None if self.when_json is None else json.loads(self.when_json)

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "id": self.id,
            "label": self.label,
            "children": list(self.children),
        }
        if self.when_json is not None:
            payload["when"] = self.when
        return payload


@dataclass(frozen=True)
class WorkflowGraphNode:
    path: str
    id: str
    kind: str
    parent: str | None
    index: int
    children: tuple[str, ...] = ()
    branches: tuple[WorkflowGraphBranch, ...] = ()
    config_json: str = "{}"

    @property
    def config(self) -> dict[str, object]:
        value = json.loads(self.config_json)
        assert isinstance(value, dict)
        return value

    def as_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "id": self.id,
            "kind": self.kind,
            "parent": self.parent,
            "index": self.index,
            "children": list(self.children),
            "branches": [item.as_dict() for item in self.branches],
            "config": self.config,
        }


@dataclass(frozen=True)
class WorkflowGraphEdge:
    source: str
    target: str
    kind: str
    label: str | None = None

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "source": self.source,
            "target": self.target,
            "kind": self.kind,
        }
        if self.label is not None:
            payload["label"] = self.label
        return payload


@dataclass(frozen=True)
class WorkflowGraph:
    root: str
    nodes: tuple[WorkflowGraphNode, ...]
    edges: tuple[WorkflowGraphEdge, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "apiVersion": WORKFLOW_GRAPH_API_VERSION,
            "root": self.root,
            "nodes": [item.as_dict() for item in self.nodes],
            "edges": [item.as_dict() for item in self.edges],
        }

    def to_json(self) -> str:
        return _canonical_json(self.as_dict())

    @property
    def sha256(self) -> str:
        return "sha256:" + sha256(self.to_json().encode("utf-8")).hexdigest()

    def node(self, path: str) -> WorkflowGraphNode:
        for item in self.nodes:
            if item.path == path:
                return item
        raise _fail("SDAI-WF2-005", f"workflow graph has no node '{path}'")


@dataclass(frozen=True)
class WorkflowGraphResolution:
    name: str
    workflow_version: int | None
    validation_mode: str
    input_definitions: tuple[dict[str, object], ...]
    public_inputs_json: str
    components: tuple[dict[str, object], ...]
    inheritance: tuple[str, ...]
    overlays: tuple[dict[str, object], ...]
    lifecycle_hooks: tuple[dict[str, object], ...]
    mandatory_steps: tuple[str, ...]
    graph: WorkflowGraph
    _input_values_json: str

    @property
    def input_values(self) -> dict[str, object]:
        value = json.loads(self._input_values_json)
        assert isinstance(value, dict)
        return value

    @property
    def public_inputs(self) -> dict[str, object]:
        value = json.loads(self.public_inputs_json)
        assert isinstance(value, dict)
        return value

    def as_dict(self) -> dict[str, object]:
        return {
            "apiVersion": WORKFLOW_RESOLUTION_API_VERSION,
            "name": self.name,
            "workflowVersion": self.workflow_version,
            "validationMode": self.validation_mode,
            "inputDefinitions": list(self.input_definitions),
            "resolvedInputs": self.public_inputs,
            "components": list(self.components),
            "inheritance": list(self.inheritance),
            "overlays": list(self.overlays),
            "lifecycleHooks": list(self.lifecycle_hooks),
            "mandatorySteps": list(self.mandatory_steps),
            "graphSha256": self.graph.sha256,
            "graph": self.graph.as_dict(),
        }

    def to_json(self) -> str:
        return _canonical_json(self.as_dict())

    @property
    def sha256(self) -> str:
        return "sha256:" + sha256(self.to_json().encode("utf-8")).hexdigest()


@dataclass
class _GraphBuilder:
    workflow_version: int | None
    nodes: list[WorkflowGraphNode]
    edges: list[WorkflowGraphEdge]
    seen_ids: set[str]
    fan_in_nodes: list[tuple[str, tuple[str, ...]]]

    def _register_id(self, step_id: str) -> None:
        if step_id in self.seen_ids:
            raise _fail("SDAI-WF2-002", f"workflow contains duplicate step id '{step_id}'")
        self.seen_ids.add(step_id)

    def _path(self, parent: str, step_id: str) -> str:
        return step_id if parent == "$root" else f"{parent}/{step_id}"

    def _add_sibling_edges(self, paths: tuple[str, ...]) -> None:
        for source, target in zip(paths, paths[1:]):
            self.edges.append(WorkflowGraphEdge(source, target, "next"))

    def _children(
        self,
        raw_steps: object,
        *,
        parent: str,
        label: str,
    ) -> tuple[str, ...]:
        if not isinstance(raw_steps, list) or not raw_steps:
            raise _fail("SDAI-WF2-003", f"{label} must contain a non-empty steps list")
        if len(raw_steps) > MAX_CONTROL_CHILDREN:
            raise _fail(
                "SDAI-WF2-003",
                f"{label} supports at most {MAX_CONTROL_CHILDREN} direct steps",
            )
        paths = tuple(
            self._step(item, index=index, parent=parent)
            for index, item in enumerate(raw_steps)
        )
        self._add_sibling_edges(paths)
        return paths

    def _control_common(self, raw: Mapping[str, object], step_id: str) -> dict[str, object]:
        retry = _parse_retry(raw.get("retry"), step_id)
        try:
            on_failure = FailureMode(str(raw.get("on_failure") or FailureMode.STOP.value))
        except ValueError as exc:
            raise _fail(
                "SDAI-WF2-003",
                f"control step '{step_id}' on_failure must be stop or continue",
            ) from exc
        return {
            "description": str(raw["description"]) if raw.get("description") else None,
            "retry": {
                "maxAttempts": retry.max_attempts,
                "delaySeconds": retry.delay_seconds,
                "backoffMultiplier": retry.backoff_multiplier,
            },
            "onFailure": on_failure.value,
        }

    def _validate_control_keys(self, raw: Mapping[str, object], kind: str, step_id: str) -> None:
        unknown = sorted(set(raw) - _CONTROL_KEYS[kind])
        if unknown:
            raise _fail(
                "SDAI-WF2-003",
                f"control step '{step_id}' has unsupported field(s): {', '.join(map(str, unknown))}",
            )

    def _leaf_config(self, step: WorkflowStep) -> dict[str, object]:
        common: dict[str, object] = {
            "description": step.description,
            "legacyCondition": step.condition,
            "retry": {
                "maxAttempts": step.retry.max_attempts,
                "delaySeconds": step.retry.delay_seconds,
                "backoffMultiplier": step.retry.backoff_multiplier,
            },
            "onFailure": step.on_failure.value,
        }
        if step.kind == StepKind.DETERMINISTIC:
            common["action"] = step.action
        elif step.kind == StepKind.AGENT:
            common.update(
                {
                    "capability": step.capability.value if step.capability else None,
                    "agent": step.agent_name,
                    "profile": step.profile,
                    "mode": step.mode.value,
                    "saveAs": step.save_as,
                }
            )
        elif step.kind == StepKind.APPROVAL:
            common["gate"] = step.gate
        elif step.kind == StepKind.QUALITY_GATE:
            common["gate"] = step.quality_gate
        elif step.kind == StepKind.PLUGIN:
            inputs = step.plugin_input_values
            common.update(
                {
                    "plugin": step.plugin_id,
                    "inputKeys": sorted(inputs),
                    "inputsSha256": _hash_json(inputs),
                }
            )
        return common

    def _leaf(
        self,
        raw: object,
        *,
        index: int,
        parent: str,
    ) -> str:
        raw_kind = (
            str(raw.get("type") or raw.get("kind") or "").strip()
            if isinstance(raw, Mapping)
            else ""
        )
        if raw_kind == WorkflowNodeKind.SAFE_COMMAND.value:
            from sdai.workflow_operational_steps import (
                WorkflowOperationalStepError,
                normalize_workflow_operational_step,
            )

            try:
                operational = normalize_workflow_operational_step(raw, index=index)
            except WorkflowOperationalStepError as exc:
                raise _fail("SDAI-WF2-002", str(exc)) from exc
            self._register_id(operational.id)
            path = self._path(parent, operational.id)
            self.nodes.append(
                WorkflowGraphNode(
                    path=path,
                    id=operational.id,
                    kind=operational.kind.value,
                    parent=parent,
                    index=index,
                    config_json=_canonical_json(
                        {
                            "operationalStep": operational.as_dict(),
                            "requiresWorkspaceWrite": bool(
                                operational.safe_command
                                and operational.safe_command.requires_workspace_write
                            ),
                        }
                    ),
                )
            )
            self.edges.append(WorkflowGraphEdge(parent, path, "contains"))
            return path
        try:
            step = _parse_step(raw, index)
        except WorkflowConfigError as exc:
            raise _fail("SDAI-WF2-002", str(exc)) from exc
        if step.kind == StepKind.PARALLEL:
            raise _fail("SDAI-WF2-003", f"parallel step '{step.id}' must be parsed as control flow")
        self._register_id(step.id)
        path = self._path(parent, step.id)
        self.nodes.append(
            WorkflowGraphNode(
                path=path,
                id=step.id,
                kind=step.kind.value,
                parent=parent,
                index=index,
                config_json=_canonical_json(self._leaf_config(step)),
            )
        )
        self.edges.append(WorkflowGraphEdge(parent, path, "contains"))
        return path

    def _step(self, raw: object, *, index: int, parent: str) -> str:
        if isinstance(raw, str):
            return self._leaf(raw, index=index, parent=parent)
        if not isinstance(raw, Mapping):
            raise _fail("SDAI-WF2-002", f"workflow step #{index + 1} must be a string or mapping")
        kind = str(raw.get("type") or raw.get("kind") or "").strip()
        if kind not in _CONTROL_KINDS:
            return self._leaf(raw, index=index, parent=parent)
        raw_id = raw.get("id")
        step_id = _safe_name(raw_id, label=f"workflow step #{index + 1} id")
        self._register_id(step_id)
        self._validate_control_keys(raw, kind, step_id)
        path = self._path(parent, step_id)
        self.edges.append(WorkflowGraphEdge(parent, path, "contains"))
        common = self._control_common(raw, step_id)
        branches: list[WorkflowGraphBranch] = []
        children: tuple[str, ...] = ()
        config = dict(common)

        if kind == WorkflowNodeKind.SEQUENCE.value:
            children = self._children(raw.get("steps"), parent=path, label=f"sequence '{step_id}'")

        elif kind == WorkflowNodeKind.PARALLEL.value:
            raw_steps = raw.get("steps")
            if self.workflow_version is None or self.workflow_version < WORKFLOW_ENGINE2_MIN_VERSION:
                try:
                    legacy = _parse_step(dict(raw), index)
                except WorkflowConfigError as exc:
                    raise _fail("SDAI-WF2-002", str(exc)) from exc
                assert legacy.kind == StepKind.PARALLEL
            if not isinstance(raw_steps, list) or not raw_steps:
                raise _fail("SDAI-WF2-003", f"parallel step '{step_id}' must contain steps")
            if len(raw_steps) > MAX_CONCURRENCY:
                raise _fail(
                    "SDAI-WF2-003",
                    f"parallel step '{step_id}' supports at most {MAX_CONCURRENCY} branches",
                )
            max_concurrency = _positive_bound(
                raw.get("max_concurrency"),
                label=f"parallel step '{step_id}' max_concurrency",
                maximum=MAX_CONCURRENCY,
                required=False,
                default=len(raw_steps),
            )
            config["maxConcurrency"] = max_concurrency
            legacy_condition = raw.get("if") if "if" in raw else raw.get("condition")
            if legacy_condition is not None:
                config["legacyCondition"] = str(legacy_condition)
            children = self._children(raw_steps, parent=path, label=f"parallel '{step_id}'")

        elif kind == WorkflowNodeKind.IF.value:
            if "condition" not in raw:
                raise _fail("SDAI-WF2-003", f"if step '{step_id}' is missing condition")
            condition = normalize_workflow_expression(raw["condition"], label=f"if '{step_id}' condition")
            config["condition"] = condition
            then_parent = f"{path}/$then"
            then_paths = self._children(raw.get("then"), parent=then_parent, label=f"if '{step_id}' then")
            self.edges.append(WorkflowGraphEdge(path, then_paths[0], "branch", "then"))
            branches.append(WorkflowGraphBranch("$then", "then", then_paths))
            raw_else = raw.get("else")
            if raw_else is not None:
                else_parent = f"{path}/$else"
                else_paths = self._children(raw_else, parent=else_parent, label=f"if '{step_id}' else")
                self.edges.append(WorkflowGraphEdge(path, else_paths[0], "branch", "else"))
                branches.append(WorkflowGraphBranch("$else", "else", else_paths))
            children = tuple(item for branch in branches for item in branch.children)

        elif kind == WorkflowNodeKind.SWITCH.value:
            if "value" not in raw:
                raise _fail("SDAI-WF2-003", f"switch step '{step_id}' is missing value")
            selector = normalize_workflow_expression(raw["value"], label=f"switch '{step_id}' value")
            config["value"] = selector
            raw_cases = raw.get("cases")
            if not isinstance(raw_cases, list) or not raw_cases:
                raise _fail("SDAI-WF2-003", f"switch step '{step_id}' must define non-empty cases")
            if len(raw_cases) > MAX_CONTROL_CHILDREN:
                raise _fail("SDAI-WF2-003", f"switch step '{step_id}' has too many cases")
            seen_cases: set[str] = set()
            for case_index, raw_case in enumerate(raw_cases):
                if not isinstance(raw_case, Mapping) or set(raw_case) != {"when", "steps"}:
                    raise _fail(
                        "SDAI-WF2-003",
                        f"switch step '{step_id}' case #{case_index + 1} must contain only when/steps",
                    )
                when = normalize_workflow_expression(
                    raw_case["when"],
                    label=f"switch '{step_id}' case #{case_index + 1} when",
                )
                when_json = _canonical_json(when)
                if when_json in seen_cases:
                    raise _fail("SDAI-WF2-003", f"switch step '{step_id}' contains duplicate case")
                seen_cases.add(when_json)
                branch_id = f"$case/{case_index}"
                case_parent = f"{path}/{branch_id}"
                case_paths = self._children(
                    raw_case["steps"],
                    parent=case_parent,
                    label=f"switch '{step_id}' case #{case_index + 1}",
                )
                self.edges.append(WorkflowGraphEdge(path, case_paths[0], "branch", f"case:{case_index}"))
                branches.append(
                    WorkflowGraphBranch(
                        branch_id,
                        f"case:{case_index}",
                        case_paths,
                        when_json=when_json,
                    )
                )
            raw_default = raw.get("default")
            if raw_default is not None:
                default_parent = f"{path}/$default"
                default_paths = self._children(
                    raw_default,
                    parent=default_parent,
                    label=f"switch '{step_id}' default",
                )
                self.edges.append(WorkflowGraphEdge(path, default_paths[0], "branch", "default"))
                branches.append(WorkflowGraphBranch("$default", "default", default_paths))
            children = tuple(item for branch in branches for item in branch.children)

        elif kind in {WorkflowNodeKind.FAN_OUT.value, WorkflowNodeKind.FOREACH.value}:
            if "items" not in raw:
                raise _fail("SDAI-WF2-003", f"{kind} step '{step_id}' is missing items")
            items = normalize_workflow_expression(raw["items"], label=f"{kind} '{step_id}' items")
            item_name = raw.get("as") or "item"
            if not isinstance(item_name, str) or not _VARIABLE_NAME.fullmatch(item_name):
                raise _fail("SDAI-WF2-003", f"{kind} step '{step_id}' as must be a safe variable name")
            max_items = _positive_bound(
                raw.get("max_items"),
                label=f"{kind} step '{step_id}' max_items",
                maximum=MAX_FAN_ITEMS,
            )
            if isinstance(items, list) and len(items) > max_items:
                raise _fail(
                    "SDAI-WF2-003",
                    f"{kind} step '{step_id}' literal item count exceeds max_items",
                )
            config.update({"items": items, "as": item_name, "maxItems": max_items})
            if kind == WorkflowNodeKind.FAN_OUT.value:
                config["maxConcurrency"] = _positive_bound(
                    raw.get("max_concurrency"),
                    label=f"fan-out step '{step_id}' max_concurrency",
                    maximum=MAX_CONCURRENCY,
                )
            body_parent = f"{path}/$body"
            children = self._children(raw.get("steps"), parent=body_parent, label=f"{kind} '{step_id}' body")
            self.edges.append(WorkflowGraphEdge(path, children[0], "body"))

        elif kind == WorkflowNodeKind.BOUNDED_WHILE.value:
            if "condition" not in raw:
                raise _fail("SDAI-WF2-003", f"bounded-while step '{step_id}' is missing condition")
            condition = normalize_workflow_expression(
                raw["condition"],
                label=f"bounded-while '{step_id}' condition",
            )
            max_iterations = _positive_bound(
                raw.get("max_iterations"),
                label=f"bounded-while step '{step_id}' max_iterations",
                maximum=MAX_LOOP_ITERATIONS,
            )
            config.update({"condition": condition, "maxIterations": max_iterations})
            body_parent = f"{path}/$body"
            children = self._children(
                raw.get("steps"),
                parent=body_parent,
                label=f"bounded-while '{step_id}' body",
            )
            self.edges.append(WorkflowGraphEdge(path, children[0], "body"))

        elif kind == WorkflowNodeKind.FAN_IN.value:
            raw_sources = raw.get("sources")
            if not isinstance(raw_sources, list) or not raw_sources or not all(
                isinstance(item, str) and item.strip() for item in raw_sources
            ):
                raise _fail("SDAI-WF2-003", f"fan-in step '{step_id}' sources must be a non-empty string list")
            sources = tuple(item.strip() for item in raw_sources)
            if len(sources) != len(set(sources)):
                raise _fail("SDAI-WF2-003", f"fan-in step '{step_id}' sources must be unique")
            strategy = str(raw.get("strategy") or "collect")
            if strategy not in _FAN_IN_STRATEGIES:
                raise _fail(
                    "SDAI-WF2-003",
                    f"fan-in step '{step_id}' strategy must be one of: {', '.join(sorted(_FAN_IN_STRATEGIES))}",
                )
            config.update({"sources": list(sources), "strategy": strategy})
            self.fan_in_nodes.append((path, sources))

        else:
            raise AssertionError(kind)

        self.nodes.append(
            WorkflowGraphNode(
                path=path,
                id=step_id,
                kind=kind,
                parent=parent,
                index=index,
                children=children,
                branches=tuple(branches),
                config_json=_canonical_json(config),
            )
        )
        return path

    def resolve_fan_in(self) -> None:
        paths = {node.path for node in self.nodes}
        by_id: dict[str, list[str]] = {}
        for node in self.nodes:
            if node.path == "$root":
                continue
            by_id.setdefault(node.id, []).append(node.path)
        for fan_in_path, sources in self.fan_in_nodes:
            for source in sources:
                if source in paths:
                    resolved = source
                else:
                    matches = by_id.get(source, [])
                    if len(matches) != 1:
                        detail = "missing" if not matches else "ambiguous"
                        raise _fail(
                            "SDAI-WF2-005",
                            f"fan-in '{fan_in_path}' source '{source}' is {detail}; use an exact canonical path",
                        )
                    resolved = matches[0]
                if resolved == fan_in_path:
                    raise _fail("SDAI-WF2-005", f"fan-in '{fan_in_path}' cannot depend on itself")
                self.edges.append(WorkflowGraphEdge(resolved, fan_in_path, "fan-in"))


def _contains_kind(raw_steps: object, kinds: frozenset[str]) -> bool:
    if not isinstance(raw_steps, list):
        return False
    for raw in raw_steps:
        if not isinstance(raw, Mapping):
            continue
        kind = str(raw.get("type") or raw.get("kind") or "").strip()
        if kind in kinds:
            return True
        nested: list[object] = []
        for key in ("steps", "then", "else", "default"):
            value = raw.get(key)
            if isinstance(value, list):
                nested.extend(value)
        cases = raw.get("cases")
        if isinstance(cases, list):
            for case in cases:
                if isinstance(case, Mapping) and isinstance(case.get("steps"), list):
                    nested.extend(case["steps"])
        if _contains_kind(nested, kinds):
            return True
    return False


def _uses_v9_features(raw_steps: object) -> bool:
    if _contains_kind(raw_steps, _NEW_V9_KINDS):
        return True
    if not isinstance(raw_steps, list):
        return False
    for raw in raw_steps:
        if isinstance(raw, Mapping):
            kind = str(raw.get("type") or raw.get("kind") or "").strip()
            if kind == WorkflowNodeKind.PARALLEL.value and "max_concurrency" in raw:
                return True
    return False


def _public_inputs(
    definitions: tuple[object, ...],
    values: Mapping[str, object],
) -> dict[str, object]:
    sensitive = {
        str(getattr(item, "name"))
        for item in definitions
        if bool(getattr(item, "sensitive", False))
    }
    result: dict[str, object] = {}
    for name in sorted(values):
        normalized = _normalize_json(values[name], label=f"workflow input '{name}'")
        if name in sensitive:
            result[name] = {
                "sensitive": True,
                "sha256": _hash_json(normalized),
            }
        else:
            result[name] = normalized
    return result


def load_workflow_graph(
    project_root: Path,
    name: str,
    *,
    input_values: Mapping[str, object] | None = None,
    environ: Mapping[str, str] | None = None,
) -> WorkflowGraphResolution:
    """Resolve an existing workflow into a canonical Workflow Engine 2 graph.

    The function reuses SDAI's existing inheritance, overlay, typed-input, and
    component composition pipeline. It adds v9 control-flow validation and a stable
    graph contract without changing the legacy workflow executor.
    """

    root = project_root.resolve()
    name = _safe_name(name, label="workflow name")
    try:
        resolution = resolve_workflow_data(root, name, environ=environ)
        data = resolution.data
        raw_steps = data.get("steps") or []
        composition = compose_workflow(root, data, input_values=input_values)
    except (WorkflowOverlayError, WorkflowComponentError, FileNotFoundError) as exc:
        raise _fail("SDAI-WF2-001", str(exc)) from exc

    if not isinstance(raw_steps, list) or not raw_steps:
        raise _fail("SDAI-WF2-002", f"workflow '{name}' must define at least one step")
    raw_version = data.get("version")
    if raw_version is None:
        version: int | None = None
    elif isinstance(raw_version, bool) or not isinstance(raw_version, int):
        raise _fail("SDAI-WF2-002", "workflow version must be an integer")
    else:
        version = raw_version

    uses_components = any(isinstance(item, Mapping) and "uses" in item for item in raw_steps)
    uses_typed_inputs = "inputs" in data or "input_values" in data or bool(input_values)
    composed_steps = list(composition.steps)
    uses_plugin = _contains_kind(composed_steps, frozenset({StepKind.PLUGIN.value}))
    uses_safe_command = _contains_kind(
        composed_steps,
        frozenset({WorkflowNodeKind.SAFE_COMMAND.value}),
    )
    uses_v9 = _uses_v9_features(composed_steps)
    if (uses_components or uses_typed_inputs) and (version is None or version < 6):
        raise _fail("SDAI-WF2-002", "workflow components/typed inputs require workflow version 6 or newer")
    if uses_plugin and (version is None or version < 8):
        raise _fail("SDAI-WF2-002", "plugin workflow steps require workflow version 8 or newer")
    if uses_safe_command and (version is None or version < WORKFLOW_ENGINE2_MIN_VERSION):
        raise _fail(
            "SDAI-WF2-002",
            f"safe-command workflow steps require workflow version {WORKFLOW_ENGINE2_MIN_VERSION} or newer",
        )
    if uses_v9 and (version is None or version < WORKFLOW_ENGINE2_MIN_VERSION):
        raise _fail(
            "SDAI-WF2-002",
            f"Workflow Engine 2 control flow requires workflow version {WORKFLOW_ENGINE2_MIN_VERSION} or newer",
        )

    try:
        validation_mode = LifecycleMode(str(data.get("validation_mode") or name)).value
    except ValueError as exc:
        raise _fail(
            "SDAI-WF2-002",
            f"workflow '{name}' must define validation_mode as light, standard, or critical",
        ) from exc

    builder = _GraphBuilder(version, [], [], set(), [])
    top_paths = builder._children(composed_steps, parent="$root", label=f"workflow '{name}'")
    builder.nodes.append(
        WorkflowGraphNode(
            path="$root",
            id="$root",
            kind=WorkflowNodeKind.SEQUENCE.value,
            parent=None,
            index=0,
            children=top_paths,
            config_json=_canonical_json({"synthetic": True}),
        )
    )
    builder.resolve_fan_in()

    mandatory = tuple(sorted(resolution.mandatory_steps))
    missing_mandatory = sorted(item for item in mandatory if item not in builder.seen_ids)
    if missing_mandatory:
        raise _fail(
            "SDAI-WF2-006",
            "organization-mandated workflow step(s) are missing after graph composition: "
            + ", ".join(missing_mandatory),
        )

    nodes = tuple(sorted(builder.nodes, key=lambda item: item.path))
    edges = tuple(
        sorted(
            builder.edges,
            key=lambda item: (item.source, item.target, item.kind, item.label or ""),
        )
    )
    graph = WorkflowGraph("$root", nodes, edges)
    definitions = tuple(
        _normalize_json(item.as_dict(), label="workflow input definition")
        for item in composition.workflow_inputs
    )
    raw_inputs = {
        name: _normalize_json(value, label=f"workflow input '{name}'")
        for name, value in sorted(composition.resolved_workflow_inputs.items())
    }
    public_inputs = _public_inputs(
        composition.workflow_inputs,
        composition.resolved_workflow_inputs,
    )
    components = tuple(
        _normalize_json(item.as_dict(), label="workflow component provenance")
        for item in composition.components
    )
    overlays = tuple(
        _normalize_json(item.as_dict(), label="workflow overlay provenance")
        for item in resolution.overlays
    )
    hooks = tuple(
        _normalize_json(item.as_dict(), label="workflow lifecycle hook provenance")
        for item in resolution.hooks
    )
    return WorkflowGraphResolution(
        name=_safe_name(str(data.get("name") or name), label="workflow definition name"),
        workflow_version=version,
        validation_mode=validation_mode,
        input_definitions=definitions,  # type: ignore[arg-type]
        public_inputs_json=_canonical_json(public_inputs),
        components=components,  # type: ignore[arg-type]
        inheritance=tuple(resolution.inheritance),
        overlays=overlays,  # type: ignore[arg-type]
        lifecycle_hooks=hooks,  # type: ignore[arg-type]
        mandatory_steps=mandatory,
        graph=graph,
        _input_values_json=_canonical_json(raw_inputs),
    )
