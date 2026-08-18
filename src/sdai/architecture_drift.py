from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
import json
import math
from pathlib import Path, PurePosixPath
import re
from types import MappingProxyType
from typing import Iterable, Mapping, Protocol

import yaml
from yaml.constructor import ConstructorError
from yaml.resolver import BaseResolver

from sdai.models import validate_feature_id
from sdai.path_safety import PathSafetyError, ensure_within_project
from sdai.trace_evidence import (
    EvidenceBindingKind,
    EvidenceKind,
    EvidenceStatus,
    TraceEvidence,
    TraceEvidenceError,
    load_trace_evidence,
)
from sdai.trace_freshness import (
    EvidenceFreshnessReport,
    ProofFreshness,
    TraceFreshnessError,
    evaluate_trace_evidence_freshness,
)
from sdai.trace_graph import TraceProvenance


ARCHITECTURE_TOPOLOGY_API_VERSION = "sdai.architecture-topology/v1"
ARCHITECTURE_OBSERVATION_API_VERSION = "sdai.architecture-observation/v1"
ARCHITECTURE_DRIFT_API_VERSION = "sdai.architecture-drift/v1"
ARCHITECTURE_TOPOLOGY_FILENAME = "architecture/approved-topology.yaml"
ARCHITECTURE_TOPOLOGY_MAX_BYTES = 4 * 1024 * 1024
ARCHITECTURE_MAX_COMPONENTS = 4096
ARCHITECTURE_MAX_FACTS = 100_000
ARCHITECTURE_MAX_JSON_ITEMS = 500_000
ARCHITECTURE_MAX_JSON_DEPTH = 32

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+\-]{0,255}$")
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_WINDOWS_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
)
_WINDOWS_FORBIDDEN = frozenset('<>:"|?*')


class ArchitectureDriftError(RuntimeError):
    """Raised when architecture truth, evidence, observation, or drift is unsafe/ambiguous."""


class ArchitectureFactKind(StrEnum):
    DEPENDENCY = "dependency"
    COMMUNICATION = "communication"
    DATA_OWNERSHIP = "data-ownership"
    DATA_ACCESS = "data-access"
    TRUST_BOUNDARY = "trust-boundary"
    DEPLOYMENT = "deployment"
    CONTRACT = "contract"


class ArchitectureFactMode(StrEnum):
    REQUIRED = "required"
    ALLOWED = "allowed"
    FORBIDDEN = "forbidden"


class ArchitectureDriftSeverity(StrEnum):
    WARNING = "warning"
    ERROR = "error"


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
                "found a duplicate mapping key",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping)


def _fail(code: str, message: str) -> ArchitectureDriftError:
    return ArchitectureDriftError(f"{code}: {message}")


def _validate_json(
    value: object,
    *,
    label: str,
    depth: int = 0,
    counter: list[int] | None = None,
) -> None:
    if counter is None:
        counter = [0]
    counter[0] += 1
    if counter[0] > ARCHITECTURE_MAX_JSON_ITEMS:
        raise _fail("SDAI-ARCH-DRIFT-001", f"{label} exceeds the finite JSON item limit")
    if depth > ARCHITECTURE_MAX_JSON_DEPTH:
        raise _fail("SDAI-ARCH-DRIFT-001", f"{label} exceeds the finite JSON nesting limit")
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise _fail("SDAI-ARCH-DRIFT-001", f"{label} contains a non-finite number")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _validate_json(item, label=f"{label}[{index}]", depth=depth + 1, counter=counter)
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str) or not key:
                raise _fail("SDAI-ARCH-DRIFT-001", f"{label} keys must be non-empty strings")
            _validate_json(item, label=f"{label}.{key}", depth=depth + 1, counter=counter)
        return
    raise _fail(
        "SDAI-ARCH-DRIFT-001",
        f"{label} contains unsupported JSON type {type(value).__name__}",
    )


def _canonical_json(value: object) -> str:
    _validate_json(value, label="architecture data")
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise _fail("SDAI-ARCH-DRIFT-001", f"architecture data is not canonical JSON: {exc}") from exc


def _hash_json(value: object) -> str:
    return "sha256:" + sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _hash_bytes(value: bytes) -> str:
    return "sha256:" + sha256(value).hexdigest()


