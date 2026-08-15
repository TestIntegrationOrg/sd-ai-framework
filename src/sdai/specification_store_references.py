from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
from typing import Mapping
import unicodedata

import yaml
from yaml.constructor import ConstructorError
from yaml.resolver import BaseResolver

from sdai.pack_manifest import PackManifestError, SemVer
from sdai.spec_changes import (
    CurrentSpecification,
    SpecChangeBundle,
    current_spec_path,
    load_current_spec,
    load_spec_change,
    validate_change_feature_id,
)
from sdai.specification_stores import (
    SPECIFICATION_STORE_MANIFEST_PATH,
    SpecificationStoreError,
    SpecificationStoreManifest,
    SpecificationStoreRegistry,
    _portable_relative_path,
    load_specification_store_manifest,
)


SPECIFICATION_STORE_REFERENCES_API_VERSION = "sdai.specification-store-references/v1"
SPECIFICATION_STORE_REFERENCE_RESOLUTION_API_VERSION = (
    "sdai.specification-store-reference-resolution/v1"
)
SPECIFICATION_STORE_CONTENT_SNAPSHOT_API_VERSION = (
    "sdai.specification-store-content-snapshot/v1"
)
SPECIFICATION_STORE_REFERENCES_PATH = ".sdai/specification-stores.yaml"
SPECIFICATION_STORE_REFERENCES_MAX_BYTES = 1024 * 1024
SPECIFICATION_STORE_CONTENT_MAX_FILES = 100_000
SPECIFICATION_STORE_CONTENT_MAX_DIRECTORIES = 100_000
SPECIFICATION_STORE_CONTENT_MAX_FILE_BYTES = 16 * 1024 * 1024
SPECIFICATION_STORE_CONTENT_MAX_TOTAL_BYTES = 256 * 1024 * 1024


class SpecificationStoreReferenceError(RuntimeError):
    """Raised when a local store reference or content snapshot is not trustworthy."""


_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_REFERENCE_TOP_LEVEL_KEYS = frozenset({"apiVersion", "kind", "references"})
_REFERENCE_KEYS = frozenset({"store", "version", "path", "content"})
_CONTENT_BINDING_KEYS = frozenset({"manifestSha256", "snapshotSha256"})


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


def _fail(code: str, message: str) -> SpecificationStoreReferenceError:
    return SpecificationStoreReferenceError(f"{code}: {message}")


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
            "SDAI-STORE-REF-001",
            "store reference data must be canonical finite JSON",
        ) from exc


def _sha256_bytes(value: bytes) -> str:
    return "sha256:" + sha256(value).hexdigest()


def _sha256_json(value: object) -> str:
    return _sha256_bytes(_canonical_json(value).encode("utf-8"))


def _mapping(value: object, *, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise _fail("SDAI-STORE-REF-001", f"{label} must be a string-keyed mapping")
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
            "SDAI-STORE-REF-001",
            f"{label} contains unsupported field(s): {', '.join(unknown)}",
        )
    if missing:
        raise _fail(
            "SDAI-STORE-REF-001",
            f"{label} is missing required field(s): {', '.join(missing)}",
        )


