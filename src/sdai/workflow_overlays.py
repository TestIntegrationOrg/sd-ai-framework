from __future__ import annotations

import copy
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
import json
import os
from pathlib import Path
import re
from typing import Mapping

import yaml

from sdai.config import load_yaml
from sdai.path_safety import ensure_within_project
from sdai.text import TextEncodingError, read_utf8_text


class WorkflowOverlayError(RuntimeError):
    """Raised when workflow inheritance/overlay resolution is ambiguous or unsafe."""


class WorkflowOverlayLayer(StrEnum):
    ORG = "org"
    REPO = "repo"
    USER = "user"

    @property
    def priority(self) -> int:
        return {
            WorkflowOverlayLayer.ORG: 10,
            WorkflowOverlayLayer.REPO: 20,
            WorkflowOverlayLayer.USER: 30,
        }[self]


@dataclass(frozen=True)
class WorkflowOverlayOperationProvenance:
    op: str
    target: str | None
    inserted_step: str | None
    pre_graph_sha256: str
    post_graph_sha256: str

    def as_dict(self) -> dict[str, object]:
        return {
            "op": self.op,
            "target": self.target,
            "inserted_step": self.inserted_step,
            "pre_graph_sha256": self.pre_graph_sha256,
            "post_graph_sha256": self.post_graph_sha256,
        }


@dataclass(frozen=True)
class WorkflowOverlayProvenance:
    layer: WorkflowOverlayLayer
    overlay_id: str
    source: str
    target: str
    operations: tuple[str, ...]
    hooks: tuple[str, ...]
    required_steps: tuple[str, ...]
    operation_provenance: tuple[WorkflowOverlayOperationProvenance, ...] = ()
    pre_graph_sha256: str | None = None
    post_graph_sha256: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "layer": self.layer.value,
            "overlay_id": self.overlay_id,
            "source": self.source,
            "target": self.target,
            "operations": list(self.operations),
            "hooks": list(self.hooks),
            "required_steps": list(self.required_steps),
            "operation_provenance": [item.as_dict() for item in self.operation_provenance],
            "pre_graph_sha256": self.pre_graph_sha256,
            "post_graph_sha256": self.post_graph_sha256,
        }


@dataclass(frozen=True)
class LifecycleHookProvenance:
    point: str
    anchor_step: str
    layer: WorkflowOverlayLayer
    overlay_id: str
    source: str
    step_ids: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "point": self.point,
            "anchor_step": self.anchor_step,
            "layer": self.layer.value,
            "overlay_id": self.overlay_id,
            "source": self.source,
            "step_ids": list(self.step_ids),
        }


@dataclass(frozen=True)
class WorkflowResolution:
    data: dict[str, object]
    inheritance: tuple[str, ...]
    overlays: tuple[WorkflowOverlayProvenance, ...]
    hooks: tuple[LifecycleHookProvenance, ...]
    mandatory_steps: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "version": 1,
            "inheritance": list(self.inheritance),
            "overlays": [item.as_dict() for item in self.overlays],
            "hooks": [item.as_dict() for item in self.hooks],
            "mandatory_steps": list(self.mandatory_steps),
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), indent=2, sort_keys=True, ensure_ascii=False)


@dataclass(frozen=True)
class _OverlayOperation:
    op: str
    target: str | None
    step: object | None


@dataclass(frozen=True)
class _OverlayDocument:
    layer: WorkflowOverlayLayer
    overlay_id: str
    source: str
    target: str
    operations: tuple[_OverlayOperation, ...]
    hooks: dict[str, tuple[object, ...]]
    required_steps: tuple[str, ...]


@dataclass(frozen=True)
class _PendingHook:
    point: str
    anchor_step: str
    layer: WorkflowOverlayLayer
    overlay_id: str
    source: str
    steps: tuple[object, ...]


@dataclass(frozen=True)
class _StepLocation:
    path: str
    step_id: str
    container: list[object]
    index: int
    step: object


_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_OVERLAY_ID = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?$")
_TOP_LEVEL_KEYS = frozenset(
    {"version", "id", "workflow", "operations", "hooks", "required_steps"}
)
_OPERATION_KEYS = frozenset({"op", "target", "step"})
_OPERATION_ALIASES = {
    "prepend": "prepend",
    "append": "append",
    "add-before": "insert-before",
    "add-after": "insert-after",
    "insert-before": "insert-before",
    "insert-after": "insert-after",
    "replace": "replace",
    "disable": "remove",
    "remove": "remove",
}
_OPERATION_TYPES = frozenset(_OPERATION_ALIASES)
_HOOK_POINTS = (
    "before:requirements",
    "after:requirements",
    "before:architecture",
    "after:architecture",
    "before:implementation",
    "after:implementation",
    "before:verify",
    "after:verify",
    "before:delivery",
    "after:delivery",
    "before:pr",
    "after:pr",
)
_HOOK_POINT_SET = frozenset(_HOOK_POINTS)
_LIFECYCLE_KEYS = frozenset(
    {"requirements", "architecture", "implementation", "verify", "delivery", "pr"}
)
_FORBIDDEN_STEP_KEYS = frozenset(
    {"profile", "provider", "shell", "command", "commands", "exec", "executable", "argv"}
)
_STEP_TYPES = frozenset(
    {
        "deterministic", "agent", "approval", "validate", "validator",
        "quality-gate", "parallel", "sequence", "if", "switch", "fan-out",
        "fan-in", "foreach", "bounded-while",
    }
)
_PROTECTED_STEP_TYPES = frozenset(
    {"approval", "quality-gate", "validate", "validator", "parallel", "fan-out"}
)
_CONCURRENT_STEP_TYPES = frozenset({"parallel", "fan-out"})
_VALIDATION_RANK = {"light": 0, "standard": 1, "critical": 2}