def _freeze_json(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze_json(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze_json(item) for item in value)
    return value


def _thaw_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _json_mapping(value: object, *, label: str) -> Mapping[str, object]:
    if value is None:
        return MappingProxyType({})
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise _fail("SDAI-ARCH-DRIFT-002", f"{label} must be a string-keyed mapping")
    _validate_json(value, label=label)
    clone = json.loads(_canonical_json(dict(value)))
    frozen = _freeze_json(clone)
    assert isinstance(frozen, Mapping)
    return frozen


def _identifier(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value) or "\\" in value:
        raise _fail("SDAI-ARCH-DRIFT-002", f"{label} is not a safe portable identifier: {value!r}")
    return value


def _portable_relative_path(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise _fail("SDAI-ARCH-DRIFT-003", f"{label} must be a non-empty repository-relative POSIX path")
    if "\\" in value or "\x00" in value or re.match(r"^[A-Za-z]:", value):
        raise _fail("SDAI-ARCH-DRIFT-003", f"{label} must be a repository-relative POSIX path")
    path = PurePosixPath(value)
    parts = value.split("/")
    if path.is_absolute() or any(part in {"", ".", ".."} for part in parts):
        raise _fail("SDAI-ARCH-DRIFT-003", f"{label} is unsafe: {value!r}")
    if len(value.encode("utf-8")) > 4096 or len(parts) > 64:
        raise _fail("SDAI-ARCH-DRIFT-003", f"{label} exceeds portable path limits")
    for part in parts:
        if len(part.encode("utf-8")) > 255 or part != part.strip():
            raise _fail("SDAI-ARCH-DRIFT-003", f"{label} contains a non-portable segment")
        if any(ord(char) < 32 for char in part):
            raise _fail("SDAI-ARCH-DRIFT-003", f"{label} contains a control character")
        if any(char in _WINDOWS_FORBIDDEN for char in part) or part.endswith((".", " ")):
            raise _fail("SDAI-ARCH-DRIFT-003", f"{label} is not portable across Windows/Linux")
        if part.split(".", 1)[0].upper() in _WINDOWS_RESERVED:
            raise _fail("SDAI-ARCH-DRIFT-003", f"{label} uses a reserved Windows path segment")
    return path.as_posix()


def _safe_existing_file(root: Path, relative: str, *, label: str) -> Path:
    candidate = root / relative
    try:
        safe = ensure_within_project(root, candidate, label=label)
    except PathSafetyError as exc:
        raise _fail("SDAI-ARCH-DRIFT-003", f"{label} must remain inside the project root") from exc
    current = root
    try:
        parts = safe.relative_to(root).parts
    except ValueError as exc:
        raise _fail("SDAI-ARCH-DRIFT-003", f"{label} must remain inside the project root") from exc
    for part in parts:
        current = current / part
        if current.exists() and current.is_symlink():
            raise _fail("SDAI-ARCH-DRIFT-003", f"{label} must not traverse symbolic links: {relative}")
    if safe.is_symlink() or not safe.is_file():
        raise _fail("SDAI-ARCH-DRIFT-003", f"{label} must be a regular file: {relative}")
    return safe


def _read_bounded(path: Path, *, label: str) -> bytes:
    try:
        with path.open("rb") as stream:
            data = stream.read(ARCHITECTURE_TOPOLOGY_MAX_BYTES + 1)
    except OSError as exc:
        raise _fail("SDAI-ARCH-DRIFT-003", f"unable to read {label}: {path}") from exc
    if len(data) > ARCHITECTURE_TOPOLOGY_MAX_BYTES:
        raise _fail(
            "SDAI-ARCH-DRIFT-003",
            f"{label} exceeds the {ARCHITECTURE_TOPOLOGY_MAX_BYTES}-byte limit",
        )
    return data


def _provenance(
    source: str,
    *,
    declaration_sha256: str | None = None,
    detail: str | None = None,
) -> tuple[TraceProvenance, ...]:
    return (
        TraceProvenance(
            source=source,
            line=1,
            declaration_sha256=declaration_sha256,
            detail=detail,
        ),
    )


def resolve_architecture_workspace(project_root: Path, feature_id: str) -> Path:
    """Resolve current change workspace first, while rejecting dual-layout ambiguity."""
    root = project_root.resolve()
    feature = validate_feature_id(feature_id)
    candidates = (
        root / "specs" / "changes" / feature,
        root / "specs" / feature,
    )
    present: list[Path] = []
    for candidate in candidates:
        try:
            safe = ensure_within_project(root, candidate, label="architecture feature workspace")
        except PathSafetyError as exc:
            raise _fail("SDAI-ARCH-DRIFT-004", "architecture feature workspace escapes project root") from exc
        if safe.exists():
            if safe.is_symlink() or not safe.is_dir():
                raise _fail(
                    "SDAI-ARCH-DRIFT-004",
                    f"architecture feature workspace must be a regular directory: {safe.relative_to(root).as_posix()}",
                )
            present.append(safe)
    if not present:
        raise _fail(
            "SDAI-ARCH-DRIFT-004",
            f"no architecture feature workspace exists for {feature!r}",
        )
    if len(present) > 1:
        raise _fail(
            "SDAI-ARCH-DRIFT-004",
            f"architecture feature workspace is ambiguous for {feature!r}: both specs/changes and legacy specs layouts exist",
        )
    return present[0]


def architecture_topology_path(project_root: Path, feature_id: str) -> Path:
    root = project_root.resolve()
    workspace = resolve_architecture_workspace(root, feature_id)
    candidate = workspace / ARCHITECTURE_TOPOLOGY_FILENAME
    try:
        safe = ensure_within_project(root, candidate, label="approved architecture topology")
    except PathSafetyError as exc:
        raise _fail("SDAI-ARCH-DRIFT-004", "approved architecture topology escapes project root") from exc
    return safe


@dataclass(frozen=True, slots=True)
class ArchitectureComponent:
    component_id: str
    roots: tuple[str, ...]
    module_prefixes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "component_id", _identifier(self.component_id, label="component.id"))
        roots = tuple(
            sorted(
                {_portable_relative_path(item, label=f"component {self.component_id!r} root") for item in self.roots},
                key=lambda item: (item.casefold(), item),
            )
        )
        if not roots:
            raise _fail("SDAI-ARCH-DRIFT-002", f"component {self.component_id!r} requires at least one root")
        prefixes: set[str] = set()
        for value in self.module_prefixes:
            if not isinstance(value, str) or not value.strip() or value != value.strip() or "\\" in value:
                raise _fail(
                    "SDAI-ARCH-DRIFT-002",
                    f"component {self.component_id!r} modulePrefix must be bounded portable text",
                )
            if len(value) > 512 or any(ord(char) < 32 for char in value):
                raise _fail("SDAI-ARCH-DRIFT-002", f"component {self.component_id!r} modulePrefix is invalid")
            prefixes.add(value)
        object.__setattr__(self, "roots", roots)
        object.__setattr__(self, "module_prefixes", tuple(sorted(prefixes, key=lambda item: (item.casefold(), item))))

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.component_id,
            "roots": list(self.roots),
            "modulePrefixes": list(self.module_prefixes),
        }