def _hash(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise _fail(
            "SDAI-STORE-REF-001",
            f"{label} must be a lowercase SHA-256 digest",
        )
    return value


def _store_id(value: object) -> str:
    if not isinstance(value, str):
        raise _fail("SDAI-STORE-REF-001", "reference store must be a string")
    candidate = unicodedata.normalize("NFC", value.strip())
    if not re.fullmatch(r"^[a-z][a-z0-9]*(?:[-_.][a-z0-9]+)*$", candidate):
        raise _fail(
            "SDAI-STORE-REF-001",
            "reference store must be a portable lowercase identifier",
        )
    return candidate


def _version(value: object) -> SemVer:
    try:
        if isinstance(value, SemVer):
            candidate = str(value)
        elif isinstance(value, str):
            candidate = value
        else:
            raise TypeError("version must be text")
        if len(candidate) > 256:
            raise ValueError("version is too long")
        return SemVer.parse(candidate)
    except (PackManifestError, TypeError, ValueError) as exc:
        raise _fail(
            "SDAI-STORE-REF-001",
            "reference version must be an exact valid SemVer",
        ) from exc


def _contains_surrogate(value: str) -> bool:
    return any(0xD800 <= ord(character) <= 0xDFFF for character in value)


def _declared_path(value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise _fail(
            "SDAI-STORE-REF-002",
            "reference path must be explicit non-empty local path text",
        )
    normalized = unicodedata.normalize("NFC", value)
    if (
        "\x00" in normalized
        or _contains_surrogate(normalized)
        or len(normalized.encode("utf-8")) > 4096
        or any(ord(character) < 32 for character in normalized)
    ):
        raise _fail(
            "SDAI-STORE-REF-002",
            "reference path contains invalid or oversized local path text",
        )
    return normalized


def _is_redirect(path: Path, *, label: str) -> bool:
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        if is_junction is not None and is_junction():
            return True
        try:
            attributes = getattr(path.lstat(), "st_file_attributes", 0)
        except FileNotFoundError:
            return False
        reparse_point = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)
        return bool(attributes & reparse_point)
    except (OSError, UnicodeError, ValueError) as exc:
        raise _fail(
            "SDAI-STORE-REF-002",
            f"{label} redirect status could not be verified",
        ) from exc


def _reject_redirect_chain(path: Path, *, label: str) -> None:
    absolute = path.absolute()
    anchor = Path(absolute.anchor)
    candidate = anchor
    for part in absolute.parts[1:]:
        candidate = candidate / part
        if _is_redirect(candidate, label=label):
            raise _fail(
                "SDAI-STORE-REF-002",
                f"{label} must not contain a symlink, junction, or reparse point",
            )


def _resolve_existing_directory(project_root: Path, declared: str) -> Path:
    candidate = Path(declared)
    if not candidate.is_absolute():
        candidate = project_root / candidate
    _reject_redirect_chain(candidate, label="SpecificationStore reference path")
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError, UnicodeError, ValueError) as exc:
        raise _fail(
            "SDAI-STORE-REF-002",
            "SpecificationStore reference path must be an explicit existing local directory",
        ) from exc
    if not resolved.is_dir():
        raise _fail(
            "SDAI-STORE-REF-002",
            "SpecificationStore reference path must be an explicit existing local directory",
        )
    return resolved


def _resolve_declaration_file(project_root: Path, path: Path | None) -> Path:
    try:
        candidate = Path(path) if path is not None else project_root / SPECIFICATION_STORE_REFERENCES_PATH
    except (TypeError, ValueError) as exc:
        raise _fail("SDAI-STORE-REF-002", "reference declaration path is invalid") from exc
    if not candidate.is_absolute():
        candidate = project_root / candidate
    _reject_redirect_chain(candidate, label="store reference declaration")
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError, UnicodeError, ValueError) as exc:
        raise _fail(
            "SDAI-STORE-REF-002",
            f"store reference declaration not found at {SPECIFICATION_STORE_REFERENCES_PATH}",
        ) from exc
    try:
        resolved.relative_to(project_root)
    except ValueError as exc:
        raise _fail(
            "SDAI-STORE-REF-002",
            "reference declaration must stay inside the project",
        ) from exc
    if not resolved.is_file():
        raise _fail(
            "SDAI-STORE-REF-002",
            f"store reference declaration not found at {SPECIFICATION_STORE_REFERENCES_PATH}",
        )
    return resolved


def _read_bounded_utf8(path: Path, *, label: str) -> tuple[bytes, str]:
    try:
        with path.open("rb") as stream:
            data = stream.read(SPECIFICATION_STORE_REFERENCES_MAX_BYTES + 1)
    except (OSError, UnicodeError, ValueError) as exc:
        raise _fail("SDAI-STORE-REF-002", f"unable to read {label}") from exc
    if len(data) > SPECIFICATION_STORE_REFERENCES_MAX_BYTES:
        raise _fail("SDAI-STORE-REF-001", f"{label} exceeds the 1 MiB input limit")
    try:
        decoded = data.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError as exc:
        raise _fail("SDAI-STORE-REF-001", f"{label} is not valid UTF-8") from exc
    return data, decoded.replace("\r\n", "\n").replace("\r", "\n")


