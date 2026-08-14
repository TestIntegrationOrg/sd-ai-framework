from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from functools import cmp_to_key
from hashlib import sha256
import json
from pathlib import Path, PurePosixPath
import re
from types import MappingProxyType
from typing import Iterable, Mapping
import unicodedata

import yaml
from yaml.constructor import ConstructorError
from yaml.resolver import BaseResolver

from sdai.pack_manifest import PackManifestError, SemVer
from sdai.text import TextEncodingError


SPECIFICATION_STORE_MANIFEST_API_VERSION = "sdai.specification-store/v1"
SPECIFICATION_STORE_REGISTRY_API_VERSION = "sdai.specification-store-registry/v1"
SPECIFICATION_STORE_RESOLUTION_API_VERSION = "sdai.specification-store-resolution/v1"
SPECIFICATION_STORE_MANIFEST_PATH = ".sdai-store/store.yaml"
SPECIFICATION_STORE_MANIFEST_MAX_BYTES = 1024 * 1024


class SpecificationStoreError(RuntimeError):
    """Raised when a SpecificationStore definition or resolution is unsafe."""


class SpecificationStoreLayer(StrEnum):
    CORE = "core"
    ORG = "org"
    REPO = "repo"
    USER = "user"

    @property
    def priority(self) -> int:
        return _LAYER_PRIORITY[self]


_LAYER_PRIORITY = {
    SpecificationStoreLayer.CORE: 0,
    SpecificationStoreLayer.ORG: 20,
    SpecificationStoreLayer.REPO: 30,
    SpecificationStoreLayer.USER: 40,
}
_LOCKABLE_LAYERS = frozenset(
    {SpecificationStoreLayer.CORE, SpecificationStoreLayer.ORG}
)
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9]*(?:[-_.][a-z0-9]+)*$")
_WINDOWS_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CLOCK$", "CONIN$", "CONOUT$"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
    | {f"COM{suffix}" for suffix in ("¹", "²", "³")}
    | {f"LPT{suffix}" for suffix in ("¹", "²", "³")}
)
_WINDOWS_FORBIDDEN = frozenset('<>:"|?*')
_TOP_LEVEL_KEYS = frozenset({"apiVersion", "kind", "metadata", "spec"})
_METADATA_KEYS = frozenset({"id", "version", "description"})
_SPEC_REQUIRED_KEYS = frozenset({"specificationRoots", "capabilities"})
_SPEC_OPTIONAL_KEYS = frozenset({"metadata"})


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


def _fail(code: str, message: str) -> SpecificationStoreError:
    return SpecificationStoreError(f"{code}: {message}")


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
        raise _fail(
            "SDAI-STORE-001",
            "SpecificationStore data must be canonical finite JSON",
        ) from exc