def _fail(code: str, message: str) -> WorkflowOverlayError:
    return WorkflowOverlayError(f"{code}: {message}")


def _safe_name(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not _SAFE_NAME.fullmatch(value.strip()):
        raise _fail(
            "SDAI-WFOVER-001",
            f"{label} must use only letters, numbers, dot, underscore, or hyphen",
        )
    return value.strip()


def _safe_target(value: object, *, label: str) -> str:
    if not isinstance(value, str) or value != value.strip() or not value:
        raise _fail("SDAI-WFOVER-001", f"{label} must be a canonical step path or identity")
    if "\\" in value or "//" in value or value.startswith(("/", "$root")):
        raise _fail("SDAI-WFOVER-001", f"{label} must be a canonical step path or identity")
    parts = value.split("/")
    structural = {"$then", "$else", "$default", "$body", "$case"}
    for index, part in enumerate(parts):
        if part in structural:
            continue
        if index > 0 and parts[index - 1] == "$case" and part.isdigit():
            continue
        _safe_name(part, label=label)
    return value


def _portable(root: Path, path: Path) -> str:
    safe = ensure_within_project(root, path, label="workflow overlay path")
    return safe.relative_to(root.resolve()).as_posix()


def _external_source(path: Path) -> str:
    return path.resolve().as_posix()


def _workflow_path(root: Path, name: str) -> Path:
    return ensure_within_project(
        root,
        root / ".sdai" / "workflows" / f"{name}.yaml",
        label=f"workflow '{name}'",
    )


def _raw_step_id(raw: object) -> str | None:
    if isinstance(raw, str):
        return raw.strip() or None
    if isinstance(raw, Mapping):
        value = raw.get("id")
        return str(value).strip() if value is not None and str(value).strip() else None
    return None


def _step_type(raw: object) -> str:
    if isinstance(raw, str):
        return "validate" if raw.strip() == "validate" else "deterministic"
    if not isinstance(raw, Mapping):
        return ""
    kind = str(raw.get("type") or raw.get("kind") or "").strip()
    return "validate" if kind == "validator" else kind


def _agent_mode(raw: object) -> str:
    if not isinstance(raw, Mapping):
        return "advisory"
    return str(raw.get("mode") or "advisory").strip()


def _agent_capability(raw: object) -> str:
    if not isinstance(raw, Mapping):
        return ""
    return str(raw.get("capability") or "").strip()


def _agent_name(raw: object) -> str:
    if not isinstance(raw, Mapping):
        return ""
    return str(raw.get("agent") or "").strip()


def _deterministic_action(raw: object) -> str:
    if isinstance(raw, str):
        return raw.strip()
    if not isinstance(raw, Mapping):
        return ""
    return str(raw.get("action") or raw.get("id") or "").strip()


def _nested_step_lists(raw: object, *, path: str) -> tuple[tuple[str, list[object]], ...]:
    if not isinstance(raw, Mapping):
        return ()
    kind = _step_type(raw)
    result: list[tuple[str, list[object]]] = []

    def add(suffix: str, value: object) -> None:
        if isinstance(value, list):
            result.append((f"{path}/{suffix}" if suffix else path, value))

    if kind in {"sequence", "parallel"}:
        add("", raw.get("steps"))
    elif kind == "if":
        add("$then", raw.get("then"))
        add("$else", raw.get("else"))
    elif kind == "switch":
        cases = raw.get("cases")
        if isinstance(cases, list):
            for index, case in enumerate(cases):
                if isinstance(case, Mapping):
                    add(f"$case/{index}", case.get("steps"))
        add("$default", raw.get("default"))
    elif kind in {"fan-out", "foreach", "bounded-while"}:
        add("$body", raw.get("steps"))
    return tuple(result)


def _walk_steps(steps: list[object], *, parent: str = "$root") -> tuple[_StepLocation, ...]:
    result: list[_StepLocation] = []
    for index, step in enumerate(steps):
        step_id = _raw_step_id(step)
        if step_id is None:
            continue
        path = step_id if parent == "$root" else f"{parent}/{step_id}"
        result.append(_StepLocation(path, step_id, steps, index, step))
        for nested_parent, nested in _nested_step_lists(step, path=path):
            result.extend(_walk_steps(nested, parent=nested_parent))
    return tuple(result)


def _declares_workspace_write(raw: object) -> bool:
    if not isinstance(raw, Mapping):
        return False
    kind = _step_type(raw)
    if kind == "agent" and _agent_mode(raw) != "advisory":
        return True
    if kind == "safe-command" and raw.get("workspace_write") is True:
        return True
    if kind == "plugin":
        # Plugin permissions are resolved from the trusted registry rather than the
        # raw step. An overlay cannot prove the plugin read-only at graph-edit time.
        return True
    return False


def _contains_workspace_write(raw: object) -> bool:
    if _declares_workspace_write(raw):
        return True
    for nested_parent, children in _nested_step_lists(raw, path="step"):
        del nested_parent
        if any(_contains_workspace_write(child) for child in children):
            return True
    return False


def _is_protected_step(raw: object) -> bool:
    kind = _step_type(raw)
    if kind in _PROTECTED_STEP_TYPES:
        return True
    if kind == "agent" and _agent_capability(raw) == "security":
        return True
    return any(
        _is_protected_step(child)
        for _, children in _nested_step_lists(raw, path="step")
        for child in children
    )


def _validate_overlay_step(raw: object, *, label: str, hook: bool = False) -> None:
    if not isinstance(raw, Mapping):
        raise _fail(
            "SDAI-WFOVER-003",
            f"{label} must be a step mapping; shorthand/component uses are not allowed in overlays",
        )
    forbidden = sorted(set(raw) & _FORBIDDEN_STEP_KEYS)
    if forbidden:
        raise _fail(
            "SDAI-WFOVER-003",
            f"{label} contains forbidden provider/shell field(s): {', '.join(forbidden)}",
        )
    if "uses" in raw:
        raise _fail(
            "SDAI-WFOVER-003",
            f"{label} cannot use nested workflow components; add the component to the base/derived workflow",
        )
    step_id = _safe_name(raw.get("id"), label=f"{label} id")
    kind = _step_type(raw)
    if kind not in _STEP_TYPES:
        raise _fail(
            "SDAI-WFOVER-003",
            f"{label} '{step_id}' has unsupported step type '{kind}'",
        )
    if hook:
        if kind not in {"agent", "approval", "quality-gate", "validate"}:
            raise _fail(
                "SDAI-WFOVER-007",
                f"lifecycle hook step '{step_id}' type '{kind}' is not allowed; hooks are review/gate/validation only",
            )
        if kind == "agent" and _agent_mode(raw) != "advisory":
            raise _fail(
                "SDAI-WFOVER-007",
                f"lifecycle hook agent '{step_id}' must use advisory mode",
            )
    nested = _nested_step_lists(raw, path=step_id)
    for nested_path, children in nested:
        if not children:
            raise _fail(
                "SDAI-WFOVER-003",
                f"overlay control path '{nested_path}' must contain a non-empty step list",
            )
        for index, child in enumerate(children):
            _validate_overlay_step(child, label=f"{label} {nested_path} child #{index + 1}", hook=hook)
    if kind in _CONCURRENT_STEP_TYPES and any(
        _contains_workspace_write(child)
        for _, children in nested
        for child in children
    ):
        raise _fail(
            "SDAI-WFOVER-004",
            f"overlay concurrent step '{step_id}' cannot contain workspace-writing branches",
        )


def _validation_mode(value: object, *, label: str) -> str:
    mode = str(value or "").strip()
    if mode not in _VALIDATION_RANK:
        raise _fail(
            "SDAI-WFOVER-002",
            f"{label} validation_mode must be light, standard, or critical",
        )
    return mode


def _merge_inherited(parent: Mapping[str, object], child: Mapping[str, object], *, child_name: str) -> dict[str, object]:
    result = dict(parent)
    parent_mode = _validation_mode(parent.get("validation_mode") or "standard", label="parent workflow")
    child_mode = _validation_mode(child.get("validation_mode") or parent_mode, label=f"workflow '{child_name}'")
    if _VALIDATION_RANK[child_mode] < _VALIDATION_RANK[parent_mode]:
        raise _fail(
            "SDAI-WFOVER-002",
            f"workflow '{child_name}' cannot lower inherited validation_mode from {parent_mode} to {child_mode}",
        )

    parent_steps = parent.get("steps") or []
    child_steps = child.get("steps") or []
    if not isinstance(parent_steps, list) or not isinstance(child_steps, list):
        raise _fail("SDAI-WFOVER-002", "inherited workflow steps must be lists")
    result["steps"] = [*parent_steps, *child_steps]

    for key in ("inputs", "input_values", "lifecycle"):
        parent_values = parent.get(key) or {}
        child_values = child.get(key) or {}
        if not isinstance(parent_values, Mapping) or not isinstance(child_values, Mapping):
            raise _fail(
                "SDAI-WFOVER-002",
                f"inherited workflow field '{key}' must be a mapping",
            )
        if parent_values or child_values:
            merged = dict(parent_values)
            merged.update(child_values)
            result[key] = merged

    for key, value in child.items():
        if key in {"extends", "steps", "inputs", "input_values", "lifecycle"}:
            continue
        result[key] = value
    result["validation_mode"] = child_mode
    result["version"] = max(
        7,
        int(parent.get("version") or 0) if isinstance(parent.get("version"), int) else 0,
        int(child.get("version") or 0) if isinstance(child.get("version"), int) else 0,
    )
    result.setdefault("name", child_name)
    return result


def _load_inherited_workflow(root: Path, name: str, stack: tuple[str, ...] = ()) -> tuple[dict[str, object], tuple[str, ...]]:
    name = _safe_name(name, label="workflow name")
    if name in stack:
        cycle = " -> ".join((*stack, name))
        raise _fail("SDAI-WFOVER-002", f"workflow inheritance cycle: {cycle}")
    path = _workflow_path(root, name)
    if not path.exists():
        raise FileNotFoundError(path)
    data = load_yaml(path)
    parent_name = data.get("extends")
    if parent_name is None:
        result = dict(data)
        result.pop("extends", None)
        return result, (name,)
    parent = _safe_name(parent_name, label=f"workflow '{name}' extends")
    parent_data, inheritance = _load_inherited_workflow(root, parent, (*stack, name))
    return _merge_inherited(parent_data, data, child_name=name), (*inheritance, name)


def _external_paths(value: str | None, *, label: str) -> tuple[Path, ...]:
    if not value:
        return ()
    path = Path(value)
    if not path.is_absolute():
        raise _fail("SDAI-WFOVER-008", f"{label} must be an absolute file or directory path")
    if path.is_symlink():
        raise _fail("SDAI-WFOVER-008", f"{label} must not be a symlink")
    if path.is_file():
        return (path,)
    if path.is_dir():
        candidates = tuple(
            sorted(
                [*path.glob("*.yaml"), *path.glob("*.yml")],
                key=lambda item: item.name.casefold(),
            )
        )
        for candidate in candidates:
            if candidate.is_symlink() or not candidate.is_file():
                raise _fail(
                    "SDAI-WFOVER-008",
                    f"{label} overlay must be a regular non-symlink file: {candidate}",
                )
        return candidates
    raise _fail("SDAI-WFOVER-008", f"{label} does not exist: {path}")


def _overlay_files(
    root: Path,
    *,
    environ: Mapping[str, str],
) -> tuple[tuple[WorkflowOverlayLayer, Path, str], ...]:
    result: list[tuple[WorkflowOverlayLayer, Path, str]] = []
    for path in _external_paths(
        environ.get("SDAI_ORG_WORKFLOW_OVERLAY_PATH"),
        label="SDAI_ORG_WORKFLOW_OVERLAY_PATH",
    ):
        result.append((WorkflowOverlayLayer.ORG, path, _external_source(path)))

    repo_dir = ensure_within_project(
        root,
        root / ".sdai" / "workflow-overlays",
        label="repository workflow overlay directory",
    )
    if repo_dir.exists():
        if repo_dir.is_symlink() or not repo_dir.is_dir():
            raise _fail(
                "SDAI-WFOVER-008",
                ".sdai/workflow-overlays must be a real directory",
            )
        for path in sorted(
            [*repo_dir.glob("*.yaml"), *repo_dir.glob("*.yml")],
            key=lambda item: item.name.casefold(),
        ):
            if path.is_symlink() or not path.is_file():
                raise _fail(
                    "SDAI-WFOVER-008",
                    f"repository workflow overlay must be a regular non-symlink file: {_portable(root, path)}",
                )
            result.append((WorkflowOverlayLayer.REPO, path, _portable(root, path)))

    for path in _external_paths(
        environ.get("SDAI_USER_WORKFLOW_OVERLAY_PATH"),
        label="SDAI_USER_WORKFLOW_OVERLAY_PATH",
    ):
        result.append((WorkflowOverlayLayer.USER, path, _external_source(path)))
    return tuple(result)


def _string_list(value: object, *, label: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise _fail("SDAI-WFOVER-001", f"{label} must be a string list")
    normalized = tuple(_safe_name(item, label=label) for item in value)
    if len(normalized) != len(set(normalized)):
        raise _fail("SDAI-WFOVER-001", f"{label} must not contain duplicates")
    return normalized


def _parse_overlay(
    path: Path,
    *,
    layer: WorkflowOverlayLayer,
    source: str,
) -> _OverlayDocument:
    try:
        raw = yaml.safe_load(read_utf8_text(path)) or {}
    except (OSError, TextEncodingError, yaml.YAMLError) as exc:
        raise _fail("SDAI-WFOVER-001", f"unable to read workflow overlay {source}: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise _fail("SDAI-WFOVER-001", f"workflow overlay {source} must be a mapping")
    unknown = sorted(set(raw) - _TOP_LEVEL_KEYS)
    if unknown:
        raise _fail(
            "SDAI-WFOVER-001",
            f"workflow overlay {source} has unknown field(s): {', '.join(map(str, unknown))}",
        )
    if raw.get("version") != 1:
        raise _fail("SDAI-WFOVER-001", f"workflow overlay {source} version must be 1")
    overlay_id = raw.get("id")
    if not isinstance(overlay_id, str) or not _OVERLAY_ID.fullmatch(overlay_id):
        raise _fail("SDAI-WFOVER-001", f"workflow overlay {source} id is invalid")
    target = _safe_name(raw.get("workflow"), label=f"workflow overlay '{overlay_id}' target")

    required_steps = _string_list(
        raw.get("required_steps"),
        label=f"workflow overlay '{overlay_id}' required_steps",
    )
    if required_steps and layer is not WorkflowOverlayLayer.ORG:
        raise _fail(
            "SDAI-WFOVER-004",
            f"workflow overlay '{overlay_id}' cannot declare required_steps from non-organization layer '{layer.value}'",
        )

    raw_operations = raw.get("operations") or []
    if not isinstance(raw_operations, list):
        raise _fail("SDAI-WFOVER-001", f"workflow overlay '{overlay_id}' operations must be a list")
    operations: list[_OverlayOperation] = []
    for index, item in enumerate(raw_operations):
        if not isinstance(item, Mapping):
            raise _fail("SDAI-WFOVER-001", f"workflow overlay '{overlay_id}' operation #{index + 1} must be a mapping")
        operation_unknown = sorted(set(item) - _OPERATION_KEYS)
        if operation_unknown:
            raise _fail(
                "SDAI-WFOVER-001",
                f"workflow overlay '{overlay_id}' operation #{index + 1} has unknown field(s): {', '.join(map(str, operation_unknown))}",
            )
        raw_op = str(item.get("op") or "").strip()
        if raw_op not in _OPERATION_TYPES:
            raise _fail("SDAI-WFOVER-001", f"workflow overlay '{overlay_id}' operation #{index + 1} has unsupported op '{raw_op}'")
        op = _OPERATION_ALIASES[raw_op]
        target_value = item.get("target")
        target_id = None if target_value is None else _safe_target(target_value, label=f"workflow overlay '{overlay_id}' operation target")
        step = item.get("step")
        if op in {"insert-before", "insert-after", "replace", "remove"} and target_id is None:
            raise _fail("SDAI-WFOVER-001", f"workflow overlay '{overlay_id}' operation '{op}' requires target")
        if op in {"prepend", "append", "insert-before", "insert-after", "replace"}:
            if step is None:
                raise _fail("SDAI-WFOVER-001", f"workflow overlay '{overlay_id}' operation '{op}' requires step")
            _validate_overlay_step(
                step,
                label=f"workflow overlay '{overlay_id}' operation #{index + 1} step",
            )
            if op == "replace" and _raw_step_id(step) != target_id.rsplit("/", 1)[-1]:
                raise _fail(
                    "SDAI-WFOVER-003",
                    f"workflow overlay '{overlay_id}' replacement step id must remain '{target_id.rsplit('/', 1)[-1]}'",
                )
        elif step is not None:
            raise _fail("SDAI-WFOVER-001", f"workflow overlay '{overlay_id}' remove operation must not define step")
        operations.append(_OverlayOperation(op, target_id, step))

    raw_hooks = raw.get("hooks") or {}
    if not isinstance(raw_hooks, Mapping):
        raise _fail("SDAI-WFOVER-001", f"workflow overlay '{overlay_id}' hooks must be a mapping")
    hooks: dict[str, tuple[object, ...]] = {}
    for point, steps in raw_hooks.items():
        point_text = str(point)
        if point_text not in _HOOK_POINT_SET:
            raise _fail(
                "SDAI-WFOVER-007",
                f"workflow overlay '{overlay_id}' has unsupported lifecycle hook '{point_text}'",
            )
        if not isinstance(steps, list) or not steps:
            raise _fail("SDAI-WFOVER-007", f"workflow overlay '{overlay_id}' hook '{point_text}' must be a non-empty step list")
        for index, step in enumerate(steps):
            _validate_overlay_step(
                step,
                label=f"workflow overlay '{overlay_id}' hook '{point_text}' step #{index + 1}",
                hook=True,
            )
        hooks[point_text] = tuple(steps)

    return _OverlayDocument(
        layer=layer,
        overlay_id=overlay_id,
        source=source,
        target=target,
        operations=tuple(operations),
        hooks=hooks,
        required_steps=required_steps,
    )


def _find_step_location(steps: list[object], target: str) -> _StepLocation:
    locations = _walk_steps(steps)
    if "/" in target:
        matches = [item for item in locations if item.path == target]
    else:
        matches = [item for item in locations if item.step_id == target]
    if not matches:
        raise _fail("SDAI-WFOVER-005", f"workflow overlay target step '{target}' does not exist")
    if len(matches) > 1:
        paths = ", ".join(sorted(item.path for item in matches))
        raise _fail(
            "SDAI-WFOVER-003",
            f"workflow overlay target step '{target}' is ambiguous; use one of: {paths}",
        )
    return matches[0]


def _ensure_unique_step_id(steps: list[object], raw_step: object, *, label: str) -> str:
    step_id = _raw_step_id(raw_step)
    if step_id is None:
        raise _fail("SDAI-WFOVER-003", f"{label} is missing step id")
    new_ids = [item.step_id for item in _walk_steps([raw_step])]
    if len(new_ids) != len(set(new_ids)):
        raise _fail("SDAI-WFOVER-003", f"{label} contains duplicate nested step ids")
    existing_ids = {item.step_id for item in _walk_steps(steps)}
    duplicate = sorted(existing_ids & set(new_ids))
    if duplicate:
        raise _fail("SDAI-WFOVER-003", f"{label} would duplicate step id '{duplicate[0]}'")
    return step_id


def _subtree_ids(raw: object) -> set[str]:
    return {item.step_id for item in _walk_steps([raw])}


def _graph_hash(steps: list[object]) -> str:
    try:
        canonical = json.dumps(
            {"steps": steps},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise _fail("SDAI-WFOVER-003", "overlay graph must be canonical finite JSON data") from exc
    return "sha256:" + sha256(canonical.encode("utf-8")).hexdigest()


def _writable_concurrent_branches(steps: list[object]) -> set[tuple[str, str]]:
    locations = _walk_steps(steps)
    concurrent = [item for item in locations if _step_type(item.step) in _CONCURRENT_STEP_TYPES]
    writers = [item for item in locations if _declares_workspace_write(item.step)]
    return {
        (parent.path, writer.path)
        for parent in concurrent
        for writer in writers
        if writer.path.startswith(parent.path + "/")
    }


def _validate_lower_replacement(existing: object, replacement: object, *, target: str, layer: WorkflowOverlayLayer) -> None:
    if _is_protected_step(existing):
        raise _fail(
            "SDAI-WFOVER-004",
            f"{layer.value} overlay cannot replace protected approval/gate/validation/parallel/security step '{target}'",
        )
    existing_type = _step_type(existing)
    replacement_type = _step_type(replacement)
    if existing_type != replacement_type:
        raise _fail(
            "SDAI-WFOVER-004",
            f"{layer.value} overlay cannot change step '{target}' type from {existing_type} to {replacement_type}",
        )
    if existing_type == "agent":
        if _agent_capability(existing) != _agent_capability(replacement):
            raise _fail(
                "SDAI-WFOVER-004",
                f"{layer.value} overlay cannot change agent step '{target}' capability",
            )
        if _agent_name(existing) != _agent_name(replacement):
            raise _fail(
                "SDAI-WFOVER-004",
                f"{layer.value} overlay cannot change semantic agent for step '{target}'",
            )
        if _agent_mode(existing) == "advisory" and _agent_mode(replacement) != "advisory":
            raise _fail(
                "SDAI-WFOVER-004",
                f"{layer.value} overlay cannot widen advisory step '{target}' to workspace-write",
            )
    if existing_type == "deterministic" and _deterministic_action(existing) != _deterministic_action(replacement):
        raise _fail(
            "SDAI-WFOVER-004",
            f"{layer.value} overlay cannot change deterministic action for step '{target}'",
        )


def _lifecycle_anchors(data: Mapping[str, object]) -> dict[str, str]:
    raw = data.get("lifecycle") or {}
    if not isinstance(raw, Mapping):
        raise _fail("SDAI-WFOVER-006", "workflow lifecycle must be a mapping")
    unknown = sorted(set(raw) - _LIFECYCLE_KEYS)
    if unknown:
        raise _fail(
            "SDAI-WFOVER-006",
            f"workflow lifecycle contains unsupported phase(s): {', '.join(map(str, unknown))}",
        )
    return {
        str(phase): _safe_name(step_id, label=f"workflow lifecycle phase '{phase}' anchor")
        for phase, step_id in raw.items()
    }


def _apply_operation(
    steps: list[object],
    operation: _OverlayOperation,
    *,
    layer: WorkflowOverlayLayer,
    mandatory_steps: set[str],
    protected_anchors: set[str],
) -> tuple[str, str | None, str | None]:
    op = operation.op
    target = operation.target
    if op == "prepend":
        assert operation.step is not None
        step_id = _ensure_unique_step_id(steps, operation.step, label=f"{layer.value} prepend")
        steps.insert(0, operation.step)
        return f"prepend:{step_id}", None, step_id
    if op == "append":
        assert operation.step is not None
        step_id = _ensure_unique_step_id(steps, operation.step, label=f"{layer.value} append")
        steps.append(operation.step)
        return f"append:{step_id}", None, step_id

    assert target is not None
    location = _find_step_location(steps, target)
    existing = location.step
    canonical_target = location.path
    destructive = op in {"replace", "remove"}
    if layer is not WorkflowOverlayLayer.ORG and destructive:
        affected_ids = _subtree_ids(existing)
        mandated = sorted(affected_ids & mandatory_steps)
        if mandated:
            raise _fail(
                "SDAI-WFOVER-004",
                f"{layer.value} overlay cannot modify organization-mandated step '{mandated[0]}'",
            )
        anchors = sorted(affected_ids & protected_anchors)
        if anchors:
            raise _fail(
                "SDAI-WFOVER-004",
                f"{layer.value} overlay cannot modify lifecycle-hook anchor step '{anchors[0]}'",
            )

    if op == "remove":
        if layer is not WorkflowOverlayLayer.ORG and _is_protected_step(existing):
            raise _fail(
                "SDAI-WFOVER-004",
                f"{layer.value} overlay cannot disable protected step '{canonical_target}'",
            )
        location.container.pop(location.index)
        return f"remove:{canonical_target}", canonical_target, None

    assert operation.step is not None
    if op == "replace":
        if layer is not WorkflowOverlayLayer.ORG:
            _validate_lower_replacement(
                existing,
                operation.step,
                target=canonical_target,
                layer=layer,
            )
        replacement_ids = [item.step_id for item in _walk_steps([operation.step])]
        if len(replacement_ids) != len(set(replacement_ids)):
            raise _fail("SDAI-WFOVER-003", f"{layer.value} replacement contains duplicate nested step ids")
        outside_ids = {item.step_id for item in _walk_steps(steps)} - _subtree_ids(existing)
        duplicates = sorted(outside_ids & set(replacement_ids))
        if duplicates:
            raise _fail(
                "SDAI-WFOVER-003",
                f"{layer.value} replacement would duplicate step id '{duplicates[0]}'",
            )
        location.container[location.index] = operation.step
        return f"replace:{canonical_target}", canonical_target, _raw_step_id(operation.step)

    step_id = _ensure_unique_step_id(steps, operation.step, label=f"{layer.value} {op}")
    if op == "insert-before":
        location.container.insert(location.index, operation.step)
    elif op == "insert-after":
        location.container.insert(location.index + 1, operation.step)
    else:  # pragma: no cover - parser prevents this
        raise AssertionError(op)
    return f"{op}:{canonical_target}:{step_id}", canonical_target, step_id


def _insert_hooks(
    steps: list[object],
    pending: tuple[_PendingHook, ...],
) -> tuple[LifecycleHookProvenance, ...]:
    grouped: dict[str, list[_PendingHook]] = {point: [] for point in _HOOK_POINTS}
    for item in pending:
        grouped[item.point].append(item)

    provenance: list[LifecycleHookProvenance] = []
    known_ids = {item.step_id for item in _walk_steps(steps)}
    for point in _HOOK_POINTS:
        entries = grouped[point]
        if not entries:
            continue
        anchor = entries[0].anchor_step
        if any(item.anchor_step != anchor for item in entries):
            raise _fail(
                "SDAI-WFOVER-006",
                f"lifecycle hook '{point}' resolved to inconsistent anchor steps",
            )
        anchor_location = _find_step_location(steps, anchor)
        flattened: list[object] = []
        for entry in entries:
            step_ids: list[str] = []
            for raw_step in entry.steps:
                step_id = _raw_step_id(raw_step)
                assert step_id is not None
                if step_id in known_ids:
                    raise _fail(
                        "SDAI-WFOVER-003",
                        f"lifecycle hook '{point}' duplicates step id '{step_id}'",
                    )
                known_ids.add(step_id)
                step_ids.append(step_id)
                flattened.append(raw_step)
            provenance.append(
                LifecycleHookProvenance(
                    point=point,
                    anchor_step=anchor,
                    layer=entry.layer,
                    overlay_id=entry.overlay_id,
                    source=entry.source,
                    step_ids=tuple(step_ids),
                )
            )
        insert_at = anchor_location.index if point.startswith("before:") else anchor_location.index + 1
        anchor_location.container[insert_at:insert_at] = flattened
    return tuple(provenance)


def resolve_workflow_data(
    project_root: Path,
    name: str,
    *,
    environ: Mapping[str, str] | None = None,
) -> WorkflowResolution:
    """Resolve inheritance, layered overlays, and safe lifecycle hooks deterministically."""

    root = project_root.resolve()
    env = dict(os.environ if environ is None else environ)
    data, inheritance = _load_inherited_workflow(root, name)
    steps = copy.deepcopy(list(data.get("steps") or []))
    if not steps:
        raise _fail("SDAI-WFOVER-002", f"workflow '{name}' must define at least one effective step")
    lifecycle = _lifecycle_anchors(data)

    overlays: list[WorkflowOverlayProvenance] = []
    pending_hooks: list[_PendingHook] = []
    mandatory_steps: set[str] = set()
    protected_anchors: set[str] = set()
    seen_overlay_ids: dict[WorkflowOverlayLayer, set[str]] = {
        layer: set() for layer in WorkflowOverlayLayer
    }
    mutated_targets: dict[WorkflowOverlayLayer, set[str]] = {
        layer: set() for layer in WorkflowOverlayLayer
    }

    documents: list[_OverlayDocument] = []
    for layer, path, source in _overlay_files(root, environ=env):
        document = _parse_overlay(path, layer=layer, source=source)
        if document.target not in inheritance:
            continue
        if document.overlay_id in seen_overlay_ids[layer]:
            raise _fail(
                "SDAI-WFOVER-001",
                f"duplicate workflow overlay id '{document.overlay_id}' in layer '{layer.value}'",
            )
        seen_overlay_ids[layer].add(document.overlay_id)
        documents.append(document)

    inheritance_rank = {workflow_name: index for index, workflow_name in enumerate(inheritance)}
    documents.sort(
        key=lambda item: (
            item.layer.priority,
            inheritance_rank[item.target],
            item.overlay_id,
            item.source.casefold(),
        )
    )

    for document in documents:
        document_pre_hash = _graph_hash(steps)
        operation_labels: list[str] = []
        operation_provenance: list[WorkflowOverlayOperationProvenance] = []
        for operation in document.operations:
            if operation.op in {"replace", "remove"}:
                assert operation.target is not None
                prior_targets = sorted(
                    path
                    for path in mutated_targets[document.layer]
                    if path == operation.target
                    or ("/" not in operation.target and path.rsplit("/", 1)[-1] == operation.target)
                )
                if prior_targets:
                    raise _fail(
                        "SDAI-WFOVER-003",
                        f"layer '{document.layer.value}' mutates step '{prior_targets[0]}' more than once",
                    )
                canonical_target = _find_step_location(steps, operation.target).path
                if canonical_target in mutated_targets[document.layer]:
                    raise _fail(
                        "SDAI-WFOVER-003",
                        f"layer '{document.layer.value}' mutates step '{canonical_target}' more than once",
                    )
                mutated_targets[document.layer].add(canonical_target)
            pre_hash = _graph_hash(steps)
            writable_concurrent_before = _writable_concurrent_branches(steps)
            label, resolved_target, inserted_step = _apply_operation(
                steps,
                operation,
                layer=document.layer,
                mandatory_steps=mandatory_steps,
                protected_anchors=protected_anchors,
            )
            newly_writable_concurrent = sorted(
                _writable_concurrent_branches(steps) - writable_concurrent_before
            )
            if newly_writable_concurrent:
                raise _fail(
                    "SDAI-WFOVER-004",
                    "overlay operation cannot add workspace-writing concurrent branch under "
                    f"'{newly_writable_concurrent[0][0]}'",
                )
            post_hash = _graph_hash(steps)
            operation_labels.append(label)
            operation_provenance.append(
                WorkflowOverlayOperationProvenance(
                    op=operation.op,
                    target=resolved_target,
                    inserted_step=inserted_step,
                    pre_graph_sha256=pre_hash,
                    post_graph_sha256=post_hash,
                )
            )
            if document.layer is WorkflowOverlayLayer.ORG and operation.op != "remove":
                mandatory_steps.update(_subtree_ids(operation.step))

        if document.layer is WorkflowOverlayLayer.ORG:
            mandatory_steps.update(document.required_steps)

        hook_labels: list[str] = []
        for point in _HOOK_POINTS:
            hook_steps = document.hooks.get(point)
            if not hook_steps:
                continue
            phase = point.split(":", 1)[1]
            anchor = lifecycle.get(phase)
            if anchor is None:
                raise _fail(
                    "SDAI-WFOVER-006",
                    f"workflow '{name}' does not declare lifecycle anchor '{phase}' required by hook '{point}'",
                )
            _find_step_location(steps, anchor)
            protected_anchors.add(anchor)
            pending_hooks.append(
                _PendingHook(
                    point=point,
                    anchor_step=anchor,
                    layer=document.layer,
                    overlay_id=document.overlay_id,
                    source=document.source,
                    steps=hook_steps,
                )
            )
            hook_ids = tuple(_raw_step_id(step) for step in hook_steps)
            hook_labels.append(f"{point}:{','.join(str(item) for item in hook_ids)}")
            if document.layer is WorkflowOverlayLayer.ORG:
                mandatory_steps.update(str(item) for item in hook_ids if item)
                mandatory_steps.add(anchor)

        overlays.append(
            WorkflowOverlayProvenance(
                layer=document.layer,
                overlay_id=document.overlay_id,
                source=document.source,
                target=document.target,
                operations=tuple(operation_labels),
                hooks=tuple(hook_labels),
                required_steps=document.required_steps,
                operation_provenance=tuple(operation_provenance),
                pre_graph_sha256=document_pre_hash,
                post_graph_sha256=_graph_hash(steps),
            )
        )

    hook_provenance = _insert_hooks(steps, tuple(pending_hooks))
    effective_ids = {item.step_id for item in _walk_steps(steps)}
    missing_mandatory = sorted(step_id for step_id in mandatory_steps if step_id not in effective_ids)
    if missing_mandatory:
        raise _fail(
            "SDAI-WFOVER-004",
            "organization-mandated workflow step(s) are missing after overlay resolution: "
            + ", ".join(missing_mandatory),
        )

    effective = dict(data)
    effective["steps"] = steps
    effective.pop("extends", None)
    if documents or len(inheritance) > 1:
        raw_version = effective.get("version")
        effective["version"] = max(
            7,
            raw_version if isinstance(raw_version, int) and not isinstance(raw_version, bool) else 0,
        )
    return WorkflowResolution(
        data=effective,
        inheritance=inheritance,
        overlays=tuple(overlays),
        hooks=hook_provenance,
        mandatory_steps=tuple(sorted(mandatory_steps)),
    )