@dataclass(frozen=True)
class SpecificationStoreContentBinding:
    manifest_sha256: str
    snapshot_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "manifest_sha256",
            _hash(self.manifest_sha256, label="content.manifestSha256"),
        )
        object.__setattr__(
            self,
            "snapshot_sha256",
            _hash(self.snapshot_sha256, label="content.snapshotSha256"),
        )

    def as_dict(self) -> dict[str, str]:
        return {
            "manifestSha256": self.manifest_sha256,
            "snapshotSha256": self.snapshot_sha256,
        }

    @classmethod
    def from_dict(cls, value: object) -> "SpecificationStoreContentBinding":
        raw = _mapping(value, label="reference content binding")
        _keys(raw, expected=_CONTENT_BINDING_KEYS, label="reference content binding")
        return cls(
            manifest_sha256=raw["manifestSha256"],  # type: ignore[arg-type]
            snapshot_sha256=raw["snapshotSha256"],  # type: ignore[arg-type]
        )


@dataclass(frozen=True)
class SpecificationStoreReference:
    store: str
    version: SemVer
    path: str
    content: SpecificationStoreContentBinding | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "store", _store_id(self.store))
        object.__setattr__(self, "version", _version(self.version))
        object.__setattr__(self, "path", _declared_path(self.path))
        if self.content is not None and not isinstance(
            self.content, SpecificationStoreContentBinding
        ):
            raise _fail(
                "SDAI-STORE-REF-001",
                "reference content must be a SpecificationStoreContentBinding",
            )

    @property
    def identity(self) -> str:
        return f"{self.store}@{self.version}"

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "path": self.path,
            "store": self.store,
            "version": str(self.version),
        }
        if self.content is not None:
            payload["content"] = self.content.as_dict()
        return payload

    @classmethod
    def from_dict(cls, value: object) -> "SpecificationStoreReference":
        raw = _mapping(value, label="SpecificationStore reference")
        allowed = _REFERENCE_KEYS if "content" in raw else _REFERENCE_KEYS - {"content"}
        _keys(raw, expected=allowed, label="SpecificationStore reference")
        return cls(
            store=raw["store"],  # type: ignore[arg-type]
            version=_version(raw["version"]),
            path=raw["path"],  # type: ignore[arg-type]
            content=(
                SpecificationStoreContentBinding.from_dict(raw["content"])
                if "content" in raw
                else None
            ),
        )


@dataclass(frozen=True)
class SpecificationStoreReferenceSet:
    references: tuple[SpecificationStoreReference, ...]
    source_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.references, (list, tuple)) or not self.references:
            raise _fail(
                "SDAI-STORE-REF-001",
                "references must be a non-empty materialized list",
            )
        if not all(isinstance(item, SpecificationStoreReference) for item in self.references):
            raise _fail(
                "SDAI-STORE-REF-001",
                "references must contain only SpecificationStoreReference values",
            )
        ordered = tuple(
            sorted(
                self.references,
                key=lambda item: (item.store, str(item.version), item.path.casefold(), item.path),
            )
        )
        identities = [item.identity for item in ordered]
        if len(set(identities)) != len(identities):
            raise _fail(
                "SDAI-STORE-REF-003",
                "project references contain a duplicate exact store identity",
            )
        paths = [item.path.casefold() for item in ordered]
        if len(set(paths)) != len(paths):
            raise _fail(
                "SDAI-STORE-REF-003",
                "project references contain a duplicate declared path",
            )
        object.__setattr__(self, "references", ordered)
        object.__setattr__(
            self,
            "source_sha256",
            _hash(self.source_sha256, label="reference source sha256"),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "apiVersion": SPECIFICATION_STORE_REFERENCES_API_VERSION,
            "kind": "SpecificationStoreReferences",
            "references": [item.as_dict() for item in self.references],
        }

    def to_json(self) -> str:
        return _canonical_json(self.as_dict())

    @classmethod
    def from_dict(
        cls,
        value: object,
        *,
        source_sha256: str,
    ) -> "SpecificationStoreReferenceSet":
        raw = _mapping(value, label="SpecificationStore references")
        _keys(raw, expected=_REFERENCE_TOP_LEVEL_KEYS, label="SpecificationStore references")
        if raw["apiVersion"] != SPECIFICATION_STORE_REFERENCES_API_VERSION:
            raise _fail("SDAI-STORE-REF-001", "unsupported store references apiVersion")
        if raw["kind"] != "SpecificationStoreReferences":
            raise _fail(
                "SDAI-STORE-REF-001",
                "store references kind must be 'SpecificationStoreReferences'",
            )
        items = raw["references"]
        if not isinstance(items, list) or not items or len(items) > 1024:
            raise _fail(
                "SDAI-STORE-REF-001",
                "references must be a non-empty list with at most 1024 entries",
            )
        return cls(
            tuple(SpecificationStoreReference.from_dict(item) for item in items),
            source_sha256,
        )