def _hash_json(value: object) -> str:
    return "sha256:" + sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _mapping(value: object, *, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise _fail("SDAI-STORE-001", f"{label} must be a string-keyed mapping")
    return value


def _validate_keys(
    value: Mapping[str, object],
    *,
    required: frozenset[str],
    optional: frozenset[str] = frozenset(),
    label: str,
) -> None:
    unknown = sorted(set(value) - required - optional)
    missing = sorted(required - set(value))
    if unknown:
        raise _fail(
            "SDAI-STORE-001",
            f"{label} contains unsupported field(s): {', '.join(unknown)}",
        )
    if missing:
        raise _fail(
            "SDAI-STORE-001",
            f"{label} is missing required field(s): {', '.join(missing)}",
        )


def _contains_surrogate(value: str) -> bool:
    return any(0xD800 <= ord(char) <= 0xDFFF for char in value)


def _text(value: object, *, label: str, maximum: int = 1024) -> str:
    if not isinstance(value, str):
        raise _fail("SDAI-STORE-001", f"{label} must be a string")
    normalized = unicodedata.normalize("NFC", value.strip())
    if (
        not normalized
        or len(normalized) > maximum
        or "\x00" in normalized
        or _contains_surrogate(normalized)
    ):
        raise _fail(
            "SDAI-STORE-001",
            f"{label} must contain 1-{maximum} portable Unicode characters",
        )
    if any(ord(char) < 32 and char not in {"\n", "\t"} for char in normalized):
        raise _fail("SDAI-STORE-001", f"{label} contains a control character")
    return normalized


def _identifier(value: object, *, label: str) -> str:
    text = _text(value, label=label, maximum=128)
    if not _IDENTIFIER.fullmatch(text):
        raise _fail(
            "SDAI-STORE-001",
            f"{label} '{text}' is not a portable lowercase identifier",
        )
    return text


def _portable_relative_path(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise _fail("SDAI-STORE-002", f"{label} must be a portable relative path")
    if (
        "\\" in value
        or "\x00" in value
        or _contains_surrogate(value)
        or re.match(r"^[A-Za-z]:", value)
    ):
        raise _fail("SDAI-STORE-002", f"{label} must be a portable relative path")
    normalized = unicodedata.normalize("NFC", value)
    path = PurePosixPath(normalized)
    parts = normalized.split("/")
    if path.is_absolute() or any(part in {"", ".", ".."} for part in parts):
        raise _fail("SDAI-STORE-002", f"{label} must be a portable relative path")
    for part in parts:
        if part != part.strip() or any(ord(char) < 32 for char in part):
            raise _fail("SDAI-STORE-002", f"{label} contains a non-portable segment")
        if any(char in _WINDOWS_FORBIDDEN for char in part) or part.endswith((".", " ")):
            raise _fail("SDAI-STORE-002", f"{label} is not portable across Windows/Linux")
        if part.split(".", 1)[0].upper() in _WINDOWS_RESERVED:
            raise _fail("SDAI-STORE-002", f"{label} uses a reserved Windows segment")
    return path.as_posix()


def _normalize_json(
    value: object,
    *,
    label: str,
    ancestors: frozenset[int] = frozenset(),
    depth: int = 0,
    nodes: list[int] | None = None,
) -> object:
    if nodes is None:
        nodes = [0]
    nodes[0] += 1
    if nodes[0] > 100_000:
        raise _fail("SDAI-STORE-001", f"{label} exceeds the maximum value count")
    if depth > 64:
        raise _fail("SDAI-STORE-001", f"{label} exceeds the maximum nesting depth")
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, str):
        if "\x00" in value or _contains_surrogate(value):
            raise _fail("SDAI-STORE-001", f"{label} contains invalid Unicode text")
        return unicodedata.normalize("NFC", value)
    if isinstance(value, float):
        if value != value or value in {float("inf"), float("-inf")}:
            raise _fail("SDAI-STORE-001", f"{label} contains a non-finite number")
        return value
    if isinstance(value, list):
        identity = id(value)
        if identity in ancestors:
            raise _fail("SDAI-STORE-001", f"{label} contains a recursive value")
        descendants = ancestors | {identity}
        return [
            _normalize_json(
                item,
                label=f"{label}[]",
                ancestors=descendants,
                depth=depth + 1,
                nodes=nodes,
            )
            for item in value
        ]
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) and key for key in value):
            raise _fail("SDAI-STORE-001", f"{label} keys must be non-empty strings")
        identity = id(value)
        if identity in ancestors:
            raise _fail("SDAI-STORE-001", f"{label} contains a recursive value")
        descendants = ancestors | {identity}
        normalized: dict[str, object] = {}
        for key in sorted(value):
            canonical_key = unicodedata.normalize("NFC", key)
            if "\x00" in canonical_key or _contains_surrogate(canonical_key):
                raise _fail(
                    "SDAI-STORE-001",
                    f"{label} contains an invalid Unicode key",
                )
            if canonical_key in normalized:
                raise _fail(
                    "SDAI-STORE-001",
                    f"{label} contains Unicode-normalization-colliding keys",
                )
            normalized[canonical_key] = _normalize_json(
                value[key],
                label=f"{label}.{key}",
                ancestors=descendants,
                depth=depth + 1,
                nodes=nodes,
            )
        return normalized
    raise _fail("SDAI-STORE-001", f"{label} must contain only JSON-compatible values")


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _version(value: object) -> SemVer:
    try:
        candidate = str(value) if isinstance(value, SemVer) else value
        if isinstance(candidate, str) and len(candidate) > 256:
            raise _fail("SDAI-STORE-003", "SpecificationStore version exceeds 256 characters")
        return SemVer.parse(candidate)
    except (PackManifestError, TypeError, ValueError) as exc:
        raise _fail("SDAI-STORE-003", f"invalid SpecificationStore version {value!r}") from exc


