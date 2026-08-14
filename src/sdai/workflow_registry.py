from __future__ import annotations

from dataclasses import dataclass
from functools import cmp_to_key
from hashlib import sha256
import json
from pathlib import Path, PurePosixPath
import re
from typing import Mapping
import unicodedata

from sdai.config import ConfigError, load_yaml
from sdai.extensions.registry import RegistryLayer
from sdai.pack_manifest import PackManifestError, SemVer
from sdai.workflow_graph import WorkflowGraphError, WorkflowGraphResolution, load_workflow_graph


WORKFLOW_REGISTRY_API_VERSION = "sdai.workflow-registry/v2"
WORKFLOW_REGISTRY_RESOLUTION_API_VERSION = "sdai.workflow-registry-resolution/v2"
LEGACY_WORKFLOW_REGISTRY_VERSION = "0.0.0"


class WorkflowRegistryError(RuntimeError):
    """Raised when layered workflow discovery or resolution is ambiguous or unsafe."""


_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_LOCKABLE_LAYERS = frozenset({RegistryLayer.BUILTIN, RegistryLayer.ORG})
_WINDOWS_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
)
_WINDOWS_FORBIDDEN = frozenset('<>:"|?*')


def _fail(code: str, message: str) -> WorkflowRegistryError:
    return WorkflowRegistryError(f"{code}: {message}")


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise _fail("SDAI-WF2-REG-001", "workflow registry data is not canonical finite JSON") from exc


def _normalize_json(value: object, *, label: str) -> object:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if value != value or value in {float("inf"), float("-inf")}:
            raise _fail("SDAI-WF2-REG-001", f"{label} contains a non-finite number")
        return value
    if isinstance(value, list):
        return [_normalize_json(item, label=f"{label}[]") for item in value]
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise _fail("SDAI-WF2-REG-001", f"{label} mapping keys must be strings")
        return {key: _normalize_json(value[key], label=f"{label}.{key}") for key in sorted(value)}
    raise _fail("SDAI-WF2-REG-001", f"{label} must contain JSON-compatible values")


def _hash_json(value: object) -> str:
    return "sha256:" + sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _valid_sha(value: object) -> str:
    if not isinstance(value, str) or not value.startswith("sha256:"):
        raise _fail("SDAI-WF2-REG-001", "workflow provenance hash must be SHA-256")
    digest = value.removeprefix("sha256:")
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise _fail("SDAI-WF2-REG-001", "workflow provenance hash must be lowercase SHA-256")
    return value


def _layer(value: RegistryLayer) -> RegistryLayer:
    try:
        return RegistryLayer(value)
    except ValueError as exc:
        raise _fail("SDAI-WF2-REG-001", f"unknown workflow registry layer {value!r}") from exc


def _name(value: object, *, label: str) -> str:
    if not isinstance(value, str) or value != value.strip() or not _NAME.fullmatch(value):
        raise _fail("SDAI-WF2-REG-001", f"{label} is not a portable workflow identifier")
    return value


def _source_label(value: object) -> str:
    if not isinstance(value, str):
        raise _fail("SDAI-WF2-REG-001", "workflow source label must be a string")
    text = unicodedata.normalize("NFC", value.strip())
    if not text or "\x00" in text:
        raise _fail("SDAI-WF2-REG-001", "workflow source label is invalid")
    return text


