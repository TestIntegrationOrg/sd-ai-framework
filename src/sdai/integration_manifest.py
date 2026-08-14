from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
import json
from pathlib import Path, PurePosixPath
import re
from typing import Mapping
import unicodedata

import yaml
from yaml.constructor import ConstructorError
from yaml.resolver import BaseResolver

from sdai.pack_manifest import PackManifestError, SemVer
from sdai.path_safety import ensure_within_project


INTEGRATION_MANIFEST_API_VERSION = "sdai.integration-manifest/v1"


class IntegrationManifestError(RuntimeError):
    """Raised when a declarative Integration manifest is malformed or unsafe."""


class IntegrationCapability(StrEnum):
    AGENT_EXECUTION = "agent-execution"
    SKILLS = "skills"
    COMMANDS = "commands"
    AGENT_FILES = "agent-files"


class ProjectionKind(StrEnum):
    SKILL = "skill"
    COMMAND = "command"
    AGENT_FILE = "agent-file"


class IntegrationInputMode(StrEnum):
    NONE = "none"
    STDIN = "stdin"
    ARGUMENT = "argument"


class IntegrationOutputMode(StrEnum):
    NONE = "none"
    STDOUT = "stdout"
    JSON_STDOUT = "json-stdout"
    FILE = "file"


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(
    loader: _UniqueKeyLoader,
    node: yaml.nodes.MappingNode,
    deep: bool = False,
) -> dict[object, object]:
    mapping: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as exc:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found an unhashable mapping key",
                key_node.start_mark,
            ) from exc
        if duplicate:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key {key!r}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping)


_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9]*(?:[-_.][a-z0-9]+)*$")
_ENVIRONMENT_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_EXECUTABLE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")
_WINDOWS_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
)
_WINDOWS_FORBIDDEN = frozenset('<>:"|?*')

_MANIFEST_KEYS = frozenset(
    {
        "apiVersion",
        "id",
        "version",
        "displayName",
        "description",
        "capabilities",
        "projections",
        "execution",
        "security",
    }
)
_PROJECTION_KEYS = frozenset({"kind", "source", "target"})
_EXECUTION_KEYS = frozenset(
    {
        "executable",
        "argsBeforeInput",
        "inputMode",
        "argsAfterInput",
        "outputMode",
        "outputPath",
        "timeoutSeconds",
    }
)
_SECURITY_KEYS = frozenset({"requiresNetwork", "requiresWorkspaceWrite", "environment"})

_PROJECTION_CAPABILITY = {
    ProjectionKind.SKILL: IntegrationCapability.SKILLS,
    ProjectionKind.COMMAND: IntegrationCapability.COMMANDS,
    ProjectionKind.AGENT_FILE: IntegrationCapability.AGENT_FILES,
}


def _fail(code: str, message: str) -> IntegrationManifestError:
    return IntegrationManifestError(f"{code}: {message}")


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise _fail("SDAI-INTEGRATION-001", "manifest is not canonical finite JSON") from exc


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _fail(
                "SDAI-INTEGRATION-001",
                f"Integration manifest JSON contains duplicate key '{key}'",
            )
        result[key] = value
    return result