def _version_compare(left: SemVer, right: SemVer) -> int:
    precedence = left.compare_precedence(right)
    if precedence:
        return precedence
    if str(left) == str(right):
        return 0
    return -1 if str(left) < str(right) else 1


def _layer(value: SpecificationStoreLayer | str) -> SpecificationStoreLayer:
    try:
        return SpecificationStoreLayer(value)
    except ValueError as exc:
        raise _fail("SDAI-STORE-REG-001", f"unknown store registry layer {value!r}") from exc


def _source_label(value: object) -> str:
    return _text(value, label="store source label", maximum=512)


def _read_manifest_text(path: Path) -> str:
    with path.open("rb") as stream:
        data = stream.read(SPECIFICATION_STORE_MANIFEST_MAX_BYTES + 1)
    if len(data) > SPECIFICATION_STORE_MANIFEST_MAX_BYTES:
        raise _fail(
            "SDAI-STORE-001",
            "SpecificationStore manifest exceeds the 1 MiB input limit",
        )
    try:
        decoded = data.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError as exc:
        raise TextEncodingError("SpecificationStore manifest is not valid UTF-8") from exc
    return decoded.replace("\r\n", "\n").replace("\r", "\n")


def _validate_layout(store_root: Path, roots: tuple["SpecificationRoot", ...]) -> None:
    if store_root.is_symlink():
        raise _fail("SDAI-STORE-002", "SpecificationStore root must not be a symlink")
    if not store_root.exists() or not store_root.is_dir():
        raise _fail("SDAI-STORE-002", "SpecificationStore root must be an existing directory")
    resolved_root = store_root.resolve()
    for root in roots:
        candidate = resolved_root
        for part in PurePosixPath(root.path).parts:
            candidate = candidate / part
            if candidate.is_symlink():
                raise _fail(
                    "SDAI-STORE-002",
                    f"specification root '{root.path}' contains a symlink component",
                )
        if not candidate.exists() or not candidate.is_dir():
            raise _fail(
                "SDAI-STORE-002",
                f"specification root '{root.path}' must be an existing directory",
            )
        try:
            candidate.resolve().relative_to(resolved_root)
        except ValueError as exc:
            raise _fail(
                "SDAI-STORE-002",
                f"specification root '{root.path}' escapes the store root",
            ) from exc


@dataclass(frozen=True)
class SpecificationRoot:
    id: str
    path: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _identifier(self.id, label="specification root id"))
        path = _portable_relative_path(self.path, label=f"specification root '{self.id}' path")
        portable_key = path.casefold()
        if portable_key == ".sdai-store" or portable_key.startswith(".sdai-store/"):
            raise _fail("SDAI-STORE-002", "specification roots cannot contain store metadata")
        object.__setattr__(self, "path", path)

    def as_dict(self) -> dict[str, str]:
        return {"id": self.id, "path": self.path}