@dataclass(frozen=True, slots=True)
class ArchitectureFact:
    fact_id: str
    kind: ArchitectureFactKind
    mode: ArchitectureFactMode
    source: str
    target: str
    attributes: Mapping[str, object]
    provenance: tuple[TraceProvenance, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "fact_id", _identifier(self.fact_id, label="fact.id"))
        try:
            kind = self.kind if isinstance(self.kind, ArchitectureFactKind) else ArchitectureFactKind(self.kind)
        except ValueError as exc:
            raise _fail("SDAI-ARCH-DRIFT-002", f"unsupported architecture fact kind: {self.kind!r}") from exc
        try:
            mode = self.mode if isinstance(self.mode, ArchitectureFactMode) else ArchitectureFactMode(self.mode)
        except ValueError as exc:
            raise _fail("SDAI-ARCH-DRIFT-002", f"unsupported architecture fact mode: {self.mode!r}") from exc
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "mode", mode)
        object.__setattr__(self, "source", _identifier(self.source, label=f"fact {self.fact_id!r} source"))
        object.__setattr__(self, "target", _identifier(self.target, label=f"fact {self.fact_id!r} target"))
        object.__setattr__(self, "attributes", _json_mapping(self.attributes, label=f"fact {self.fact_id!r} attributes"))
        object.__setattr__(self, "provenance", _canonical_provenance(self.provenance))

    @property
    def semantic_key(self) -> str:
        return _hash_json(
            {
                "kind": self.kind.value,
                "source": self.source,
                "target": self.target,
                "attributes": _thaw_json(self.attributes),
            }
        )

    def truth_dict(self) -> dict[str, object]:
        return {
            "id": self.fact_id,
            "kind": self.kind.value,
            "mode": self.mode.value,
            "source": self.source,
            "target": self.target,
            "attributes": _thaw_json(self.attributes),
        }

    def to_dict(self) -> dict[str, object]:
        value = self.truth_dict()
        value["semanticSha256"] = self.semantic_key
        value["provenance"] = [item.as_dict() for item in self.provenance]
        return value


def _canonical_provenance(values: Iterable[TraceProvenance]) -> tuple[TraceProvenance, ...]:
    by_location: dict[tuple[str, int], TraceProvenance] = {}
    for item in values:
        if not isinstance(item, TraceProvenance):
            raise _fail("SDAI-ARCH-DRIFT-002", "architecture provenance contains an invalid item")
        previous = by_location.get(item.location)
        if previous is not None and previous != item:
            raise _fail(
                "SDAI-ARCH-DRIFT-002",
                f"conflicting architecture provenance at {item.source}:{item.line}",
            )
        by_location[item.location] = item
    return tuple(
        sorted(
            by_location.values(),
            key=lambda item: (
                item.source.casefold(),
                item.source,
                item.line,
                item.declaration_sha256 or "",
                item.detail or "",
            ),
        )
    )