@dataclass(frozen=True)
class SpecificationStoreContentEntry:
    root: str
    path: str
    sha256: str
    size: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", _store_id(self.root))
        object.__setattr__(
            self,
            "path",
            _portable_relative_path(self.path, label="SpecificationStore content path"),
        )
        object.__setattr__(self, "sha256", _hash(self.sha256, label="content file sha256"))
        if (
            not isinstance(self.size, int)
            or isinstance(self.size, bool)
            or self.size < 0
            or self.size > SPECIFICATION_STORE_CONTENT_MAX_FILE_BYTES
        ):
            raise _fail(
                "SDAI-STORE-REF-006",
                "content file size is outside the supported read-only snapshot bound",
            )

    def as_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "root": self.root,
            "sha256": self.sha256,
            "size": self.size,
        }


@dataclass(frozen=True)
class SpecificationStoreContentSnapshot:
    identity: str
    manifest_sha256: str
    manifest_file_sha256: str
    entries: tuple[SpecificationStoreContentEntry, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.identity, str) or not self.identity:
            raise _fail("SDAI-STORE-REF-001", "snapshot identity must be non-empty")
        object.__setattr__(
            self,
            "manifest_sha256",
            _hash(self.manifest_sha256, label="snapshot manifestSha256"),
        )
        object.__setattr__(
            self,
            "manifest_file_sha256",
            _hash(self.manifest_file_sha256, label="snapshot manifestFileSha256"),
        )
        if not isinstance(self.entries, (list, tuple)) or not all(
            isinstance(item, SpecificationStoreContentEntry) for item in self.entries
        ):
            raise _fail(
                "SDAI-STORE-REF-001",
                "snapshot entries must be a materialized content-entry list",
            )
        ordered = tuple(sorted(self.entries, key=lambda item: (item.path, item.root)))
        paths = [item.path for item in ordered]
        if len(set(paths)) != len(paths):
            raise _fail(
                "SDAI-STORE-REF-003",
                "snapshot contains a duplicate content path across roots",
            )
        folded = [path.casefold() for path in paths]
        if len(set(folded)) != len(folded):
            raise _fail(
                "SDAI-STORE-REF-003",
                "snapshot contains case-insensitive content path collisions",
            )
        object.__setattr__(self, "entries", ordered)

    def as_dict(self) -> dict[str, object]:
        return {
            "apiVersion": SPECIFICATION_STORE_CONTENT_SNAPSHOT_API_VERSION,
            "files": [item.as_dict() for item in self.entries],
            "identity": self.identity,
            "manifestFileSha256": self.manifest_file_sha256,
            "manifestSha256": self.manifest_sha256,
        }

    def to_json(self) -> str:
        return _canonical_json(self.as_dict())

    @property
    def sha256(self) -> str:
        return _sha256_json(self.as_dict())

    def entry(self, source: str) -> SpecificationStoreContentEntry | None:
        return next((item for item in self.entries if item.path == source), None)


@dataclass(frozen=True)
class SpecificationStoreReadProvenance:
    store_identity: str
    manifest_sha256: str
    snapshot_sha256: str
    content: tuple[SpecificationStoreContentEntry, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "content": [item.as_dict() for item in self.content],
            "manifestSha256": self.manifest_sha256,
            "snapshotSha256": self.snapshot_sha256,
            "storeIdentity": self.store_identity,
        }


@dataclass(frozen=True)
class ReferencedCurrentSpecification:
    specification: CurrentSpecification
    provenance: SpecificationStoreReadProvenance


@dataclass(frozen=True)
class ReferencedSpecChange:
    change: SpecChangeBundle
    provenance: SpecificationStoreReadProvenance