@dataclass(frozen=True)
class SpecificationStoreManifest:
    id: str
    version: SemVer
    description: str
    specification_roots: tuple[SpecificationRoot, ...]
    capabilities: tuple[str, ...]
    metadata: Mapping[str, object] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        store_id = _identifier(self.id, label="store id")
        version = _version(self.version)
        description = _text(self.description, label="store description", maximum=2048)
        if (
            not isinstance(self.specification_roots, (list, tuple))
            or not self.specification_roots
            or not all(
                isinstance(item, SpecificationRoot)
                for item in self.specification_roots
            )
        ):
            raise _fail("SDAI-STORE-001", "specificationRoots must not be empty")
        roots = tuple(sorted(self.specification_roots, key=lambda item: item.id))
        ids = [item.id for item in roots]
        if len(set(ids)) != len(ids):
            raise _fail("SDAI-STORE-001", "specificationRoots must not repeat an id")
        path_keys = [item.path.casefold() for item in roots]
        if len(set(path_keys)) != len(path_keys):
            raise _fail("SDAI-STORE-002", "specificationRoots contain a path collision")
        for left_index, left in enumerate(roots):
            left_parts = tuple(part.casefold() for part in PurePosixPath(left.path).parts)
            for right in roots[left_index + 1 :]:
                right_parts = tuple(part.casefold() for part in PurePosixPath(right.path).parts)
                shorter, longer = sorted((left_parts, right_parts), key=len)
                if longer[: len(shorter)] == shorter:
                    raise _fail("SDAI-STORE-002", "specificationRoots must not overlap")
        if not isinstance(self.capabilities, (list, tuple)):
            raise _fail("SDAI-STORE-001", "capabilities must be a non-empty unique list")
        capabilities = tuple(
            sorted(_identifier(item, label="store capability") for item in self.capabilities)
        )
        if not capabilities or len(set(capabilities)) != len(capabilities):
            raise _fail("SDAI-STORE-001", "capabilities must be a non-empty unique list")
        normalized_metadata = _normalize_json(self.metadata, label="store metadata")
        if not isinstance(normalized_metadata, Mapping):
            raise _fail("SDAI-STORE-001", "store metadata must be a mapping")
        if len(_canonical_json(normalized_metadata).encode("utf-8")) > 65536:
            raise _fail("SDAI-STORE-001", "store metadata exceeds 65536 UTF-8 bytes")
        object.__setattr__(self, "id", store_id)
        object.__setattr__(self, "version", version)
        object.__setattr__(self, "description", description)
        object.__setattr__(self, "specification_roots", roots)
        object.__setattr__(self, "capabilities", capabilities)
        object.__setattr__(self, "metadata", _freeze(normalized_metadata))
        _canonical_json(self.as_dict())

    @property
    def identity(self) -> str:
        return f"{self.id}@{self.version}"

    def as_dict(self) -> dict[str, object]:
        return {
            "apiVersion": SPECIFICATION_STORE_MANIFEST_API_VERSION,
            "kind": "SpecificationStore",
            "metadata": {
                "description": self.description,
                "id": self.id,
                "version": str(self.version),
            },
            "spec": {
                "capabilities": list(self.capabilities),
                "metadata": _thaw(self.metadata),
                "specificationRoots": {
                    item.id: item.path for item in self.specification_roots
                },
            },
        }

    def to_json(self) -> str:
        return _canonical_json(self.as_dict())

    @property
    def sha256(self) -> str:
        return "sha256:" + sha256(self.to_json().encode("utf-8")).hexdigest()

    @classmethod
    def from_dict(cls, value: object) -> "SpecificationStoreManifest":
        raw = _mapping(value, label="SpecificationStore manifest")
        _validate_keys(raw, required=_TOP_LEVEL_KEYS, label="SpecificationStore manifest")
        if raw["apiVersion"] != SPECIFICATION_STORE_MANIFEST_API_VERSION:
            raise _fail(
                "SDAI-STORE-001",
                f"unsupported apiVersion {raw['apiVersion']!r}; expected "
                f"{SPECIFICATION_STORE_MANIFEST_API_VERSION!r}",
            )
        if raw["kind"] != "SpecificationStore":
            raise _fail("SDAI-STORE-001", "kind must be 'SpecificationStore'")
        metadata = _mapping(raw["metadata"], label="SpecificationStore metadata")
        _validate_keys(metadata, required=_METADATA_KEYS, label="SpecificationStore metadata")
        spec = _mapping(raw["spec"], label="SpecificationStore spec")
        _validate_keys(
            spec,
            required=_SPEC_REQUIRED_KEYS,
            optional=_SPEC_OPTIONAL_KEYS,
            label="SpecificationStore spec",
        )
        raw_roots = _mapping(spec["specificationRoots"], label="specificationRoots")
        roots = tuple(
            SpecificationRoot(root_id, path)  # type: ignore[arg-type]
            for root_id, path in raw_roots.items()
        )
        raw_capabilities = spec["capabilities"]
        if not isinstance(raw_capabilities, list):
            raise _fail("SDAI-STORE-001", "capabilities must be a list")
        annotations = spec.get("metadata", {})
        annotations = _mapping(annotations, label="store metadata")
        return cls(
            id=metadata["id"],
            version=_version(metadata["version"]),
            description=metadata["description"],
            specification_roots=roots,
            capabilities=tuple(raw_capabilities),
            metadata=annotations,
        )