@dataclass(frozen=True, slots=True)
class ArchitectureTopology:
    topology_id: str
    feature_id: str
    components: tuple[ArchitectureComponent, ...]
    facts: tuple[ArchitectureFact, ...]
    approval_evidence: str
    source: str
    file_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "topology_id", _identifier(self.topology_id, label="metadata.id"))
        object.__setattr__(self, "feature_id", validate_feature_id(self.feature_id))
        object.__setattr__(self, "approval_evidence", _portable_relative_path(self.approval_evidence, label="metadata.approvalEvidence"))
        object.__setattr__(self, "source", _portable_relative_path(self.source, label="topology source"))
        if not isinstance(self.file_sha256, str) or _SHA256.fullmatch(self.file_sha256) is None:
            raise _fail("SDAI-ARCH-DRIFT-002", "topology file SHA-256 is invalid")
        components = tuple(sorted(self.components, key=lambda item: item.component_id))
        facts = tuple(sorted(self.facts, key=lambda item: (item.fact_id, item.semantic_key)))
        if len(components) > ARCHITECTURE_MAX_COMPONENTS:
            raise _fail("SDAI-ARCH-DRIFT-002", "architecture topology exceeds the component limit")
        if len(facts) > ARCHITECTURE_MAX_FACTS:
            raise _fail("SDAI-ARCH-DRIFT-002", "architecture topology exceeds the fact limit")
        component_ids = [item.component_id for item in components]
        if len(component_ids) != len(set(component_ids)):
            raise _fail("SDAI-ARCH-DRIFT-002", "architecture topology contains duplicate component ids")
        fact_ids = [item.fact_id for item in facts]
        if len(fact_ids) != len(set(fact_ids)):
            raise _fail("SDAI-ARCH-DRIFT-002", "architecture topology contains duplicate fact ids")
        root_owner: dict[str, str] = {}
        prefix_owner: dict[str, str] = {}
        for component in components:
            for root in component.roots:
                previous = root_owner.get(root)
                if previous is not None and previous != component.component_id:
                    raise _fail(
                        "SDAI-ARCH-DRIFT-002",
                        f"architecture root {root!r} is owned by both {previous!r} and {component.component_id!r}",
                    )
                root_owner[root] = component.component_id
            for prefix in component.module_prefixes:
                previous = prefix_owner.get(prefix)
                if previous is not None and previous != component.component_id:
                    raise _fail(
                        "SDAI-ARCH-DRIFT-002",
                        f"architecture module prefix {prefix!r} is owned by both {previous!r} and {component.component_id!r}",
                    )
                prefix_owner[prefix] = component.component_id
        object.__setattr__(self, "components", components)
        object.__setattr__(self, "facts", facts)

    @property
    def subject(self) -> str:
        return f"architecture-topology:{self.feature_id}:{self.topology_id}"

    def truth_dict(self) -> dict[str, object]:
        return {
            "apiVersion": ARCHITECTURE_TOPOLOGY_API_VERSION,
            "kind": "ApprovedArchitecture",
            "metadata": {
                "id": self.topology_id,
                "feature": self.feature_id,
                "approvalEvidence": self.approval_evidence,
            },
            "spec": {
                "components": [item.to_dict() for item in self.components],
                "facts": [item.truth_dict() for item in self.facts],
            },
        }

    @property
    def sha256(self) -> str:
        return _hash_json(self.truth_dict())

    def to_dict(self) -> dict[str, object]:
        value = self.truth_dict()
        value["source"] = self.source
        value["fileSha256"] = self.file_sha256
        value["sha256"] = self.sha256
        return value

    def to_json(self) -> str:
        return _canonical_json(self.to_dict()) + "\n"


@dataclass(frozen=True, slots=True)
class ApprovedArchitecture:
    topology: ArchitectureTopology
    approval: TraceEvidence
    freshness: EvidenceFreshnessReport

    def to_dict(self) -> dict[str, object]:
        return {
            "topology": self.topology.to_dict(),
            "approvalEvidenceId": self.approval.evidence_id,
            "approvalTruthSha256": self.approval.truth_sha256,
            "approvalFreshness": self.freshness.as_dict(),
        }


def _parse_component(raw: object) -> ArchitectureComponent:
    if not isinstance(raw, Mapping) or not all(isinstance(key, str) for key in raw):
        raise _fail("SDAI-ARCH-DRIFT-002", "component must be a string-keyed mapping")
    if set(raw) != {"id", "roots", "modulePrefixes"}:
        raise _fail("SDAI-ARCH-DRIFT-002", "component fields must be exactly id, roots, modulePrefixes")
    roots = raw.get("roots")
    prefixes = raw.get("modulePrefixes")
    if not isinstance(roots, list) or not all(isinstance(item, str) for item in roots):
        raise _fail("SDAI-ARCH-DRIFT-002", "component.roots must be a string list")
    if not isinstance(prefixes, list) or not all(isinstance(item, str) for item in prefixes):
        raise _fail("SDAI-ARCH-DRIFT-002", "component.modulePrefixes must be a string list")
    component_id = raw.get("id")
    if not isinstance(component_id, str):
        raise _fail("SDAI-ARCH-DRIFT-002", "component.id must be text")
    return ArchitectureComponent(component_id, tuple(roots), tuple(prefixes))


def _parse_fact(raw: object, *, topology_source: str, file_sha256: str) -> ArchitectureFact:
    if not isinstance(raw, Mapping) or not all(isinstance(key, str) for key in raw):
        raise _fail("SDAI-ARCH-DRIFT-002", "fact must be a string-keyed mapping")
    expected = {"id", "kind", "mode", "source", "target", "attributes"}
    if set(raw) != expected:
        raise _fail(
            "SDAI-ARCH-DRIFT-002",
            "fact fields must be exactly id, kind, mode, source, target, attributes",
        )
    if not all(isinstance(raw.get(name), str) for name in ("id", "kind", "mode", "source", "target")):
        raise _fail("SDAI-ARCH-DRIFT-002", "fact identity/kind/mode/source/target fields must be text")
    return ArchitectureFact(
        fact_id=str(raw["id"]),
        kind=ArchitectureFactKind(str(raw["kind"])),
        mode=ArchitectureFactMode(str(raw["mode"])),
        source=str(raw["source"]),
        target=str(raw["target"]),
        attributes=_json_mapping(raw.get("attributes"), label=f"fact {raw['id']!r} attributes"),
        provenance=_provenance(
            topology_source,
            declaration_sha256=file_sha256,
            detail=f"approved architecture fact {raw['id']}",
        ),
    )