@dataclass(frozen=True)
class ResolvedSpecificationStoreReference:
    reference: SpecificationStoreReference
    root: Path
    manifest: SpecificationStoreManifest
    snapshot: SpecificationStoreContentSnapshot
    ordinal: int

    @property
    def identity(self) -> str:
        return self.manifest.identity

    def as_dict(self) -> dict[str, object]:
        return {
            "apiVersion": SPECIFICATION_STORE_REFERENCE_RESOLUTION_API_VERSION,
            "ordinal": self.ordinal,
            "reference": self.reference.as_dict(),
            "snapshot": self.snapshot.as_dict(),
            "snapshotSha256": self.snapshot.sha256,
            "source": SPECIFICATION_STORE_REFERENCES_PATH,
        }

    def to_json(self) -> str:
        return _canonical_json(self.as_dict())

    def verify_unchanged(self) -> None:
        try:
            current_manifest = load_specification_store_manifest(self.root)
        except SpecificationStoreError as exc:
            raise _fail(
                "SDAI-STORE-REF-004",
                f"referenced store '{self.identity}' manifest is missing, invalid, or stale",
            ) from exc
        if current_manifest.sha256 != self.manifest.sha256:
            raise _fail(
                "SDAI-STORE-REF-004",
                f"referenced store '{self.identity}' manifest is stale",
            )
        current = _build_stable_content_snapshot(self.root, current_manifest)
        if current.sha256 != self.snapshot.sha256:
            raise _fail(
                "SDAI-STORE-REF-004",
                f"referenced store '{self.identity}' content is dirty or stale",
            )

    def _root_path(self, root_id: str, capability: str) -> str:
        if capability not in self.manifest.capabilities:
            raise _fail(
                "SDAI-STORE-REF-007",
                f"referenced store '{self.identity}' does not declare capability '{capability}'",
            )
        selected = next(
            (item.path for item in self.manifest.specification_roots if item.id == root_id),
            None,
        )
        if selected is None:
            raise _fail(
                "SDAI-STORE-REF-007",
                f"referenced store '{self.identity}' does not declare root '{root_id}'",
            )
        return selected

    def _provenance(self, sources: tuple[str, ...]) -> SpecificationStoreReadProvenance:
        entries: list[SpecificationStoreContentEntry] = []
        for source in sorted(set(sources)):
            entry = self.snapshot.entry(source)
            if entry is None:
                raise _fail(
                    "SDAI-STORE-REF-004",
                    f"referenced content '{source}' is missing from the bound snapshot",
                )
            entries.append(entry)
        return SpecificationStoreReadProvenance(
            self.identity,
            self.manifest.sha256,
            self.snapshot.sha256,
            tuple(entries),
        )

    def read_current(self, domain: str) -> ReferencedCurrentSpecification:
        specification_root = self._root_path("current", "current-specifications")
        self.verify_unchanged()
        source = current_spec_path(
            self.root,
            domain,
            specification_root=specification_root,
        ).relative_to(self.root).as_posix()
        entry = self.snapshot.entry(source)
        if entry is None:
            raise _fail(
                "SDAI-STORE-REF-004",
                f"referenced content '{source}' is missing from the bound snapshot",
            )
        specification = load_current_spec(
            self.root,
            domain,
            specification_root=specification_root,
            expected_file_sha256=entry.sha256,
        )
        self.verify_unchanged()
        return ReferencedCurrentSpecification(
            specification,
            self._provenance((specification.source,)),
        )

    def read_change(self, feature_id: str) -> ReferencedSpecChange:
        changes_root = self._root_path("changes", "changes")
        feature = validate_change_feature_id(feature_id)
        delta_parent = PurePosixPath(changes_root) / feature / "deltas"
        expected_delta_sources = tuple(
            entry.path
            for entry in self.snapshot.entries
            if PurePosixPath(entry.path).parent == delta_parent
            and PurePosixPath(entry.path).suffix in {".yaml", ".yml"}
        )
        self.verify_unchanged()
        change = load_spec_change(
            self.root,
            feature,
            changes_root=changes_root,
            expected_file_sha256_by_source={
                entry.path: entry.sha256 for entry in self.snapshot.entries
            },
            expected_delta_sources=expected_delta_sources,
        )
        self.verify_unchanged()
        sources = (
            change.metadata.source,
            *(delta.source for delta in change.deltas),
        )
        return ReferencedSpecChange(change, self._provenance(tuple(sources)))


