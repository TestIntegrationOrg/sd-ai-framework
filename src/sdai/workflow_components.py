from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any, Mapping

import yaml

from sdai.extensions.manifests import (
    ExtensionKind,
    ExtensionManifestError,
    parse_extension_manifest,
)
from sdai.path_safety import ensure_within_project
from sdai.text import TextEncodingError, read_utf8_text


class WorkflowComponentError(RuntimeError):
    """Raised when workflow/component composition is invalid or unsafe."""


@dataclass(frozen=True)
class TypedInputDefinition:
    name: str
    type: str
    required: bool
    default: object | None
    enum: tuple[object, ...]
    sensitive: bool = False

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "name": self.name,
            "type": self.type,
            "required": self.required,
            "sensitive": self.sensitive,
        }
        if self.default is not None:
            payload["default"] = self.default
        if self.enum:
            payload["enum"] = list(self.enum)
        return payload


@dataclass(frozen=True)
class WorkflowComponentDefinition:
    id: str
    version: str
    description: str
    source: str
    inputs: tuple[TypedInputDefinition, ...]
    requires: tuple[str, ...]
    steps: tuple[object, ...]

    def input_map(self) -> dict[str, TypedInputDefinition]:
        return {item.name: item for item in self.inputs}


@dataclass(frozen=True)
class ComponentUseProvenance:
    component_id: str
    version: str
    source: str
    use_index: int
    inputs: dict[str, object]
    requires: tuple[str, ...]
    expanded_step_ids: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "component_id": self.component_id,
            "version": self.version,
            "source": self.source,
            "use_index": self.use_index,
            "inputs": self.inputs,
            "requires": list(self.requires),
            "expanded_step_ids": list(self.expanded_step_ids),
        }


@dataclass(frozen=True)
class WorkflowComposition:
    steps: tuple[object, ...]
    workflow_inputs: tuple[TypedInputDefinition, ...]
    resolved_workflow_inputs: dict[str, object]
    components: tuple[ComponentUseProvenance, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "version": 1,
            "workflow_inputs": [item.as_dict() for item in self.workflow_inputs],
            "resolved_workflow_inputs": self.resolved_workflow_inputs,
            "components": [item.as_dict() for item in self.components],
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), indent=2, sort_keys=True, ensure_ascii=False)


_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_INPUT_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_COMPONENT_REF = re.compile(r"^component:([a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?)$")
_EXPRESSION = re.compile(r"\$\{\{\s*inputs\.([a-z][a-z0-9_-]{0,63})\s*\}\}")
_EXACT_EXPRESSION = re.compile(r"^\$\{\{\s*inputs\.([a-z][a-z0-9_-]{0,63})\s*\}\}$")
_INPUT_TYPES = frozenset({"string", "integer", "number", "boolean", "string-list"})
_INPUT_KEYS = frozenset({"type", "required", "default", "enum", "sensitive"})
_COMPONENT_SPEC_KEYS = frozenset({"inputs", "requires", "steps"})
_USE_KEYS = frozenset({"uses", "with"})
_COMMON_STEP_KEYS = frozenset(
    {"id", "type", "kind", "description", "if", "condition", "retry", "on_failure"}
)
_STEP_KEYS = {
    "deterministic": _COMMON_STEP_KEYS | {"action"},
    "agent": _COMMON_STEP_KEYS | {"capability", "agent", "mode", "save_as"},
    "approval": _COMMON_STEP_KEYS | {"gate"},
    "quality-gate": _COMMON_STEP_KEYS | {"gate", "quality_gate"},
    "parallel": _COMMON_STEP_KEYS | {"steps"},
    "validate": _COMMON_STEP_KEYS,
}
_FORBIDDEN_COMPONENT_KEYS = frozenset(
    {
        "profile",
        "provider",
        "shell",
        "command",
        "commands",
        "exec",
        "executable",
        "argv",
    }
)


def _fail(code: str, message: str) -> WorkflowComponentError:
    return WorkflowComponentError(f"{code}: {message}")


def _portable(root: Path, path: Path) -> str:
    safe = ensure_within_project(root, path, label="workflow component path")
    return safe.relative_to(root.resolve()).as_posix()