def _mapping(value: object, *, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise _fail("SDAI-INTEGRATION-001", f"{label} must be a string-keyed mapping")
    return value


def _keys(
    value: Mapping[str, object],
    *,
    expected: frozenset[str],
    label: str,
) -> None:
    unknown = sorted(set(value) - expected)
    missing = sorted(expected - set(value))
    if unknown:
        raise _fail(
            "SDAI-INTEGRATION-001",
            f"{label} contains unsupported field(s): {', '.join(unknown)}",
        )
    if missing:
        raise _fail(
            "SDAI-INTEGRATION-001",
            f"{label} is missing required field(s): {', '.join(missing)}",
        )


def _text(value: object, *, label: str, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise _fail("SDAI-INTEGRATION-001", f"{label} must be a string")
    normalized = unicodedata.normalize("NFC", value.strip())
    if not allow_empty and not normalized:
        raise _fail("SDAI-INTEGRATION-001", f"{label} must be a non-empty string")
    if "\x00" in normalized:
        raise _fail("SDAI-INTEGRATION-001", f"{label} must not contain NUL")
    return normalized


def _identifier(value: object, *, label: str) -> str:
    text = _text(value, label=label)
    if not _IDENTIFIER_RE.fullmatch(text):
        raise _fail(
            "SDAI-INTEGRATION-001",
            f"{label} '{text}' is not a portable lowercase identifier",
        )
    return text


def _enum(value: object, enum_type: type[StrEnum], *, label: str) -> StrEnum:
    if not isinstance(value, str):
        raise _fail("SDAI-INTEGRATION-001", f"{label} must be a string")
    try:
        return enum_type(value)
    except ValueError as exc:
        supported = ", ".join(item.value for item in enum_type)
        raise _fail(
            "SDAI-INTEGRATION-001",
            f"unsupported {label} '{value}'; supported values: {supported}",
        ) from exc


def _portable_relative_path(value: object, *, label: str) -> str:
    text = _text(value, label=label)
    if "\\" in text:
        raise _fail(
            "SDAI-INTEGRATION-002",
            f"{label} '{text}' must use portable POSIX '/' separators",
        )
    if re.match(r"^[A-Za-z]:", text):
        raise _fail("SDAI-INTEGRATION-002", f"{label} '{text}' must be project-relative")
    path = PurePosixPath(text)
    if path.is_absolute():
        raise _fail("SDAI-INTEGRATION-002", f"{label} '{text}' must be project-relative")
    parts = text.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise _fail(
            "SDAI-INTEGRATION-002",
            f"{label} '{text}' contains an unsafe path segment",
        )
    for part in parts:
        if any(ord(char) < 32 for char in part):
            raise _fail(
                "SDAI-INTEGRATION-002",
                f"{label} '{text}' contains a control character",
            )
        if any(char in _WINDOWS_FORBIDDEN for char in part):
            raise _fail(
                "SDAI-INTEGRATION-002",
                f"{label} '{text}' is not portable across Windows and POSIX filesystems",
            )
        if part.endswith((" ", ".")):
            raise _fail(
                "SDAI-INTEGRATION-002",
                f"{label} '{text}' contains a non-portable trailing space or dot",
            )
        reserved_stem = part.split(".", 1)[0].upper()
        if reserved_stem in _WINDOWS_RESERVED:
            raise _fail(
                "SDAI-INTEGRATION-002",
                f"{label} '{text}' uses reserved Windows path segment '{part}'",
            )
    return PurePosixPath(*parts).as_posix()


def _executable(value: object) -> str:
    text = _text(value, label="execution.executable")
    if any(char.isspace() for char in text):
        raise _fail(
            "SDAI-INTEGRATION-003",
            "execution.executable must name one executable, not a shell command string",
        )
    if "\\" in text or text.startswith("/") or re.match(r"^[A-Za-z]:", text):
        raise _fail(
            "SDAI-INTEGRATION-003",
            "execution.executable must be a portable command name or project-relative executable path",
        )
    parts = text.split("/")
    if any(part in {"", ".", ".."} or not _EXECUTABLE_SEGMENT_RE.fullmatch(part) for part in parts):
        raise _fail(
            "SDAI-INTEGRATION-003",
            "execution.executable must be a portable command name or project-relative executable path",
        )
    return "/".join(parts)


def _argument(value: object, *, label: str) -> str:
    if not isinstance(value, str) or value == "":
        raise _fail("SDAI-INTEGRATION-003", f"{label} must be a non-empty argv token")
    normalized = unicodedata.normalize("NFC", value)
    if "\x00" in normalized:
        raise _fail("SDAI-INTEGRATION-003", f"{label} must not contain NUL")
    if "{prompt}" in normalized:
        raise _fail(
            "SDAI-INTEGRATION-003",
            f"{label} must not use implicit prompt interpolation; declare inputMode instead",
        )
    return normalized


def _argument_list(value: object, *, label: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise _fail("SDAI-INTEGRATION-003", f"{label} must be a list of argv tokens")
    return tuple(_argument(item, label=f"{label}[{index}]") for index, item in enumerate(value))


def _environment(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise _fail("SDAI-INTEGRATION-004", "security.environment must be a list of variable names")
    names: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not _ENVIRONMENT_RE.fullmatch(item):
            raise _fail(
                "SDAI-INTEGRATION-004",
                f"security.environment[{index}] must be an uppercase portable environment variable name",
            )
        names.append(item)
    if len(set(names)) != len(names):
        raise _fail("SDAI-INTEGRATION-004", "security.environment must not contain duplicates")
    return tuple(sorted(names))


def _bool(value: object, *, label: str) -> bool:
    if not isinstance(value, bool):
        raise _fail("SDAI-INTEGRATION-004", f"{label} must be a boolean")
    return value


def _targets_overlap(left: str, right: str) -> bool:
    left_parts = PurePosixPath(left).parts
    right_parts = PurePosixPath(right).parts
    common = min(len(left_parts), len(right_parts))
    return left_parts[:common] == right_parts[:common]


@dataclass(frozen=True)
class IntegrationProjection:
    kind: ProjectionKind
    source: str
    target: str

    def as_dict(self) -> dict[str, object]:
        return {"kind": self.kind.value, "source": self.source, "target": self.target}

    @classmethod
    def from_dict(cls, value: object) -> "IntegrationProjection":
        raw = _mapping(value, label="integration projection")
        _keys(raw, expected=_PROJECTION_KEYS, label="integration projection")
        return cls(
            kind=_enum(raw["kind"], ProjectionKind, label="projection kind"),  # type: ignore[arg-type]
            source=_portable_relative_path(raw["source"], label="projection source"),
            target=_portable_relative_path(raw["target"], label="projection target"),
        )


@dataclass(frozen=True)
class IntegrationExecution:
    executable: str
    args_before_input: tuple[str, ...]
    input_mode: IntegrationInputMode
    args_after_input: tuple[str, ...]
    output_mode: IntegrationOutputMode
    output_path: str | None
    timeout_seconds: int

    def as_dict(self) -> dict[str, object]:
        return {
            "argsAfterInput": list(self.args_after_input),
            "argsBeforeInput": list(self.args_before_input),
            "executable": self.executable,
            "inputMode": self.input_mode.value,
            "outputMode": self.output_mode.value,
            "outputPath": self.output_path,
            "timeoutSeconds": self.timeout_seconds,
        }

    @classmethod
    def from_dict(cls, value: object) -> "IntegrationExecution":
        raw = _mapping(value, label="integration execution")
        _keys(raw, expected=_EXECUTION_KEYS, label="integration execution")
        input_mode = _enum(raw["inputMode"], IntegrationInputMode, label="input mode")
        output_mode = _enum(raw["outputMode"], IntegrationOutputMode, label="output mode")
        timeout_seconds = raw["timeoutSeconds"]
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int):
            raise _fail("SDAI-INTEGRATION-003", "execution.timeoutSeconds must be an integer")
        if timeout_seconds < 1 or timeout_seconds > 86400:
            raise _fail(
                "SDAI-INTEGRATION-003",
                "execution.timeoutSeconds must be between 1 and 86400",
            )
        output_path_raw = raw["outputPath"]
        if output_mode == IntegrationOutputMode.FILE:
            output_path = _portable_relative_path(output_path_raw, label="execution.outputPath")
        else:
            if output_path_raw is not None:
                raise _fail(
                    "SDAI-INTEGRATION-003",
                    "execution.outputPath must be null unless outputMode is 'file'",
                )
            output_path = None
        return cls(
            executable=_executable(raw["executable"]),
            args_before_input=_argument_list(raw["argsBeforeInput"], label="execution.argsBeforeInput"),
            input_mode=input_mode,  # type: ignore[arg-type]
            args_after_input=_argument_list(raw["argsAfterInput"], label="execution.argsAfterInput"),
            output_mode=output_mode,  # type: ignore[arg-type]
            output_path=output_path,
            timeout_seconds=timeout_seconds,
        )


@dataclass(frozen=True)
class IntegrationSecurity:
    requires_network: bool
    requires_workspace_write: bool
    environment: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "environment": list(self.environment),
            "requiresNetwork": self.requires_network,
            "requiresWorkspaceWrite": self.requires_workspace_write,
        }

    @classmethod
    def from_dict(cls, value: object) -> "IntegrationSecurity":
        raw = _mapping(value, label="integration security")
        _keys(raw, expected=_SECURITY_KEYS, label="integration security")
        return cls(
            requires_network=_bool(raw["requiresNetwork"], label="security.requiresNetwork"),
            requires_workspace_write=_bool(
                raw["requiresWorkspaceWrite"], label="security.requiresWorkspaceWrite"
            ),
            environment=_environment(raw["environment"]),
        )


@dataclass(frozen=True)
class IntegrationManifest:
    id: str
    version: SemVer
    display_name: str
    description: str
    capabilities: tuple[IntegrationCapability, ...]
    projections: tuple[IntegrationProjection, ...]
    execution: IntegrationExecution | None
    security: IntegrationSecurity

    @property
    def identity(self) -> str:
        return f"{self.id}@{self.version}"

    def as_dict(self) -> dict[str, object]:
        return {
            "apiVersion": INTEGRATION_MANIFEST_API_VERSION,
            "capabilities": [item.value for item in self.capabilities],
            "description": self.description,
            "displayName": self.display_name,
            "execution": None if self.execution is None else self.execution.as_dict(),
            "id": self.id,
            "projections": [item.as_dict() for item in self.projections],
            "security": self.security.as_dict(),
            "version": str(self.version),
        }

    def to_json(self) -> str:
        return _canonical_json(self.as_dict())

    def to_text(self) -> str:
        return self.to_json() + "\n"

    @property
    def sha256(self) -> str:
        return "sha256:" + sha256(self.to_json().encode("utf-8")).hexdigest()

    @classmethod
    def from_dict(cls, value: object) -> "IntegrationManifest":
        raw = _mapping(value, label="Integration manifest")
        _keys(raw, expected=_MANIFEST_KEYS, label="Integration manifest")
        if raw["apiVersion"] != INTEGRATION_MANIFEST_API_VERSION:
            raise _fail(
                "SDAI-INTEGRATION-001",
                f"unsupported apiVersion '{raw['apiVersion']}', expected '{INTEGRATION_MANIFEST_API_VERSION}'",
            )

        capabilities_raw = raw["capabilities"]
        if not isinstance(capabilities_raw, list) or not capabilities_raw:
            raise _fail("SDAI-INTEGRATION-001", "capabilities must be a non-empty list")
        capabilities: list[IntegrationCapability] = []
        for index, item in enumerate(capabilities_raw):
            capability = _enum(item, IntegrationCapability, label=f"capabilities[{index}]")
            capabilities.append(capability)  # type: ignore[arg-type]
        if len(set(capabilities)) != len(capabilities):
            raise _fail("SDAI-INTEGRATION-001", "capabilities must not contain duplicates")
        capabilities_tuple = tuple(sorted(capabilities, key=lambda item: item.value))

        projections_raw = raw["projections"]
        if not isinstance(projections_raw, list):
            raise _fail("SDAI-INTEGRATION-001", "projections must be a list")
        projections = tuple(
            sorted(
                (IntegrationProjection.from_dict(item) for item in projections_raw),
                key=lambda item: (item.kind.value, item.target, item.source),
            )
        )
        targets = [item.target for item in projections]
        if len(set(targets)) != len(targets):
            raise _fail("SDAI-INTEGRATION-002", "projection targets must be unique")
        for index, left in enumerate(projections):
            for right in projections[index + 1 :]:
                if _targets_overlap(left.target, right.target):
                    raise _fail(
                        "SDAI-INTEGRATION-002",
                        f"projection targets '{left.target}' and '{right.target}' overlap",
                    )
        capability_set = set(capabilities_tuple)
        for projection in projections:
            required = _PROJECTION_CAPABILITY[projection.kind]
            if required not in capability_set:
                raise _fail(
                    "SDAI-INTEGRATION-001",
                    f"projection kind '{projection.kind.value}' requires capability '{required.value}'",
                )
        for kind, capability in _PROJECTION_CAPABILITY.items():
            if capability in capability_set and not any(item.kind == kind for item in projections):
                raise _fail(
                    "SDAI-INTEGRATION-001",
                    f"capability '{capability.value}' requires at least one '{kind.value}' projection",
                )

        execution_raw = raw["execution"]
        execution = None if execution_raw is None else IntegrationExecution.from_dict(execution_raw)
        has_execution = IntegrationCapability.AGENT_EXECUTION in capability_set
        if has_execution != (execution is not None):
            raise _fail(
                "SDAI-INTEGRATION-003",
                "capability 'agent-execution' and execution declaration must either both be present or both be absent",
            )

        security = IntegrationSecurity.from_dict(raw["security"])
        if execution is not None and execution.output_mode == IntegrationOutputMode.FILE and not security.requires_workspace_write:
            raise _fail(
                "SDAI-INTEGRATION-004",
                "file output requires security.requiresWorkspaceWrite=true",
            )

        try:
            version = SemVer.parse(raw["version"])
        except PackManifestError as exc:
            raise _fail("SDAI-INTEGRATION-001", "version must be a valid semantic version") from exc

        manifest = cls(
            id=_identifier(raw["id"], label="integration id"),
            version=version,
            display_name=_text(raw["displayName"], label="displayName"),
            description=_text(raw["description"], label="description", allow_empty=True),
            capabilities=capabilities_tuple,
            projections=projections,
            execution=execution,
            security=security,
        )
        _canonical_json(manifest.as_dict())
        return manifest

    @classmethod
    def from_json(cls, value: str) -> "IntegrationManifest":
        try:
            raw = json.loads(value, object_pairs_hook=_unique_json_object)
        except json.JSONDecodeError as exc:
            raise _fail("SDAI-INTEGRATION-001", "Integration manifest JSON is malformed") from exc
        return cls.from_dict(raw)

    @classmethod
    def from_yaml(cls, value: str) -> "IntegrationManifest":
        try:
            raw = yaml.load(value, Loader=_UniqueKeyLoader)
        except yaml.YAMLError as exc:
            raise _fail("SDAI-INTEGRATION-001", "Integration manifest YAML is malformed") from exc
        return cls.from_dict(raw)


def load_integration_manifest(path: Path, *, root: Path | None = None) -> IntegrationManifest:
    """Load one UTF-8 Integration manifest without following symlinked manifest paths."""

    cwd = Path.cwd()
    root_input = root if root is not None else (path.parent if path.is_absolute() else cwd / path.parent)
    if not root_input.is_absolute():
        root_input = cwd / root_input
    manifest_input = path if path.is_absolute() else ((root_input / path) if root is not None else cwd / path)

    if root_input.is_symlink():
        raise _fail("SDAI-INTEGRATION-002", "Integration manifest root must not be a symlink")
    if manifest_input.is_symlink():
        raise _fail("SDAI-INTEGRATION-002", "Integration manifest must not be a symlink")
    if not manifest_input.is_file():
        raise _fail(
            "SDAI-INTEGRATION-001",
            f"Integration manifest '{manifest_input}' does not exist",
        )

    root_resolved = root_input.resolve()
    try:
        safe_manifest = ensure_within_project(
            root_resolved,
            manifest_input,
            label="Integration manifest",
        )
    except RuntimeError as exc:
        raise _fail("SDAI-INTEGRATION-002", "Integration manifest escapes its root") from exc

    relative = safe_manifest.resolve(strict=False).relative_to(root_resolved)
    current = root_resolved
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise _fail(
                "SDAI-INTEGRATION-002",
                "Integration manifest path must not contain symlink components",
            )

    try:
        text = safe_manifest.read_text(encoding="utf-8", errors="strict")
    except (OSError, UnicodeError) as exc:
        raise _fail(
            "SDAI-INTEGRATION-001",
            f"unable to read Integration manifest '{safe_manifest}' as UTF-8",
        ) from exc
    return IntegrationManifest.from_yaml(text)