def load_architecture_topology(project_root: Path, feature_id: str) -> ArchitectureTopology:
    root = project_root.resolve()
    feature = validate_feature_id(feature_id)
    path = architecture_topology_path(root, feature)
    try:
        relative = path.relative_to(root).as_posix()
    except ValueError as exc:
        raise _fail("SDAI-ARCH-DRIFT-004", "topology path escapes project root") from exc
    safe = _safe_existing_file(root, relative, label="approved architecture topology")
    content = _read_bounded(safe, label="approved architecture topology")
    try:
        text = content.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError as exc:
        raise _fail("SDAI-ARCH-DRIFT-003", "approved architecture topology must be valid UTF-8") from exc
    try:
        raw = yaml.load(text, Loader=_UniqueKeyLoader)
    except yaml.YAMLError as exc:
        raise _fail("SDAI-ARCH-DRIFT-002", f"invalid approved architecture topology YAML: {exc}") from exc
    if not isinstance(raw, Mapping) or not all(isinstance(key, str) for key in raw):
        raise _fail("SDAI-ARCH-DRIFT-002", "approved architecture topology must be a string-keyed mapping")
    if set(raw) != {"apiVersion", "kind", "metadata", "spec"}:
        raise _fail("SDAI-ARCH-DRIFT-002", "topology fields must be exactly apiVersion, kind, metadata, spec")
    if raw.get("apiVersion") != ARCHITECTURE_TOPOLOGY_API_VERSION or raw.get("kind") != "ApprovedArchitecture":
        raise _fail(
            "SDAI-ARCH-DRIFT-002",
            f"topology must use {ARCHITECTURE_TOPOLOGY_API_VERSION} / ApprovedArchitecture",
        )
    metadata = raw.get("metadata")
    spec = raw.get("spec")
    if not isinstance(metadata, Mapping) or set(metadata) != {"id", "feature", "approvalEvidence"}:
        raise _fail("SDAI-ARCH-DRIFT-002", "topology metadata requires exactly id, feature, approvalEvidence")
    if not isinstance(spec, Mapping) or set(spec) != {"components", "facts"}:
        raise _fail("SDAI-ARCH-DRIFT-002", "topology spec requires exactly components and facts")
    topology_id = metadata.get("id")
    declared_feature = metadata.get("feature")
    approval_evidence = metadata.get("approvalEvidence")
    if not isinstance(topology_id, str) or not isinstance(declared_feature, str) or not isinstance(approval_evidence, str):
        raise _fail("SDAI-ARCH-DRIFT-002", "topology metadata values must be text")
    if validate_feature_id(declared_feature) != feature:
        raise _fail(
            "SDAI-ARCH-DRIFT-002",
            f"topology metadata.feature {declared_feature!r} does not match requested feature {feature!r}",
        )
    components_raw = spec.get("components")
    facts_raw = spec.get("facts")
    if not isinstance(components_raw, list) or not isinstance(facts_raw, list):
        raise _fail("SDAI-ARCH-DRIFT-002", "topology components and facts must be lists")
    if len(components_raw) > ARCHITECTURE_MAX_COMPONENTS or len(facts_raw) > ARCHITECTURE_MAX_FACTS:
        raise _fail("SDAI-ARCH-DRIFT-002", "topology exceeds component/fact bounds")
    file_sha256 = _hash_bytes(content)
    components = tuple(_parse_component(item) for item in components_raw)
    facts = tuple(_parse_fact(item, topology_source=relative, file_sha256=file_sha256) for item in facts_raw)
    return ArchitectureTopology(
        topology_id=topology_id,
        feature_id=feature,
        components=components,
        facts=facts,
        approval_evidence=approval_evidence,
        source=relative,
        file_sha256=file_sha256,
    )


def _approval_claim(record: TraceEvidence) -> Mapping[str, object]:
    raw = record.result.get("architectureApproval") if isinstance(record.result, Mapping) else None
    if not isinstance(raw, Mapping):
        raise _fail("SDAI-ARCH-DRIFT-005", "approval result.architectureApproval claim is missing")
    if set(raw) != {"featureId", "topologyId", "topologySha256"}:
        raise _fail(
            "SDAI-ARCH-DRIFT-005",
            "approval architectureApproval claim must contain exactly featureId, topologyId, topologySha256",
        )
    return raw