def _validate_value(value: object, definition: TypedInputDefinition, *, label: str) -> object:
    valid = False
    if definition.type == "string":
        valid = isinstance(value, str)
    elif definition.type == "integer":
        valid = isinstance(value, int) and not isinstance(value, bool)
    elif definition.type == "number":
        valid = isinstance(value, (int, float)) and not isinstance(value, bool)
    elif definition.type == "boolean":
        valid = isinstance(value, bool)
    elif definition.type == "string-list":
        valid = isinstance(value, list) and all(isinstance(item, str) for item in value)
    if not valid:
        raise _fail(
            "SDAI-WFCOMP-003",
            f"{label} must be of type '{definition.type}', got {type(value).__name__}",
        )
    normalized: object = tuple(value) if definition.type == "string-list" else value
    if definition.enum and normalized not in definition.enum:
        choices = ", ".join(repr(item) for item in definition.enum)
        raise _fail(
            "SDAI-WFCOMP-003",
            f"{label} must be one of: {choices}",
        )
    return normalized


def _parse_inputs(raw: object, *, label: str) -> tuple[TypedInputDefinition, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, Mapping):
        raise _fail("SDAI-WFCOMP-002", f"{label} inputs must be a mapping")
    result: list[TypedInputDefinition] = []
    for raw_name in sorted(raw):
        name = str(raw_name)
        if not _INPUT_NAME.fullmatch(name):
            raise _fail(
                "SDAI-WFCOMP-002",
                f"{label} input name '{name}' must use lowercase letters, numbers, underscore, or hyphen",
            )
        spec = raw[raw_name]
        if not isinstance(spec, Mapping):
            raise _fail("SDAI-WFCOMP-002", f"{label} input '{name}' must be a mapping")
        unknown = sorted(set(spec) - _INPUT_KEYS)
        if unknown:
            raise _fail(
                "SDAI-WFCOMP-002",
                f"{label} input '{name}' has unknown field(s): {', '.join(map(str, unknown))}",
            )
        input_type = spec.get("type")
        if not isinstance(input_type, str) or input_type not in _INPUT_TYPES:
            raise _fail(
                "SDAI-WFCOMP-002",
                f"{label} input '{name}' type must be one of: {', '.join(sorted(_INPUT_TYPES))}",
            )
        required = spec.get("required", False)
        sensitive = spec.get("sensitive", False)
        if not isinstance(required, bool) or not isinstance(sensitive, bool):
            raise _fail(
                "SDAI-WFCOMP-002",
                f"{label} input '{name}' required/sensitive must be true or false",
            )
        has_default = "default" in spec
        default = spec.get("default")
        raw_enum = spec.get("enum") or []
        if not isinstance(raw_enum, list):
            raise _fail("SDAI-WFCOMP-002", f"{label} input '{name}' enum must be a list")
        preliminary = TypedInputDefinition(
            name=name,
            type=input_type,
            required=required,
            default=None,
            enum=(),
            sensitive=sensitive,
        )
        enum: list[object] = []
        for item in raw_enum:
            enum.append(_validate_value(item, preliminary, label=f"{label} input '{name}' enum value"))
        definition = TypedInputDefinition(
            name=name,
            type=input_type,
            required=required,
            default=None,
            enum=tuple(enum),
            sensitive=sensitive,
        )
        if has_default:
            default = _validate_value(default, definition, label=f"{label} input '{name}' default")
            definition = TypedInputDefinition(
                name=name,
                type=input_type,
                required=required,
                default=default,
                enum=tuple(enum),
                sensitive=sensitive,
            )
        result.append(definition)
    return tuple(result)


def _resolve_inputs(
    definitions: tuple[TypedInputDefinition, ...],
    provided: Mapping[str, object] | None,
    *,
    label: str,
) -> dict[str, object]:
    definitions_by_name = {item.name: item for item in definitions}
    values = dict(provided or {})
    unknown = sorted(set(values) - set(definitions_by_name))
    if unknown:
        raise _fail(
            "SDAI-WFCOMP-003",
            f"{label} received unknown input(s): {', '.join(map(str, unknown))}",
        )
    resolved: dict[str, object] = {}
    for name, definition in definitions_by_name.items():
        if name in values:
            resolved[name] = _validate_value(values[name], definition, label=f"{label} input '{name}'")
        elif definition.default is not None:
            resolved[name] = definition.default
        elif definition.required:
            raise _fail("SDAI-WFCOMP-003", f"{label} requires input '{name}'")
    return resolved