@dataclass(frozen=True)
class ResolvedSpecificationStoreReferences:
    references: tuple[ResolvedSpecificationStoreReference, ...]
    source_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "source_sha256",
            _hash(self.source_sha256, label="resolved reference source sha256"),
        )

    def get(
        self,
        store: str,
        version: str | SemVer | None = None,
    ) -> ResolvedSpecificationStoreReference | None:
        store_id = _store_id(store)
        exact = _version(version) if version is not None else None
        matches = tuple(
            item
            for item in self.references
            if item.manifest.id == store_id
            and (exact is None or str(item.manifest.version) == str(exact))
        )
        if not matches:
            return None
        if len(matches) > 1:
            raise _fail(
                "SDAI-STORE-REF-003",
                f"store '{store_id}' has more than one referenced version; request an exact version",
            )
        return matches[0]

    def as_dict(self) -> dict[str, object]:
        return {
            "apiVersion": SPECIFICATION_STORE_REFERENCE_RESOLUTION_API_VERSION,
            "references": [item.as_dict() for item in self.references],
            "sourceSha256": self.source_sha256,
        }

    def to_json(self) -> str:
        return _canonical_json(self.as_dict())

    @property
    def sha256(self) -> str:
        return _sha256_json(self.as_dict())


def load_specification_store_references(
    project_root: Path,
    path: Path | None = None,
) -> SpecificationStoreReferenceSet:
    try:
        root = Path(project_root).resolve(strict=True)
    except (OSError, RuntimeError, TypeError, UnicodeError, ValueError) as exc:
        raise _fail(
            "SDAI-STORE-REF-002",
            "project root must be an explicit existing local directory",
        ) from exc
    if not root.is_dir():
        raise _fail(
            "SDAI-STORE-REF-002",
            "project root must be an explicit existing local directory",
        )
    reference_path = _resolve_declaration_file(root, path)
    data, text = _read_bounded_utf8(reference_path, label="store reference declaration")
    try:
        if any(isinstance(event, yaml.events.AliasEvent) for event in yaml.parse(text)):
            raise _fail(
                "SDAI-STORE-REF-001",
                "store reference declaration must not contain YAML aliases",
            )
        raw = yaml.load(text, Loader=_UniqueKeyLoader)
    except SpecificationStoreReferenceError:
        raise
    except (OverflowError, RecursionError, ValueError, yaml.YAMLError) as exc:
        raise _fail(
            "SDAI-STORE-REF-001",
            "store reference declaration YAML is malformed",
        ) from exc
    return SpecificationStoreReferenceSet.from_dict(
        raw,
        source_sha256=_sha256_bytes(data),
    )


def _hash_file(path: Path, *, label: str) -> tuple[str, int]:
    try:
        if _is_redirect(path, label=label):
            raise _fail("SDAI-STORE-REF-002", f"{label} must not be a filesystem redirect")
        before = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode):
            raise _fail("SDAI-STORE-REF-006", f"{label} must be a regular file")
        if before.st_size > SPECIFICATION_STORE_CONTENT_MAX_FILE_BYTES:
            raise _fail(
                "SDAI-STORE-REF-006",
                f"{label} exceeds the 16 MiB per-file snapshot limit",
            )
        digest = sha256()
        size = 0
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as stream:
            opened_before = os.fstat(stream.fileno())
            while chunk := stream.read(64 * 1024):
                size += len(chunk)
                if size > SPECIFICATION_STORE_CONTENT_MAX_FILE_BYTES:
                    raise _fail(
                        "SDAI-STORE-REF-006",
                        f"{label} exceeds the 16 MiB per-file snapshot limit",
                    )
                digest.update(chunk)
            opened_after = os.fstat(stream.fileno())
        after = path.stat(follow_symlinks=False)
    except SpecificationStoreReferenceError:
        raise
    except (OSError, UnicodeError, ValueError) as exc:
        raise _fail("SDAI-STORE-REF-006", f"unable to inspect {label}") from exc
    tokens_before = (
        before.st_dev,
        before.st_ino,
        before.st_mode,
        before.st_size,
        before.st_mtime_ns,
    )
    tokens_opened_before = (
        opened_before.st_dev,
        opened_before.st_ino,
        opened_before.st_mode,
        opened_before.st_size,
        opened_before.st_mtime_ns,
    )
    tokens_opened_after = (
        opened_after.st_dev,
        opened_after.st_ino,
        opened_after.st_mode,
        opened_after.st_size,
        opened_after.st_mtime_ns,
    )
    tokens_after = (
        after.st_dev,
        after.st_ino,
        after.st_mode,
        after.st_size,
        after.st_mtime_ns,
    )
    if (
        tokens_before != tokens_opened_before
        or tokens_opened_before != tokens_opened_after
        or tokens_opened_after != tokens_after
        or size != after.st_size
    ):
        raise _fail("SDAI-STORE-REF-005", f"{label} mutated during read-only inspection")
    return "sha256:" + digest.hexdigest(), size