def load_approved_architecture(project_root: Path, feature_id: str) -> ApprovedArchitecture:
    """Load topology only when current human architecture approval validates its exact bytes/hash."""
    root = project_root.resolve()
    topology = load_architecture_topology(root, feature_id)
    try:
        approval = load_trace_evidence(root, Path(topology.approval_evidence))
    except TraceEvidenceError as exc:
        raise _fail("SDAI-ARCH-DRIFT-005", f"unable to load architecture approval evidence: {exc}") from exc
    if approval.kind is not EvidenceKind.APPROVAL:
        raise _fail("SDAI-ARCH-DRIFT-005", "architecture approval evidence kind must be approval")
    if approval.status is not EvidenceStatus.PASSED:
        raise _fail("SDAI-ARCH-DRIFT-005", "architecture approval evidence status must be passed")
    if approval.producer.semantic_role != "architecture-approver":
        raise _fail(
            "SDAI-ARCH-DRIFT-005",
            "architecture approval producer semantic_role must be architecture-approver",
        )
    if approval.producer.provider is not None or approval.producer.model is not None:
        raise _fail(
            "SDAI-ARCH-DRIFT-005",
            "architecture approval cannot be self-approved by an AI provider/model",
        )
    if approval.subject != topology.subject:
        raise _fail(
            "SDAI-ARCH-DRIFT-005",
            f"architecture approval subject must be {topology.subject!r}",
        )
    artifact_bindings = [
        item
        for item in approval.bindings
        if item.kind is EvidenceBindingKind.ARTIFACT and item.source == topology.source
    ]
    if len(artifact_bindings) != 1 or artifact_bindings[0].sha256 != topology.file_sha256:
        raise _fail(
            "SDAI-ARCH-DRIFT-005",
            "architecture approval must bind the exact current topology file SHA-256",
        )
    claim = _approval_claim(approval)
    if claim.get("featureId") != topology.feature_id:
        raise _fail("SDAI-ARCH-DRIFT-005", "architecture approval featureId does not match topology")
    if claim.get("topologyId") != topology.topology_id:
        raise _fail("SDAI-ARCH-DRIFT-005", "architecture approval topologyId does not match topology")
    if claim.get("topologySha256") != topology.sha256:
        raise _fail("SDAI-ARCH-DRIFT-005", "architecture approval topologySha256 does not match topology")
    try:
        freshness = evaluate_trace_evidence_freshness(root, approval)
    except TraceFreshnessError as exc:
        raise _fail("SDAI-ARCH-DRIFT-005", f"unable to evaluate architecture approval freshness: {exc}") from exc
    if freshness.freshness is not ProofFreshness.VALID:
        raise _fail(
            "SDAI-ARCH-DRIFT-005",
            "architecture approval evidence is not current: " + "; ".join(freshness.reasons),
        )
    return ApprovedArchitecture(topology=topology, approval=approval, freshness=freshness)


@dataclass(frozen=True, slots=True)
class ObservedArchitectureFact:
    kind: ArchitectureFactKind
    source: str
    target: str
    attributes: Mapping[str, object]
    provenance: tuple[TraceProvenance, ...]

    def __post_init__(self) -> None:
        try:
            kind = self.kind if isinstance(self.kind, ArchitectureFactKind) else ArchitectureFactKind(self.kind)
        except ValueError as exc:
            raise _fail("SDAI-ARCH-DRIFT-006", f"unsupported observed architecture fact kind: {self.kind!r}") from exc
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "source", _identifier(self.source, label="observed fact source"))
        object.__setattr__(self, "target", _identifier(self.target, label="observed fact target"))
        object.__setattr__(self, "attributes", _json_mapping(self.attributes, label="observed fact attributes"))
        provenance = _canonical_provenance(self.provenance)
        if not provenance:
            raise _fail("SDAI-ARCH-DRIFT-006", "observed architecture fact requires repository provenance")
        object.__setattr__(self, "provenance", provenance)

    @property
    def semantic_key(self) -> str:
        return _hash_json(
            {
                "kind": self.kind.value,
                "source": self.source,
                "target": self.target,
                "attributes": _thaw_json(self.attributes),
            }
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "source": self.source,
            "target": self.target,
            "attributes": _thaw_json(self.attributes),
            "semanticSha256": self.semantic_key,
            "provenance": [item.as_dict() for item in self.provenance],
        }


@dataclass(frozen=True, slots=True)
class ArchitectureObservation:
    observer_id: str
    facts: tuple[ObservedArchitectureFact, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "observer_id", _identifier(self.observer_id, label="observer_id"))
        facts = tuple(
            sorted(
                self.facts,
                key=lambda item: (
                    item.semantic_key,
                    tuple((p.source, p.line) for p in item.provenance),
                ),
            )
        )
        if len(facts) > ARCHITECTURE_MAX_FACTS:
            raise _fail("SDAI-ARCH-DRIFT-006", "architecture observation exceeds the fact limit")
        object.__setattr__(self, "facts", facts)

    def body_dict(self) -> dict[str, object]:
        return {
            "apiVersion": ARCHITECTURE_OBSERVATION_API_VERSION,
            "kind": "ArchitectureObservation",
            "observerId": self.observer_id,
            "facts": [item.to_dict() for item in self.facts],
        }

    @property
    def sha256(self) -> str:
        return _hash_json(self.body_dict())

    def to_dict(self) -> dict[str, object]:
        value = self.body_dict()
        value["sha256"] = self.sha256
        return value

    def to_json(self) -> str:
        return _canonical_json(self.to_dict()) + "\n"


class ArchitectureObserver(Protocol):
    @property
    def observer_id(self) -> str: ...

    def observe(
        self,
        project_root: Path,
        approved: ApprovedArchitecture,
    ) -> ArchitectureObservation: ...