def load_specification_store_manifest(store_root: Path) -> SpecificationStoreManifest:
    try:
        root = Path(store_root)
    except (TypeError, ValueError) as exc:
        raise _fail(
            "SDAI-STORE-002",
            "SpecificationStore root must be a valid local path",
        ) from exc
    if root.is_symlink():
        raise _fail("SDAI-STORE-002", "SpecificationStore root must not be a symlink")
    manifest_path = root / SPECIFICATION_STORE_MANIFEST_PATH
    candidate = root
    for part in PurePosixPath(SPECIFICATION_STORE_MANIFEST_PATH).parts:
        candidate = candidate / part
        if candidate.is_symlink():
            raise _fail("SDAI-STORE-002", "SpecificationStore manifest path must not contain symlinks")
    if not manifest_path.exists() or not manifest_path.is_file():
        raise _fail(
            "SDAI-STORE-002",
            f"SpecificationStore manifest not found at {SPECIFICATION_STORE_MANIFEST_PATH}",
        )
    try:
        raw = yaml.load(_read_manifest_text(manifest_path), Loader=_UniqueKeyLoader)
    except (
        OSError,
        OverflowError,
        RecursionError,
        TextEncodingError,
        ValueError,
        yaml.YAMLError,
    ) as exc:
        raise _fail("SDAI-STORE-001", "SpecificationStore manifest YAML is malformed") from exc
    manifest = SpecificationStoreManifest.from_dict(raw)
    try:
        _validate_layout(root, manifest.specification_roots)
    except SpecificationStoreError:
        raise
    except (OSError, RuntimeError, UnicodeError, ValueError) as exc:
        raise _fail("SDAI-STORE-002", "SpecificationStore layout could not be validated") from exc
    return manifest


@dataclass(frozen=True)
class SpecificationStoreSource:
    root: Path
    layer: SpecificationStoreLayer
    source: str
    locked: bool = False

    def __post_init__(self) -> None:
        layer = _layer(self.layer)
        if not isinstance(self.locked, bool):
            raise _fail("SDAI-STORE-REG-001", "store source locked must be boolean")
        if self.locked and layer not in _LOCKABLE_LAYERS:
            raise _fail(
                "SDAI-STORE-REG-005",
                "only core/org SpecificationStore sources may be authoritative locks",
            )
        try:
            root = Path(self.root)
        except (TypeError, ValueError) as exc:
            raise _fail(
                "SDAI-STORE-REG-001",
                "store source root must be a valid local path",
            ) from exc
        object.__setattr__(self, "root", root)
        object.__setattr__(self, "layer", layer)
        object.__setattr__(self, "source", _source_label(self.source))


@dataclass(frozen=True)
class SpecificationStoreProvenance:
    layer: SpecificationStoreLayer
    source: str
    path: str
    manifest_sha256: str
    locked: bool

    def __post_init__(self) -> None:
        layer = _layer(self.layer)
        if not isinstance(self.locked, bool):
            raise _fail("SDAI-STORE-REG-001", "store provenance locked must be boolean")
        if self.locked and layer not in _LOCKABLE_LAYERS:
            raise _fail("SDAI-STORE-REG-005", "store provenance lock is not authoritative")
        if self.path != SPECIFICATION_STORE_MANIFEST_PATH:
            raise _fail("SDAI-STORE-REG-001", "store provenance path is invalid")
        if not isinstance(self.manifest_sha256, str) or not re.fullmatch(
            r"sha256:[0-9a-f]{64}", self.manifest_sha256
        ):
            raise _fail("SDAI-STORE-REG-001", "store provenance hash must be SHA-256")
        object.__setattr__(self, "layer", layer)
        object.__setattr__(self, "source", _source_label(self.source))

    def as_dict(self) -> dict[str, object]:
        return {
            "layer": self.layer.value,
            "locked": self.locked,
            "manifestSha256": self.manifest_sha256,
            "path": self.path,
            "source": self.source,
        }


@dataclass(frozen=True)
class SpecificationStoreRegistration:
    manifest: SpecificationStoreManifest
    provenance: SpecificationStoreProvenance