def _interpolate_string(value: str, inputs: Mapping[str, object], *, label: str) -> object:
    exact = _EXACT_EXPRESSION.fullmatch(value)
    if exact:
        name = exact.group(1)
        if name not in inputs:
            raise _fail("SDAI-WFCOMP-004", f"{label} references unresolved input '{name}'")
        value_object = inputs[name]
        return list(value_object) if isinstance(value_object, tuple) else value_object

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in inputs:
            raise _fail("SDAI-WFCOMP-004", f"{label} references unresolved input '{name}'")
        replacement = inputs[name]
        if isinstance(replacement, (tuple, list, dict)):
            raise _fail(
                "SDAI-WFCOMP-004",
                f"{label} cannot embed non-scalar input '{name}' inside a string",
            )
        if isinstance(replacement, bool):
            return "true" if replacement else "false"
        return str(replacement)

    rendered = _EXPRESSION.sub(replace, value)
    if "${{" in rendered or "}}" in rendered:
        raise _fail("SDAI-WFCOMP-004", f"{label} contains malformed or unsupported input expression")
    return rendered


def _interpolate(value: object, inputs: Mapping[str, object], *, label: str) -> object:
    if isinstance(value, str):
        return _interpolate_string(value, inputs, label=label)
    if isinstance(value, list):
        return [
            _interpolate(item, inputs, label=f"{label}[{index}]")
            for index, item in enumerate(value)
        ]
    if isinstance(value, Mapping):
        return {
            str(key): _interpolate(item, inputs, label=f"{label}.{key}")
            for key, item in value.items()
        }
    return value


def _validate_component_step(raw: object, *, component_id: str, index: int) -> None:
    label = f"component '{component_id}' step #{index + 1}"
    if isinstance(raw, str):
        if not _SAFE_NAME.fullmatch(raw.strip()):
            raise _fail("SDAI-WFCOMP-005", f"{label} shorthand step id is invalid")
        return
    if not isinstance(raw, Mapping):
        raise _fail("SDAI-WFCOMP-005", f"{label} must be a string or mapping")
    forbidden = sorted(set(raw) & _FORBIDDEN_COMPONENT_KEYS)
    if forbidden:
        raise _fail(
            "SDAI-WFCOMP-005",
            f"{label} contains forbidden provider/shell field(s): {', '.join(forbidden)}",
        )
    if "uses" in raw:
        raise _fail(
            "SDAI-WFCOMP-005",
            f"{label} cannot nest component uses; declare component dependencies with spec.requires",
        )
    kind = str(raw.get("type") or raw.get("kind") or "").strip()
    allowed = _STEP_KEYS.get(kind)
    if allowed is None:
        raise _fail("SDAI-WFCOMP-005", f"{label} has unsupported step type '{kind}'")
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise _fail(
            "SDAI-WFCOMP-005",
            f"{label} has unsupported field(s): {', '.join(map(str, unknown))}",
        )
    if kind == "parallel":
        children = raw.get("steps")
        if not isinstance(children, list) or not children:
            raise _fail("SDAI-WFCOMP-005", f"{label} parallel steps must be a non-empty list")
        for child_index, child in enumerate(children):
            _validate_component_step(
                child,
                component_id=component_id,
                index=(index * 1000) + child_index,
            )