class ArchitectureObserverRegistry:
    def __init__(self, observers: Iterable[ArchitectureObserver] = ()) -> None:
        self._observers: dict[str, ArchitectureObserver] = {}
        for observer in observers:
            self.register(observer)

    def register(self, observer: ArchitectureObserver) -> None:
        observer_id = _identifier(getattr(observer, "observer_id", None), label="observer_id")
        if observer_id in self._observers:
            raise _fail("SDAI-ARCH-DRIFT-007", f"duplicate architecture observer id: {observer_id}")
        self._observers[observer_id] = observer

    @property
    def observer_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._observers))

    def require(self, observer_id: str) -> ArchitectureObserver:
        normalized = _identifier(observer_id, label="observer_id")
        observer = self._observers.get(normalized)
        if observer is None:
            raise _fail("SDAI-ARCH-DRIFT-007", f"architecture observer is not registered: {normalized}")
        return observer

    def observe_all(
        self,
        project_root: Path,
        approved: ApprovedArchitecture,
    ) -> tuple[ArchitectureObservation, ...]:
        observations: list[ArchitectureObservation] = []
        for observer_id in self.observer_ids:
            result = self._observers[observer_id].observe(project_root.resolve(), approved)
            if not isinstance(result, ArchitectureObservation):
                raise _fail(
                    "SDAI-ARCH-DRIFT-007",
                    f"architecture observer {observer_id!r} returned an invalid observation",
                )
            if result.observer_id != observer_id:
                raise _fail(
                    "SDAI-ARCH-DRIFT-007",
                    f"architecture observer {observer_id!r} returned mismatched observerId {result.observer_id!r}",
                )
            observations.append(result)
        return tuple(observations)


@dataclass(frozen=True, slots=True)
class ArchitectureDriftFinding:
    code: str
    severity: ArchitectureDriftSeverity
    kind: ArchitectureFactKind
    source: str
    target: str
    attributes: Mapping[str, object]
    approved_fact_id: str | None
    approved_provenance: tuple[TraceProvenance, ...]
    observed_provenance: tuple[TraceProvenance, ...]
    message: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "code", _identifier(self.code, label="drift finding code"))
        try:
            severity = self.severity if isinstance(self.severity, ArchitectureDriftSeverity) else ArchitectureDriftSeverity(self.severity)
        except ValueError as exc:
            raise _fail("SDAI-ARCH-DRIFT-008", f"unsupported drift severity: {self.severity!r}") from exc
        try:
            kind = self.kind if isinstance(self.kind, ArchitectureFactKind) else ArchitectureFactKind(self.kind)
        except ValueError as exc:
            raise _fail("SDAI-ARCH-DRIFT-008", f"unsupported drift kind: {self.kind!r}") from exc
        object.__setattr__(self, "severity", severity)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "source", _identifier(self.source, label="drift source"))
        object.__setattr__(self, "target", _identifier(self.target, label="drift target"))
        object.__setattr__(self, "attributes", _json_mapping(self.attributes, label="drift attributes"))
        if self.approved_fact_id is not None:
            object.__setattr__(self, "approved_fact_id", _identifier(self.approved_fact_id, label="approved_fact_id"))
        object.__setattr__(self, "approved_provenance", _canonical_provenance(self.approved_provenance))
        object.__setattr__(self, "observed_provenance", _canonical_provenance(self.observed_provenance))
        if not isinstance(self.message, str) or not self.message.strip() or len(self.message) > 4096:
            raise _fail("SDAI-ARCH-DRIFT-008", "drift finding message must be bounded non-empty text")
        object.__setattr__(self, "message", self.message.strip())

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "severity": self.severity.value,
            "kind": self.kind.value,
            "source": self.source,
            "target": self.target,
            "attributes": _thaw_json(self.attributes),
            "approvedFactId": self.approved_fact_id,
            "approvedProvenance": [item.as_dict() for item in self.approved_provenance],
            "observedProvenance": [item.as_dict() for item in self.observed_provenance],
            "message": self.message,
        }


@dataclass(frozen=True, slots=True)
class ArchitectureDriftReport:
    topology_sha256: str
    approval_truth_sha256: str
    observations: tuple[ArchitectureObservation, ...]
    findings: tuple[ArchitectureDriftFinding, ...]

    def __post_init__(self) -> None:
        if _SHA256.fullmatch(self.topology_sha256) is None or _SHA256.fullmatch(self.approval_truth_sha256) is None:
            raise _fail("SDAI-ARCH-DRIFT-008", "drift report topology/approval hashes are invalid")
        object.__setattr__(self, "observations", tuple(sorted(self.observations, key=lambda item: (item.observer_id, item.sha256))))
        object.__setattr__(
            self,
            "findings",
            tuple(
                sorted(
                    self.findings,
                    key=lambda item: (
                        item.severity.value,
                        item.code,
                        item.kind.value,
                        item.source,
                        item.target,
                        item.approved_fact_id or "",
                        _canonical_json(_thaw_json(item.attributes)),
                    ),
                )
            ),
        )

    @property
    def drifted(self) -> bool:
        return bool(self.findings)

    def body_dict(self) -> dict[str, object]:
        return {
            "apiVersion": ARCHITECTURE_DRIFT_API_VERSION,
            "kind": "ArchitectureDriftReport",
            "topologySha256": self.topology_sha256,
            "approvalTruthSha256": self.approval_truth_sha256,
            "drifted": self.drifted,
            "observations": [
                {"observerId": item.observer_id, "sha256": item.sha256}
                for item in self.observations
            ],
            "findings": [item.to_dict() for item in self.findings],
        }

    @property
    def sha256(self) -> str:
        return _hash_json(self.body_dict())

    def to_dict(self) -> dict[str, object]:
        value = self.body_dict()
        value["sha256"] = self.sha256
        return value

    def to_json(self) -> str:
        return _canonical_json(self.to_dict()) + "\n"