@dataclass(frozen=True)
class ResolvedSpecificationStore:
    manifest: SpecificationStoreManifest
    selected_provenance: SpecificationStoreProvenance
    provenance: tuple[SpecificationStoreProvenance, ...]

    @property
    def id(self) -> str:
        return self.manifest.id

    @property
    def version(self) -> SemVer:
        return self.manifest.version

    @property
    def identity(self) -> str:
        return self.manifest.identity

    @property
    def manifest_sha256(self) -> str:
        return self.manifest.sha256

    def as_dict(self) -> dict[str, object]:
        return {
            "apiVersion": SPECIFICATION_STORE_RESOLUTION_API_VERSION,
            "identity": self.identity,
            "manifest": self.manifest.as_dict(),
            "manifestSha256": self.manifest_sha256,
            "provenance": [item.as_dict() for item in self.provenance],
            "selectedProvenance": self.selected_provenance.as_dict(),
        }

    def to_json(self) -> str:
        return _canonical_json(self.as_dict())


class SpecificationStoreRegistry:
    """Deterministic SemVer registry for explicit local SpecificationStore roots."""

    def __init__(self) -> None:
        self._entries: dict[
            str,
            dict[str, dict[SpecificationStoreLayer, SpecificationStoreRegistration]],
        ] = {}

    def _copy_entries(self):
        return {
            store_id: {version: dict(layers) for version, layers in versions.items()}
            for store_id, versions in self._entries.items()
        }

    def register(
        self,
        manifest: SpecificationStoreManifest,
        *,
        layer: SpecificationStoreLayer,
        source: str,
        locked: bool = False,
    ) -> SpecificationStoreRegistration:
        if not isinstance(manifest, SpecificationStoreManifest):
            raise _fail("SDAI-STORE-REG-001", "registry manifest must be a SpecificationStoreManifest")
        layer = _layer(layer)
        source = _source_label(source)
        if not isinstance(locked, bool):
            raise _fail("SDAI-STORE-REG-001", "registry locked must be boolean")
        if locked and layer not in _LOCKABLE_LAYERS:
            raise _fail(
                "SDAI-STORE-REG-005",
                "only core/org SpecificationStore definitions may be authoritative locks",
            )
        provenance = SpecificationStoreProvenance(
            layer,
            source,
            SPECIFICATION_STORE_MANIFEST_PATH,
            manifest.sha256,
            locked,
        )
        registration = SpecificationStoreRegistration(manifest, provenance)
        candidate = self._copy_entries()
        versions = candidate.setdefault(manifest.id, {})
        exact = versions.setdefault(str(manifest.version), {})
        if layer in exact:
            previous = exact[layer]
            raise _fail(
                "SDAI-STORE-REG-002",
                f"duplicate SpecificationStore '{manifest.identity}' in layer '{layer.value}' "
                f"from '{previous.provenance.source}' and '{source}'",
            )
        exact[layer] = registration
        self._validate_store(manifest.id, versions)
        self._entries = candidate
        return registration

    @staticmethod
    def _validate_store(
        store_id: str,
        versions: dict[
            str,
            dict[SpecificationStoreLayer, SpecificationStoreRegistration],
        ],
    ) -> None:
        all_entries: list[SpecificationStoreRegistration] = []
        for version, exact in versions.items():
            hashes = {item.manifest.sha256 for item in exact.values()}
            if len(hashes) > 1:
                raise _fail(
                    "SDAI-STORE-REG-003",
                    f"exact SpecificationStore '{store_id}@{version}' has conflicting canonical content",
                )
            all_entries.extend(exact.values())
        ordered = sorted(
            all_entries,
            key=lambda item: (
                item.provenance.layer.priority,
                str(item.manifest.version),
                item.provenance.source.casefold(),
                item.provenance.source,
            ),
        )
        for locked_entry in (item for item in ordered if item.provenance.locked):
            blocked = [
                item
                for item in ordered
                if item.provenance.layer.priority > locked_entry.provenance.layer.priority
            ]
            if blocked:
                raise _fail(
                    "SDAI-STORE-REG-005",
                    f"SpecificationStore '{store_id}' is locked by "
                    f"{locked_entry.provenance.layer.value}:{locked_entry.provenance.source}; "
                    f"higher layer {blocked[0].provenance.layer.value}:{blocked[0].provenance.source} "
                    "is not allowed",
                )

    def _resolve_exact(
        self,
        store_id: str,
        version: SemVer,
    ) -> ResolvedSpecificationStore | None:
        exact = self._entries.get(store_id, {}).get(str(version))
        if not exact:
            return None
        registrations = tuple(
            sorted(
                exact.values(),
                key=lambda item: (
                    item.provenance.layer.priority,
                    item.provenance.source.casefold(),
                    item.provenance.source,
                ),
            )
        )
        selected = registrations[-1]
        return ResolvedSpecificationStore(
            selected.manifest,
            selected.provenance,
            tuple(item.provenance for item in registrations),
        )

    def list_versions(self, store_id: str) -> tuple[ResolvedSpecificationStore, ...]:
        store_id = _identifier(store_id, label="store id")
        versions = [_version(item) for item in self._entries.get(store_id, {})]
        versions.sort(key=cmp_to_key(_version_compare), reverse=True)
        return tuple(
            resolved
            for version in versions
            if (resolved := self._resolve_exact(store_id, version)) is not None
        )

    def resolve(
        self,
        store_id: str,
        version: str | SemVer | None = None,
    ) -> ResolvedSpecificationStore | None:
        store_id = _identifier(store_id, label="store id")
        if version is not None:
            return self._resolve_exact(store_id, _version(version))
        versions = self.list_versions(store_id)
        if not versions:
            return None
        best = versions[0]
        same_precedence = tuple(
            item for item in versions if item.version.same_precedence(best.version)
        )
        if len(same_precedence) > 1:
            identities = ", ".join(item.identity for item in same_precedence)
            raise _fail(
                "SDAI-STORE-REG-004",
                f"latest SpecificationStore '{store_id}' is ambiguous because SemVer build "
                f"variants share precedence: {identities}; request an exact version",
            )
        return best

    def list_resolved(self) -> tuple[ResolvedSpecificationStore, ...]:
        return tuple(
            resolved
            for store_id in sorted(self._entries)
            if (resolved := self.resolve(store_id)) is not None
        )

    def list_all_exact(self) -> tuple[ResolvedSpecificationStore, ...]:
        return tuple(
            item
            for store_id in sorted(self._entries)
            for item in self.list_versions(store_id)
        )

    def search(self, query: str = "") -> tuple[ResolvedSpecificationStore, ...]:
        if not isinstance(query, str):
            raise _fail("SDAI-STORE-REG-001", "store search query must be a string")
        needle = unicodedata.normalize("NFC", query.strip()).casefold()
        if not needle:
            return self.list_resolved()
        return tuple(
            item
            for item in self.list_resolved()
            if needle
            in "\n".join(
                (item.id, item.manifest.description, *item.manifest.capabilities)
            ).casefold()
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "apiVersion": SPECIFICATION_STORE_REGISTRY_API_VERSION,
            "stores": [item.as_dict() for item in self.list_all_exact()],
        }

    def to_json(self) -> str:
        return _canonical_json(self.as_dict())

    @property
    def sha256(self) -> str:
        return _hash_json(self.as_dict())

    def __len__(self) -> int:
        return sum(len(versions) for versions in self._entries.values())


def _source_sort_key(
    source: SpecificationStoreSource,
) -> tuple[int, str, str, str, str]:
    root = str(source.root).replace("\\", "/")
    return (
        source.layer.priority,
        source.source.casefold(),
        source.source,
        root.casefold(),
        root,
    )


def build_specification_store_registry(
    sources: Iterable[SpecificationStoreSource],
) -> SpecificationStoreRegistry:
    try:
        normalized = tuple(sources)
    except TypeError as exc:
        raise _fail("SDAI-STORE-REG-001", "registry sources must be iterable") from exc
    if not all(isinstance(source, SpecificationStoreSource) for source in normalized):
        raise _fail(
            "SDAI-STORE-REG-001",
            "all registry sources must be SpecificationStoreSource values",
        )
    loaded = tuple(
        (source, load_specification_store_manifest(source.root))
        for source in sorted(normalized, key=_source_sort_key)
    )
    registry = SpecificationStoreRegistry()
    for source, manifest in loaded:
        registry.register(
            manifest,
            layer=source.layer,
            source=source.source,
            locked=source.locked,
        )
    return registry