def _walk_error(error: OSError) -> None:
    raise _fail(
        "SDAI-STORE-REF-006",
        "unable to completely inspect declared SpecificationStore content",
    ) from error


def _scan_content_once(
    store_root: Path,
    manifest: SpecificationStoreManifest,
) -> tuple[SpecificationStoreContentEntry, ...]:
    entries: list[SpecificationStoreContentEntry] = []
    total_size = 0
    directory_count = 0
    for declared_root in manifest.specification_roots:
        content_root = store_root.joinpath(*PurePosixPath(declared_root.path).parts)
        _reject_redirect_chain(
            content_root,
            label=f"specification root '{declared_root.id}'",
        )
        for current, directories, files in os.walk(
            content_root,
            topdown=True,
            onerror=_walk_error,
            followlinks=False,
        ):
            current_path = Path(current)
            directory_count += 1
            if directory_count > SPECIFICATION_STORE_CONTENT_MAX_DIRECTORIES:
                raise _fail(
                    "SDAI-STORE-REF-006",
                    "SpecificationStore content exceeds the 100000-directory snapshot limit",
                )
            _portable_relative_path(
                current_path.relative_to(store_root).as_posix(),
                label="SpecificationStore content directory",
            )
            if _is_redirect(current_path, label="SpecificationStore content directory"):
                raise _fail(
                    "SDAI-STORE-REF-002",
                    "SpecificationStore content directories must not be filesystem redirects",
                )
            safe_directories: list[str] = []
            for directory in sorted(directories, key=lambda item: (item.casefold(), item)):
                child = current_path / directory
                _portable_relative_path(
                    child.relative_to(store_root).as_posix(),
                    label="SpecificationStore content directory",
                )
                if _is_redirect(child, label="SpecificationStore content directory"):
                    raise _fail(
                        "SDAI-STORE-REF-002",
                        "SpecificationStore content directories must not be filesystem redirects",
                    )
                safe_directories.append(directory)
            directories[:] = safe_directories
            for filename in sorted(files, key=lambda item: (item.casefold(), item)):
                path = current_path / filename
                if _is_redirect(path, label="SpecificationStore content file"):
                    raise _fail(
                        "SDAI-STORE-REF-002",
                        "SpecificationStore content files must not be filesystem redirects",
                    )
                relative = _portable_relative_path(
                    path.relative_to(store_root).as_posix(),
                    label="SpecificationStore content path",
                )
                digest, size = _hash_file(path, label=f"content file '{relative}'")
                total_size += size
                if total_size > SPECIFICATION_STORE_CONTENT_MAX_TOTAL_BYTES:
                    raise _fail(
                        "SDAI-STORE-REF-006",
                        "SpecificationStore content exceeds the 256 MiB snapshot limit",
                    )
                entries.append(
                    SpecificationStoreContentEntry(
                        root=declared_root.id,
                        path=relative,
                        sha256=digest,
                        size=size,
                    )
                )
                if len(entries) > SPECIFICATION_STORE_CONTENT_MAX_FILES:
                    raise _fail(
                        "SDAI-STORE-REF-006",
                        "SpecificationStore content exceeds the 100000-file snapshot limit",
                    )
    return tuple(entries)


def _manifest_file_sha256(store_root: Path) -> str:
    path = store_root / SPECIFICATION_STORE_MANIFEST_PATH
    if _is_redirect(path, label="SpecificationStore manifest"):
        raise _fail(
            "SDAI-STORE-REF-002",
            "SpecificationStore manifest must not be a filesystem redirect",
        )
    digest, _ = _hash_file(path, label="SpecificationStore manifest")
    return digest