def _load_component(project_root: Path, component_id: str) -> WorkflowComponentDefinition:
    if not _COMPONENT_REF.fullmatch(f"component:{component_id}"):
        raise _fail("SDAI-WFCOMP-001", f"invalid workflow component id '{component_id}'")
    root = project_root.resolve()
    directory = ensure_within_project(
        root,
        root / ".sdai" / "workflow-components",
        label="workflow component directory",
    )
    path = ensure_within_project(
        root,
        directory / f"{component_id}.yaml",
        label=f"workflow component '{component_id}'",
    )
    if not path.exists():
        raise _fail(
            "SDAI-WFCOMP-001",
            f"workflow component '{component_id}' does not exist at {_portable(root, path)}",
        )
    if path.is_symlink() or not path.is_file():
        raise _fail(
            "SDAI-WFCOMP-001",
            f"workflow component '{component_id}' must be a regular non-symlink file",
        )
    try:
        raw = yaml.safe_load(read_utf8_text(path)) or {}
    except (OSError, TextEncodingError, yaml.YAMLError) as exc:
        raise _fail("SDAI-WFCOMP-001", f"unable to read workflow component '{component_id}': {exc}") from exc
    if not isinstance(raw, Mapping):
        raise _fail("SDAI-WFCOMP-001", f"workflow component '{component_id}' must be a YAML mapping")
    try:
        manifest = parse_extension_manifest(raw, source=_portable(root, path))
    except ExtensionManifestError as exc:
        raise _fail("SDAI-WFCOMP-001", f"invalid workflow component '{component_id}': {exc}") from exc
    if manifest.kind is not ExtensionKind.WORKFLOW_COMPONENT:
        raise _fail(
            "SDAI-WFCOMP-001",
            f"workflow component '{component_id}' kind must be WorkflowComponent",
        )
    if manifest.metadata.id != component_id:
        raise _fail(
            "SDAI-WFCOMP-001",
            f"workflow component filename/id mismatch: requested '{component_id}', manifest id '{manifest.metadata.id}'",
        )
    unknown_spec = sorted(set(manifest.spec) - _COMPONENT_SPEC_KEYS)
    if unknown_spec:
        raise _fail(
            "SDAI-WFCOMP-001",
            f"workflow component '{component_id}' has unsupported spec key(s): {', '.join(unknown_spec)}",
        )
    raw_requires = manifest.spec.get("requires") or []
    if not isinstance(raw_requires, list) or not all(isinstance(item, str) for item in raw_requires):
        raise _fail("SDAI-WFCOMP-001", f"workflow component '{component_id}' requires must be a string list")
    requires = tuple(item.strip() for item in raw_requires)
    if any(not _COMPONENT_REF.fullmatch(f"component:{item}") for item in requires):
        raise _fail("SDAI-WFCOMP-001", f"workflow component '{component_id}' contains invalid required component id")
    if len(requires) != len(set(requires)) or component_id in requires:
        raise _fail("SDAI-WFCOMP-006", f"workflow component '{component_id}' has duplicate/self dependency")
    raw_steps = manifest.spec.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise _fail("SDAI-WFCOMP-001", f"workflow component '{component_id}' steps must be a non-empty list")
    for index, step in enumerate(raw_steps):
        _validate_component_step(step, component_id=component_id, index=index)
    return WorkflowComponentDefinition(
        id=component_id,
        version=manifest.metadata.version,
        description=manifest.metadata.description,
        source=_portable(root, path),
        inputs=_parse_inputs(manifest.spec.get("inputs"), label=f"component '{component_id}'"),
        requires=requires,
        steps=tuple(raw_steps),
    )


def _redacted_inputs(
    definitions: tuple[TypedInputDefinition, ...],
    resolved: Mapping[str, object],
) -> dict[str, object]:
    by_name = {item.name: item for item in definitions}
    return {
        key: ("<redacted>" if by_name[key].sensitive else value)
        for key, value in sorted(resolved.items())
    }


def _step_ids(raw_steps: list[object]) -> tuple[str, ...]:
    result: list[str] = []
    for raw in raw_steps:
        if isinstance(raw, str):
            result.append(raw.strip())
            continue
        if isinstance(raw, Mapping):
            step_id = str(raw.get("id") or "").strip()
            if step_id:
                result.append(step_id)
            children = raw.get("steps") if str(raw.get("type") or raw.get("kind") or "") == "parallel" else None
            if isinstance(children, list):
                result.extend(_step_ids(children))
    return tuple(result)


def _validate_component_dependencies(uses: tuple[ComponentUseProvenance, ...]) -> None:
    used = {item.component_id for item in uses}
    required_by_component = {
        item.component_id: item.requires
        for item in uses
    }
    for component_id, requires in required_by_component.items():
        missing = sorted(set(requires) - used)
        if missing:
            raise _fail(
                "SDAI-WFCOMP-006",
                f"workflow uses component '{component_id}' but is missing required component(s): {', '.join(missing)}",
            )

    visiting: list[str] = []
    visited: set[str] = set()

    def visit(component_id: str) -> None:
        if component_id in visited:
            return
        if component_id in visiting:
            cycle = visiting[visiting.index(component_id) :] + [component_id]
            raise _fail(
                "SDAI-WFCOMP-006",
                "workflow component dependency cycle: " + " -> ".join(cycle),
            )
        visiting.append(component_id)
        for dependency in sorted(required_by_component.get(component_id, ())):
            visit(dependency)
        visiting.pop()
        visited.add(component_id)

    for component_id in sorted(used):
        visit(component_id)