def _portable_relative(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or "\\" in value or "\x00" in value:
        raise _fail("SDAI-WF2-REG-001", f"{label} must be a portable relative path")
    normalized = unicodedata.normalize("NFC", value)
    path = PurePosixPath(normalized)
    parts = normalized.split("/")
    if path.is_absolute() or any(part in {"", ".", ".."} for part in parts):
        raise _fail("SDAI-WF2-REG-001", f"{label} must be a portable relative path")
    for part in parts:
        if part != part.strip() or any(ord(char) < 32 for char in part):
            raise _fail("SDAI-WF2-REG-001", f"{label} contains a non-portable segment")
        if any(char in _WINDOWS_FORBIDDEN for char in part) or part.endswith((".", " ")):
            raise _fail("SDAI-WF2-REG-001", f"{label} is not portable across Windows/Linux")
        if part.split(".", 1)[0].upper() in _WINDOWS_RESERVED:
            raise _fail("SDAI-WF2-REG-001", f"{label} uses a reserved Windows segment")
    return path.as_posix()


def _version(value: object) -> SemVer:
    text = LEGACY_WORKFLOW_REGISTRY_VERSION if value is None else value
    if not isinstance(text, str):
        raise _fail("SDAI-WF2-REG-001", "registry_version must be a semantic-version string")
    try:
        return SemVer.parse(text)
    except PackManifestError as exc:
        raise _fail("SDAI-WF2-REG-001", f"invalid workflow registry_version {text!r}") from exc


def _version_compare(left: SemVer, right: SemVer) -> int:
    precedence = left.compare_precedence(right)
    if precedence:
        return precedence
    if str(left) == str(right):
        return 0
    return -1 if str(left) < str(right) else 1


def _fs_key(path: Path) -> tuple[str, str]:
    value = str(path).replace("\\", "/")
    return value.casefold(), value


@dataclass(frozen=True)
class WorkflowSource:
    """A project-shaped discovery root containing `.sdai/workflows/*.yaml`."""

    project_root: Path
    layer: RegistryLayer
    source: str
    locked: bool = False

    def __post_init__(self) -> None:
        layer = _layer(self.layer)
        if not isinstance(self.locked, bool):
            raise _fail("SDAI-WF2-REG-001", "workflow source locked must be boolean")
        if self.locked and layer not in _LOCKABLE_LAYERS:
            raise _fail("SDAI-WF2-REG-005", "only builtin/org workflow sources may be authoritative locks")
        object.__setattr__(self, "project_root", Path(self.project_root))
        object.__setattr__(self, "layer", layer)
        object.__setattr__(self, "source", _source_label(self.source))


@dataclass(frozen=True)
class WorkflowSourceProvenance:
    layer: RegistryLayer
    source: str
    path: str
    source_sha256: str
    locked: bool

    def __post_init__(self) -> None:
        layer = _layer(self.layer)
        if not isinstance(self.locked, bool):
            raise _fail("SDAI-WF2-REG-001", "workflow provenance locked must be boolean")
        if self.locked and layer not in _LOCKABLE_LAYERS:
            raise _fail("SDAI-WF2-REG-005", "workflow provenance lock is not authoritative")
        object.__setattr__(self, "layer", layer)
        object.__setattr__(self, "source", _source_label(self.source))
        object.__setattr__(self, "path", _portable_relative(self.path, label="workflow provenance path"))
        object.__setattr__(self, "source_sha256", _valid_sha(self.source_sha256))

    def as_dict(self) -> dict[str, object]:
        return {
            "layer": self.layer.value,
            "locked": self.locked,
            "path": self.path,
            "source": self.source,
            "sourceSha256": self.source_sha256,
        }


@dataclass(frozen=True)
class WorkflowRegistration:
    name: str
    registry_version: SemVer
    engine_version: int | None
    source_data_json: str
    source_sha256: str
    graph_resolution: WorkflowGraphResolution
    provenance: WorkflowSourceProvenance

    @property
    def identity(self) -> str:
        return f"{self.name}@{self.registry_version}"


@dataclass(frozen=True)
class ResolvedWorkflow:
    registration: WorkflowRegistration
    selected_provenance: WorkflowSourceProvenance
    provenance: tuple[WorkflowSourceProvenance, ...]

    @property
    def name(self) -> str:
        return self.registration.name

    @property
    def registry_version(self) -> SemVer:
        return self.registration.registry_version

    @property
    def identity(self) -> str:
        return self.registration.identity

    @property
    def source_sha256(self) -> str:
        return self.registration.source_sha256

    @property
    def graph_sha256(self) -> str:
        return self.registration.graph_resolution.graph.sha256

    @property
    def resolution_sha256(self) -> str:
        return self.registration.graph_resolution.sha256

    def as_dict(self) -> dict[str, object]:
        return {
            "apiVersion": WORKFLOW_REGISTRY_RESOLUTION_API_VERSION,
            "graphResolution": self.registration.graph_resolution.as_dict(),
            "graphResolutionSha256": self.resolution_sha256,
            "graphSha256": self.graph_sha256,
            "identity": self.identity,
            "provenance": [item.as_dict() for item in self.provenance],
            "registryVersion": str(self.registry_version),
            "selectedProvenance": self.selected_provenance.as_dict(),
            "sourceSha256": self.source_sha256,
            "workflowName": self.name,
        }

    def to_json(self) -> str:
        return _canonical_json(self.as_dict())


class WorkflowRegistry:
    """Deterministic, version-aware registry for project-shaped workflow sources.

    `version:` in existing workflow YAML remains the Workflow Engine schema version.
    Optional `registry_version:` is the semantic registry identity; legacy files omit it
    and resolve as `0.0.0`. Exact `name@registry_version` content and canonical graph are
    immutable across layers. Builtin/org locks block higher-layer definitions of the name.
    """

    def __init__(self) -> None:
        self._entries: dict[str, dict[str, dict[RegistryLayer, WorkflowRegistration]]] = {}

    def _copy(self) -> dict[str, dict[str, dict[RegistryLayer, WorkflowRegistration]]]:
        return {
            name: {version: dict(layers) for version, layers in versions.items()}
            for name, versions in self._entries.items()
        }

    def register(self, registration: WorkflowRegistration) -> None:
        name = registration.name
        version = str(registration.registry_version)
        entries = self._copy()
        versions = entries.setdefault(name, {})

        for layers in versions.values():
            for layer, existing in layers.items():
                if existing.provenance.locked and registration.provenance.layer.priority > layer.priority:
                    raise _fail(
                        "SDAI-WF2-REG-005",
                        f"workflow '{name}' is locked by {layer.value}:{existing.provenance.source}; "
                        f"higher layer {registration.provenance.layer.value} cannot define {name}@{version}",
                    )
                if registration.provenance.locked and layer.priority > registration.provenance.layer.priority:
                    raise _fail(
                        "SDAI-WF2-REG-005",
                        f"cannot add authoritative lock for workflow '{name}' after higher-layer definition "
                        f"{layer.value}:{existing.provenance.source}",
                    )

        layers = versions.setdefault(version, {})
        if registration.provenance.layer in layers:
            existing = layers[registration.provenance.layer]
            raise _fail(
                "SDAI-WF2-REG-003",
                f"duplicate workflow '{name}@{version}' in layer {registration.provenance.layer.value}: "
                f"{existing.provenance.source} and {registration.provenance.source}",
            )
        if layers:
            source_hashes = {item.source_sha256 for item in layers.values()}
            graph_hashes = {item.graph_resolution.sha256 for item in layers.values()}
            if registration.source_sha256 not in source_hashes:
                raise _fail(
                    "SDAI-WF2-REG-004",
                    f"conflicting exact workflow '{name}@{version}' has different canonical source content",
                )
            if registration.graph_resolution.sha256 not in graph_hashes:
                raise _fail(
                    "SDAI-WF2-REG-004",
                    f"conflicting exact workflow '{name}@{version}' resolves to a different canonical graph",
                )
        layers[registration.provenance.layer] = registration
        self._entries = entries

    def _locked_layer(self, name: str) -> RegistryLayer | None:
        locked = [
            registration.provenance.layer
            for version_layers in self._entries.get(name, {}).values()
            for registration in version_layers.values()
            if registration.provenance.locked
        ]
        return max(locked, key=lambda item: item.priority) if locked else None

    def _resolved_exact(self, name: str, version: str) -> ResolvedWorkflow:
        layers = self._entries.get(name, {}).get(version)
        if not layers:
            raise _fail("SDAI-WF2-REG-006", f"workflow '{name}@{version}' was not found")
        locked_layer = self._locked_layer(name)
        eligible = {
            layer: item
            for layer, item in layers.items()
            if locked_layer is None or layer.priority <= locked_layer.priority
        }
        if not eligible:
            raise _fail("SDAI-WF2-REG-005", f"workflow '{name}@{version}' is hidden by an authoritative lock")
        selected_layer = max(eligible, key=lambda item: item.priority)
        selected = eligible[selected_layer]
        provenance = tuple(
            sorted((item.provenance for item in eligible.values()), key=lambda item: (item.layer.priority, item.source, item.path))
        )
        return ResolvedWorkflow(selected, selected.provenance, provenance)

    def resolve(self, reference: str) -> ResolvedWorkflow:
        if not isinstance(reference, str) or not reference.strip():
            raise _fail("SDAI-WF2-REG-001", "workflow reference must be non-empty")
        text = reference.strip()
        if "@" in text:
            name_text, version_text = text.rsplit("@", 1)
            name = _name(name_text, label="workflow name")
            return self._resolved_exact(name, str(_version(version_text)))

        name = _name(text, label="workflow name")
        versions = self._entries.get(name, {})
        if not versions:
            raise _fail("SDAI-WF2-REG-006", f"workflow '{name}' was not found")
        locked_layer = self._locked_layer(name)
        candidates: list[SemVer] = []
        for version_text, layers in versions.items():
            if any(locked_layer is None or layer.priority <= locked_layer.priority for layer in layers):
                candidates.append(_version(version_text))
        if not candidates:
            raise _fail("SDAI-WF2-REG-005", f"workflow '{name}' has no version permitted by its authoritative lock")
        candidates.sort(key=cmp_to_key(_version_compare), reverse=True)
        top = candidates[0]
        equal_precedence = [item for item in candidates if item.compare_precedence(top) == 0]
        if len({str(item) for item in equal_precedence}) > 1:
            values = ", ".join(sorted(str(item) for item in equal_precedence))
            raise _fail("SDAI-WF2-REG-004", f"workflow '{name}' has ambiguous latest SemVer build variants: {values}")
        return self._resolved_exact(name, str(top))

    def list(self) -> tuple[ResolvedWorkflow, ...]:
        return tuple(self.resolve(name) for name in sorted(self._entries))

    def search(self, query: str) -> tuple[ResolvedWorkflow, ...]:
        text = query.strip().casefold()
        if not text:
            return self.list()
        return tuple(item for item in self.list() if text in item.name.casefold() or text in item.identity.casefold())

    def info(self, name: str) -> tuple[ResolvedWorkflow, ...]:
        name = _name(name, label="workflow name")
        versions = self._entries.get(name, {})
        ordered = sorted((_version(value) for value in versions), key=cmp_to_key(_version_compare), reverse=True)
        return tuple(self._resolved_exact(name, str(version)) for version in ordered)

    def as_dict(self) -> dict[str, object]:
        return {
            "apiVersion": WORKFLOW_REGISTRY_API_VERSION,
            "workflows": [item.as_dict() for item in self.list()],
        }

    def to_json(self) -> str:
        return _canonical_json(self.as_dict())


def _workflow_directory(root: Path) -> Path:
    return root / ".sdai" / "workflows"


def _workflow_files(root: Path) -> tuple[Path, ...]:
    directory = _workflow_directory(root)
    if not directory.exists():
        return ()
    if directory.is_symlink() or not directory.is_dir():
        raise _fail("SDAI-WF2-REG-002", ".sdai/workflows must be a regular directory, not a symlink")
    files: list[Path] = []
    for path in directory.iterdir():
        if path.is_symlink():
            raise _fail("SDAI-WF2-REG-002", f"workflow source '{path.name}' must not be a symlink")
        if path.is_file() and path.suffix.casefold() in {".yaml", ".yml"}:
            files.append(path)
    return tuple(sorted(files, key=_fs_key))


def _registration(source: WorkflowSource, path: Path) -> WorkflowRegistration:
    root = source.project_root.resolve()
    if source.project_root.is_symlink() or not root.is_dir():
        raise _fail("SDAI-WF2-REG-002", f"workflow source root '{source.source}' must be an existing non-symlink directory")
    try:
        relative = path.resolve().relative_to(root).as_posix()
    except ValueError as exc:
        raise _fail("SDAI-WF2-REG-002", "workflow source escaped its source root") from exc
    relative = _portable_relative(relative, label="workflow provenance path")
    try:
        raw = load_yaml(path)
    except (ConfigError, OSError, UnicodeError) as exc:
        raise _fail("SDAI-WF2-REG-002", f"unable to load workflow source '{relative}'") from exc
    normalized = _normalize_json(raw, label=f"workflow '{relative}'")
    assert isinstance(normalized, dict)
    filename_name = path.stem
    name = _name(raw.get("name", filename_name), label=f"workflow '{relative}' name")
    if name != filename_name:
        raise _fail("SDAI-WF2-REG-002", f"workflow filename/name mismatch: '{filename_name}' != '{name}'")
    registry_version = _version(raw.get("registry_version"))
    raw_engine = raw.get("version")
    if raw_engine is not None and (isinstance(raw_engine, bool) or not isinstance(raw_engine, int)):
        raise _fail("SDAI-WF2-REG-002", f"workflow '{name}' engine version must be an integer")
    try:
        graph_resolution = load_workflow_graph(root, name, environ={})
    except (WorkflowGraphError, FileNotFoundError) as exc:
        raise _fail("SDAI-WF2-REG-002", f"workflow '{name}' cannot resolve to a canonical graph: {exc}") from exc
    source_sha = _hash_json(normalized)
    return WorkflowRegistration(
        name=name,
        registry_version=registry_version,
        engine_version=raw_engine,
        source_data_json=_canonical_json(normalized),
        source_sha256=source_sha,
        graph_resolution=graph_resolution,
        provenance=WorkflowSourceProvenance(
            layer=source.layer,
            source=source.source,
            path=relative,
            source_sha256=source_sha,
            locked=source.locked,
        ),
    )


def build_workflow_registry(sources: tuple[WorkflowSource, ...] | list[WorkflowSource]) -> WorkflowRegistry:
    registry = WorkflowRegistry()
    ordered_sources = sorted(
        sources,
        key=lambda item: (item.layer.priority, item.source, str(item.project_root).replace("\\", "/").casefold()),
    )
    for source in ordered_sources:
        staged = [_registration(source, path) for path in _workflow_files(source.project_root.resolve())]
        snapshot = registry._copy()
        try:
            for registration in staged:
                registry.register(registration)
        except WorkflowRegistryError:
            registry._entries = snapshot
            raise
    return registry