def _observed_groups(
    observations: Iterable[ArchitectureObservation],
) -> tuple[dict[str, list[ObservedArchitectureFact]], tuple[ArchitectureObservation, ...]]:
    canonical_observations = tuple(sorted(observations, key=lambda item: (item.observer_id, item.sha256)))
    seen_observers: set[str] = set()
    groups: dict[str, list[ObservedArchitectureFact]] = {}
    for observation in canonical_observations:
        if observation.observer_id in seen_observers:
            raise _fail(
                "SDAI-ARCH-DRIFT-008",
                f"duplicate architecture observation for observer {observation.observer_id!r}",
            )
        seen_observers.add(observation.observer_id)
        for fact in observation.facts:
            groups.setdefault(fact.semantic_key, []).append(fact)
    return groups, canonical_observations


def compare_architecture(
    approved: ApprovedArchitecture,
    observations: Iterable[ArchitectureObservation],
) -> ArchitectureDriftReport:
    """Compare approved truth with provider-independent observations deterministically."""
    if not isinstance(approved, ApprovedArchitecture):
        raise _fail("SDAI-ARCH-DRIFT-008", "approved architecture must be validated before comparison")
    groups, canonical_observations = _observed_groups(observations)
    approved_keys = {fact.semantic_key for fact in approved.topology.facts}
    findings: list[ArchitectureDriftFinding] = []

    for fact in approved.topology.facts:
        observed = groups.get(fact.semantic_key, [])
        observed_provenance = _canonical_provenance(
            provenance
            for item in observed
            for provenance in item.provenance
        )
        if fact.mode is ArchitectureFactMode.REQUIRED and not observed:
            findings.append(
                ArchitectureDriftFinding(
                    code="ARCH-DRIFT-REQUIRED-MISSING",
                    severity=ArchitectureDriftSeverity.ERROR,
                    kind=fact.kind,
                    source=fact.source,
                    target=fact.target,
                    attributes=fact.attributes,
                    approved_fact_id=fact.fact_id,
                    approved_provenance=fact.provenance,
                    observed_provenance=(),
                    message=f"required architecture fact {fact.fact_id!r} is not present in repository observations",
                )
            )
        elif fact.mode is ArchitectureFactMode.FORBIDDEN and observed:
            findings.append(
                ArchitectureDriftFinding(
                    code="ARCH-DRIFT-FORBIDDEN-PRESENT",
                    severity=ArchitectureDriftSeverity.ERROR,
                    kind=fact.kind,
                    source=fact.source,
                    target=fact.target,
                    attributes=fact.attributes,
                    approved_fact_id=fact.fact_id,
                    approved_provenance=fact.provenance,
                    observed_provenance=observed_provenance,
                    message=f"forbidden architecture fact {fact.fact_id!r} is present in repository observations",
                )
            )

    for semantic_key, observed in sorted(groups.items()):
        if semantic_key in approved_keys:
            continue
        first = observed[0]
        provenance = _canonical_provenance(
            item
            for fact in observed
            for item in fact.provenance
        )
        findings.append(
            ArchitectureDriftFinding(
                code="ARCH-DRIFT-UNEXPECTED-PRESENT",
                severity=ArchitectureDriftSeverity.WARNING,
                kind=first.kind,
                source=first.source,
                target=first.target,
                attributes=first.attributes,
                approved_fact_id=None,
                approved_provenance=_provenance(
                    approved.topology.source,
                    declaration_sha256=approved.topology.file_sha256,
                    detail="approved topology does not declare this observed fact",
                ),
                observed_provenance=provenance,
                message="repository contains an architecture fact not declared by the approved topology",
            )
        )

    return ArchitectureDriftReport(
        topology_sha256=approved.topology.sha256,
        approval_truth_sha256=approved.approval.truth_sha256,
        observations=canonical_observations,
        findings=tuple(findings),
    )


__all__ = [
    "ARCHITECTURE_DRIFT_API_VERSION",
    "ARCHITECTURE_OBSERVATION_API_VERSION",
    "ARCHITECTURE_TOPOLOGY_API_VERSION",
    "ARCHITECTURE_TOPOLOGY_FILENAME",
    "ApprovedArchitecture",
    "ArchitectureComponent",
    "ArchitectureDriftError",
    "ArchitectureDriftFinding",
    "ArchitectureDriftReport",
    "ArchitectureDriftSeverity",
    "ArchitectureFact",
    "ArchitectureFactKind",
    "ArchitectureFactMode",
    "ArchitectureObservation",
    "ArchitectureObserver",
    "ArchitectureObserverRegistry",
    "ArchitectureTopology",
    "ObservedArchitectureFact",
    "architecture_topology_path",
    "compare_architecture",
    "load_approved_architecture",
    "load_architecture_topology",
    "resolve_architecture_workspace",
]