def _build_content_snapshot_once(
    store_root: Path,
    manifest: SpecificationStoreManifest,
) -> SpecificationStoreContentSnapshot:
    manifest_file_before = _manifest_file_sha256(store_root)
    entries = _scan_content_once(store_root, manifest)
    manifest_file_after = _manifest_file_sha256(store_root)
    try:
        reloaded = load_specification_store_manifest(store_root)
    except SpecificationStoreError as exc:
        raise _fail(
            "SDAI-STORE-REF-005",
            f"referenced store '{manifest.identity}' mutated during read-only inspection",
        ) from exc
    if (
        manifest_file_before != manifest_file_after
        or reloaded.sha256 != manifest.sha256
    ):
        raise _fail(
            "SDAI-STORE-REF-005",
            f"referenced store '{manifest.identity}' mutated during read-only inspection",
        )
    return SpecificationStoreContentSnapshot(
        manifest.identity,
        manifest.sha256,
        manifest_file_after,
        entries,
    )


def _build_stable_content_snapshot(
    store_root: Path,
    manifest: SpecificationStoreManifest,
) -> SpecificationStoreContentSnapshot:
    first = _build_content_snapshot_once(store_root, manifest)
    second = _build_content_snapshot_once(store_root, manifest)
    if first.to_json() != second.to_json():
        raise _fail(
            "SDAI-STORE-REF-005",
            f"referenced store '{manifest.identity}' mutated during read-only inspection",
        )
    return second


def _validate_root_overlaps(
    resolved: tuple[tuple[SpecificationStoreReference, Path], ...],
) -> None:
    keyed = sorted(
        tuple(part.casefold() for part in root.parts)
        for _, root in resolved
    )
    for parent, candidate in zip(keyed, keyed[1:], strict=False):
        if candidate[: len(parent)] == parent:
            raise _fail(
                "SDAI-STORE-REF-003",
                "resolved SpecificationStore reference paths must not duplicate or overlap",
            )


def resolve_specification_store_references(
    project_root: Path,
    registry: SpecificationStoreRegistry | None = None,
    *,
    path: Path | None = None,
) -> ResolvedSpecificationStoreReferences:
    reference_set = load_specification_store_references(project_root, path)
    root = Path(project_root).resolve(strict=True)
    declarations_path = _resolve_declaration_file(root, path)
    resolved_paths = tuple(
        (
            reference,
            _resolve_existing_directory(root, reference.path),
        )
        for reference in reference_set.references
    )
    _validate_root_overlaps(resolved_paths)

    resolved: list[ResolvedSpecificationStoreReference] = []
    for ordinal, (reference, store_root) in enumerate(resolved_paths, start=1):
        try:
            manifest = load_specification_store_manifest(store_root)
        except SpecificationStoreError as exc:
            raise _fail(
                "SDAI-STORE-REF-004",
                f"reference '{reference.identity}' does not resolve to a valid SpecificationStore",
            ) from exc
        if manifest.id != reference.store or str(manifest.version) != str(reference.version):
            raise _fail(
                "SDAI-STORE-REF-004",
                f"reference '{reference.identity}' does not match the manifest at its declared path",
            )
        if registry is not None:
            selected = registry.resolve(reference.store, reference.version)
            if selected is None or selected.manifest_sha256 != manifest.sha256:
                raise _fail(
                    "SDAI-STORE-REF-004",
                    f"reference '{reference.identity}' is missing or stale in the store registry",
                )
        snapshot = _build_stable_content_snapshot(store_root, manifest)
        if reference.content is not None and (
            reference.content.manifest_sha256 != manifest.sha256
            or reference.content.snapshot_sha256 != snapshot.sha256
        ):
            raise _fail(
                "SDAI-STORE-REF-004",
                f"reference '{reference.identity}' content binding is stale",
            )
        resolved.append(
            ResolvedSpecificationStoreReference(
                reference,
                store_root,
                manifest,
                snapshot,
                ordinal,
            )
        )

    current_source, _ = _read_bounded_utf8(
        declarations_path,
        label="store reference declaration",
    )
    if _sha256_bytes(current_source) != reference_set.source_sha256:
        raise _fail(
            "SDAI-STORE-REF-005",
            "store reference declaration mutated during read-only inspection",
        )
    return ResolvedSpecificationStoreReferences(
        tuple(resolved),
        reference_set.source_sha256,
    )