def compose_workflow(
    project_root: Path,
    workflow_data: Mapping[str, object],
    *,
    input_values: Mapping[str, object] | None = None,
) -> WorkflowComposition:
    """Resolve typed workflow inputs and expand repository workflow components.

    Existing workflows with neither ``inputs`` nor ``uses`` are returned unchanged.
    Component expansion happens before the existing workflow step parser, so all
    existing step-kind safety rules continue to apply to expanded steps.
    """

    workflow_name = str(workflow_data.get("name") or "workflow")
    workflow_inputs = _parse_inputs(workflow_data.get("inputs"), label=f"workflow '{workflow_name}'")
    declared_values = workflow_data.get("input_values") or {}
    if not isinstance(declared_values, Mapping):
        raise _fail("SDAI-WFCOMP-002", f"workflow '{workflow_name}' input_values must be a mapping")
    merged_values = dict(declared_values)
    if input_values:
        merged_values.update(input_values)
    resolved_workflow_inputs = _resolve_inputs(
        workflow_inputs,
        merged_values,
        label=f"workflow '{workflow_name}'",
    )

    raw_steps = workflow_data.get("steps") or []
    if not isinstance(raw_steps, list):
        raise _fail("SDAI-WFCOMP-001", f"workflow '{workflow_name}' steps must be a list")

    expanded: list[object] = []
    provenance: list[ComponentUseProvenance] = []
    for index, raw_step in enumerate(raw_steps):
        workflow_resolved = _interpolate(
            raw_step,
            resolved_workflow_inputs,
            label=f"workflow '{workflow_name}' step #{index + 1}",
        )
        if not isinstance(workflow_resolved, Mapping) or "uses" not in workflow_resolved:
            expanded.append(workflow_resolved)
            continue
        unknown = sorted(set(workflow_resolved) - _USE_KEYS)
        if unknown:
            raise _fail(
                "SDAI-WFCOMP-001",
                f"workflow '{workflow_name}' component use #{index + 1} has unsupported field(s): {', '.join(map(str, unknown))}",
            )
        raw_ref = workflow_resolved.get("uses")
        if not isinstance(raw_ref, str):
            raise _fail("SDAI-WFCOMP-001", f"workflow '{workflow_name}' component use #{index + 1} uses must be a string")
        match = _COMPONENT_REF.fullmatch(raw_ref.strip())
        if match is None:
            raise _fail(
                "SDAI-WFCOMP-001",
                f"workflow '{workflow_name}' component use #{index + 1} must use 'component:<id>'",
            )
        component = _load_component(project_root, match.group(1))
        provided = workflow_resolved.get("with") or {}
        if not isinstance(provided, Mapping):
            raise _fail(
                "SDAI-WFCOMP-003",
                f"workflow '{workflow_name}' component '{component.id}' with must be a mapping",
            )
        resolved_component_inputs = _resolve_inputs(
            component.inputs,
            provided,
            label=f"component '{component.id}'",
        )
        component_steps = [
            _interpolate(
                step,
                resolved_component_inputs,
                label=f"component '{component.id}' step #{component_index + 1}",
            )
            for component_index, step in enumerate(component.steps)
        ]
        expanded.extend(component_steps)
        provenance.append(
            ComponentUseProvenance(
                component_id=component.id,
                version=component.version,
                source=component.source,
                use_index=index,
                inputs=_redacted_inputs(component.inputs, resolved_component_inputs),
                requires=component.requires,
                expanded_step_ids=_step_ids(component_steps),
            )
        )

    _validate_component_dependencies(tuple(provenance))
    return WorkflowComposition(
        steps=tuple(expanded),
        workflow_inputs=workflow_inputs,
        resolved_workflow_inputs=_redacted_inputs(workflow_inputs, resolved_workflow_inputs),
        components=tuple(provenance),
    )
