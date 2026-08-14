from __future__ import annotations

from dataclasses import dataclass
from functools import cmp_to_key
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import re
from typing import Iterable
import unicodedata

from sdai.extensions.registry import RegistryLayer
from sdai.integration_manifest import IntegrationManifest, load_integration_manifest
from sdai.pack_manifest import PackManifestError, SemVer


INTEGRATION_REGISTRY_API_VERSION = "sdai.integration-registry/v1"
INTEGRATION_RESOLUTION_API_VERSION = "sdai.integration-resolution/v1"


class IntegrationRegistryError(RuntimeError):
    """Raised when Integration discovery or resolution would be ambiguous or unsafe."""


_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9]*(?:[-_.][a-z0-9]+)*$")
_MANIFEST_SUFFIXES = (".integration.yaml", ".integration.yml", ".integration.json")
_LOCKABLE_LAYERS = frozenset({RegistryLayer.BUILTIN, RegistryLayer.ORG})
_WINDOWS_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
)
_WINDOWS_FORBIDDEN = frozenset('<>:"|?*')


def _fail(code: str, message: str) -> IntegrationRegistryError:
    return IntegrationRegistryError(f"{code}: {message}")


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
        raise _fail("SDAI-INTEGRATION-REG-001", "registry data is not canonical finite JSON") from exc


def _layer(value: RegistryLayer) -> RegistryLayer:
    try:
        return RegistryLayer(value)
    except ValueError as exc:
        supported = ", ".join(item.value for item in RegistryLayer)
        raise _fail(
            "SDAI-INTEGRATION-REG-001",
            f"unknown registry layer {value!r}; supported layers: {supported}",
        ) from exc


def _source_label(value: str) -> str:
    if not isinstance(value, str):
        raise _fail("SDAI-INTEGRATION-REG-001", "source label must be a string")
    text = unicodedata.normalize("NFC", value.strip())
    if not text or "\x00" in text:
        raise _fail("SDAI-INTEGRATION-REG-001", "source label must be non-empty and contain no NUL")
    return text


def _integration_id(value: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER_RE.fullmatch(value):
        raise _fail(
            "SDAI-INTEGRATION-REG-001",
            f"integration id {value!r} is not a portable lowercase identifier",
        )
    return value


def _relative_manifest_path(value: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise _fail("SDAI-INTEGRATION-REG-001", "manifest provenance path must be a safe POSIX relative path")
    normalized = unicodedata.normalize("NFC", value)
    if normalized != normalized.strip():
        raise _fail("SDAI-INTEGRATION-REG-001", "manifest provenance path must not contain surrounding whitespace")
    path = PurePosixPath(normalized)
    parts = normalized.split("/")
    if path.is_absolute() or any(part in {"", ".", ".."} for part in parts):
        raise _fail("SDAI-INTEGRATION-REG-001", "manifest provenance path must be a safe POSIX relative path")
    for part in parts:
        if part != part.strip() or any(ord(char) < 32 for char in part):
            raise _fail("SDAI-INTEGRATION-REG-001", "manifest provenance path contains a non-portable segment")
        if any(char in _WINDOWS_FORBIDDEN for char in part) or part.endswith("."):
            raise _fail("SDAI-INTEGRATION-REG-001", "manifest provenance path is not portable across filesystems")
        if part.split(".", 1)[0].upper() in _WINDOWS_RESERVED:
            raise _fail("SDAI-INTEGRATION-REG-001", "manifest provenance path uses a reserved Windows segment")
    return path.as_posix()


def _sha256(value: str) -> str:
    if not isinstance(value, str) or not value.startswith("sha256:"):
        raise _fail("SDAI-INTEGRATION-REG-001", "manifest provenance hash must be a SHA-256 digest")
    digest = value.removeprefix("sha256:")
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise _fail("SDAI-INTEGRATION-REG-001", "manifest provenance hash must be a lowercase SHA-256 digest")
    return value


def _version(value: str | SemVer) -> SemVer:
    if isinstance(value, SemVer):
        return value
    try:
        return SemVer.parse(value)
    except PackManifestError as exc:
        raise _fail("SDAI-INTEGRATION-REG-001", f"invalid Integration version {value!r}") from exc


def _version_compare(left: SemVer, right: SemVer) -> int:
    precedence = left.compare_precedence(right)
    if precedence != 0:
        return precedence
    left_text = str(left)
    right_text = str(right)
    if left_text == right_text:
        return 0
    return -1 if left_text < right_text else 1


def _portable_fs_key(path: Path) -> tuple[str, str]:
    value = str(path).replace("\\", "/")
    return value.casefold(), value


@dataclass(frozen=True)
class IntegrationSource:
    """One deterministic discovery root and the authority layer that owns it."""

    root: Path
    layer: RegistryLayer
    source: str
    locked: bool = False

    def __post_init__(self) -> None:
        layer = _layer(self.layer)
        if not isinstance(self.locked, bool):
            raise _fail("SDAI-INTEGRATION-REG-001", "Integration source locked must be a boolean")
        if self.locked and layer not in _LOCKABLE_LAYERS:
            allowed = ", ".join(item.value for item in sorted(_LOCKABLE_LAYERS, key=lambda item: item.priority))
            raise _fail(
                "SDAI-INTEGRATION-REG-005",
                f"locked Integration sources are allowed only in authoritative layers: {allowed}",
            )
        object.__setattr__(self, "root", Path(self.root))
        object.__setattr__(self, "layer", layer)
        object.__setattr__(self, "source", _source_label(self.source))


@dataclass(frozen=True)
class IntegrationProvenance:
    layer: RegistryLayer
    source: str
    path: str
    manifest_sha256: str
    locked: bool

    def __post_init__(self) -> None:
        layer = _layer(self.layer)
        if not isinstance(self.locked, bool):
            raise _fail("SDAI-INTEGRATION-REG-001", "Integration provenance locked must be a boolean")
        if self.locked and layer not in _LOCKABLE_LAYERS:
            raise _fail("SDAI-INTEGRATION-REG-005", "Integration provenance lock is not authoritative")
        object.__setattr__(self, "layer", layer)
        object.__setattr__(self, "source", _source_label(self.source))
        object.__setattr__(self, "path", _relative_manifest_path(self.path))
        object.__setattr__(self, "manifest_sha256", _sha256(self.manifest_sha256))

    def as_dict(self) -> dict[str, object]:
        return {
            "layer": self.layer.value,
            "locked": self.locked,
            "manifestSha256": self.manifest_sha256,
            "path": self.path,
            "source": self.source,
        }


@dataclass(frozen=True)
class IntegrationRegistration:
    manifest: IntegrationManifest
    provenance: IntegrationProvenance


@dataclass(frozen=True)
class ResolvedIntegration:
    manifest: IntegrationManifest
    selected_provenance: IntegrationProvenance
    provenance: tuple[IntegrationProvenance, ...]

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
            "apiVersion": INTEGRATION_RESOLUTION_API_VERSION,
            "identity": self.identity,
            "manifest": self.manifest.as_dict(),
            "manifestSha256": self.manifest.sha256,
            "provenance": [item.as_dict() for item in self.provenance],
            "selectedProvenance": self.selected_provenance.as_dict(),
        }

    def to_json(self) -> str:
        return _canonical_json(self.as_dict())


class IntegrationRegistry:
    """Version-aware deterministic registry for declarative Integration manifests.

    Exact ``id@version`` content is immutable across layers: the same exact identity
    may appear in multiple layers only when its canonical manifest hash is identical.
    In that case the highest-precedence provenance is selected while the complete
    provenance chain remains visible. Built-in and organization sources may be locked;
    a lock blocks every higher-precedence definition of that Integration id, including
    a different version, so lower-precedence configuration cannot route around policy.
    """

    def __init__(self) -> None:
        self._entries: dict[str, dict[str, dict[RegistryLayer, IntegrationRegistration]]] = {}

    def _copy_entries(self) -> dict[str, dict[str, dict[RegistryLayer, IntegrationRegistration]]]:
        return {
            integration_id: {
                version: dict(layer_entries)
                for version, layer_entries in versions.items()
            }
            for integration_id, versions in self._entries.items()
        }

    def register(
        self,
        manifest: IntegrationManifest,
        *,
        layer: RegistryLayer,
        source: str,
        path: str,
        locked: bool = False,
    ) -> IntegrationRegistration:
        if not isinstance(manifest, IntegrationManifest):
            raise _fail("SDAI-INTEGRATION-REG-001", "registry manifest must be an IntegrationManifest")
        layer = _layer(layer)
        source = _source_label(source)
        path = _relative_manifest_path(path)
        if not isinstance(locked, bool):
            raise _fail("SDAI-INTEGRATION-REG-001", "registry locked must be a boolean")
        if locked and layer not in _LOCKABLE_LAYERS:
            allowed = ", ".join(item.value for item in sorted(_LOCKABLE_LAYERS, key=lambda item: item.priority))
            raise _fail(
                "SDAI-INTEGRATION-REG-005",
                f"locked Integration definitions are allowed only in authoritative layers: {allowed}",
            )

        provenance = IntegrationProvenance(
            layer=layer,
            source=source,
            path=path,
            manifest_sha256=manifest.sha256,
            locked=locked,
        )
        registration = IntegrationRegistration(manifest=manifest, provenance=provenance)
        integration_id = manifest.id
        version_text = str(manifest.version)

        existing_id = self._entries.get(integration_id, {})
        candidate_id = {
            version: dict(layer_entries)
            for version, layer_entries in existing_id.items()
        }
        exact = candidate_id.setdefault(version_text, {})
        if layer in exact:
            previous = exact[layer]
            raise _fail(
                "SDAI-INTEGRATION-REG-002",
                f"duplicate Integration '{manifest.identity}' in layer '{layer.value}' "
                f"(existing: {previous.provenance.source}/{previous.provenance.path}; "
                f"new: {source}/{path})",
            )
        exact[layer] = registration
        self._validate_id(integration_id, candidate_id)
        self._entries[integration_id] = candidate_id
        return registration

    @staticmethod
    def _validate_id(
        integration_id: str,
        versions: dict[str, dict[RegistryLayer, IntegrationRegistration]],
    ) -> None:
        all_registrations: list[IntegrationRegistration] = []
        for version_text, exact in versions.items():
            hashes = {item.manifest.sha256 for item in exact.values()}
            if len(hashes) > 1:
                details = ", ".join(
                    f"{item.provenance.layer.value}:{item.provenance.source}/{item.provenance.path}={item.manifest.sha256}"
                    for item in sorted(exact.values(), key=lambda item: item.provenance.layer.priority)
                )
                raise _fail(
                    "SDAI-INTEGRATION-REG-003",
                    f"exact Integration identity '{integration_id}@{version_text}' has conflicting canonical content: {details}",
                )
            all_registrations.extend(exact.values())

        ordered = sorted(
            all_registrations,
            key=lambda item: (
                item.provenance.layer.priority,
                str(item.manifest.version),
                item.provenance.source.casefold(),
                item.provenance.path.casefold(),
            ),
        )
        for locked_entry in (item for item in ordered if item.provenance.locked):
            blocked = [
                item
                for item in ordered
                if item.provenance.layer.priority > locked_entry.provenance.layer.priority
            ]
            if blocked:
                first = blocked[0]
                raise _fail(
                    "SDAI-INTEGRATION-REG-005",
                    f"Integration '{integration_id}' is locked by layer '{locked_entry.provenance.layer.value}' "
                    f"from '{locked_entry.provenance.source}/{locked_entry.provenance.path}'; "
                    f"higher layer '{first.provenance.layer.value}' from "
                    f"'{first.provenance.source}/{first.provenance.path}' is not allowed",
                )

    def _resolve_exact(self, integration_id: str, version: SemVer) -> ResolvedIntegration | None:
        exact = self._entries.get(integration_id, {}).get(str(version))
        if not exact:
            return None
        registrations = tuple(
            sorted(
                exact.values(),
                key=lambda item: (
                    item.provenance.layer.priority,
                    item.provenance.source.casefold(),
                    item.provenance.path.casefold(),
                ),
            )
        )
        selected = registrations[-1]
        return ResolvedIntegration(
            manifest=selected.manifest,
            selected_provenance=selected.provenance,
            provenance=tuple(item.provenance for item in registrations),
        )

    def list_versions(self, integration_id: str) -> tuple[ResolvedIntegration, ...]:
        integration_id = _integration_id(integration_id)
        versions = [_version(value) for value in self._entries.get(integration_id, {})]
        versions.sort(key=cmp_to_key(_version_compare), reverse=True)
        return tuple(
            resolved
            for version in versions
            if (resolved := self._resolve_exact(integration_id, version)) is not None
        )

    def resolve(
        self,
        integration_id: str,
        version: str | SemVer | None = None,
    ) -> ResolvedIntegration | None:
        integration_id = _integration_id(integration_id)
        if version is not None:
            return self._resolve_exact(integration_id, _version(version))
        versions = self.list_versions(integration_id)
        if not versions:
            return None
        best = versions[0]
        same_precedence = tuple(
            item for item in versions if item.version.same_precedence(best.version)
        )
        if len(same_precedence) > 1:
            identities = ", ".join(item.identity for item in same_precedence)
            raise _fail(
                "SDAI-INTEGRATION-REG-004",
                f"latest Integration '{integration_id}' is ambiguous because multiple exact versions "
                f"share the same SemVer precedence: {identities}; request an exact version",
            )
        return best

    def info(
        self,
        integration_id: str,
        version: str | SemVer | None = None,
    ) -> ResolvedIntegration | None:
        return self.resolve(integration_id, version)

    def list_resolved(self) -> tuple[ResolvedIntegration, ...]:
        results: list[ResolvedIntegration] = []
        for integration_id in sorted(self._entries):
            resolved = self.resolve(integration_id)
            if resolved is not None:
                results.append(resolved)
        return tuple(results)

    def search(self, query: str = "") -> tuple[ResolvedIntegration, ...]:
        if not isinstance(query, str):
            raise _fail("SDAI-INTEGRATION-REG-001", "search query must be a string")
        needle = unicodedata.normalize("NFC", query.strip()).casefold()
        results = self.list_resolved()
        if not needle:
            return results
        return tuple(
            item
            for item in results
            if needle
            in "\n".join(
                (
                    item.manifest.id,
                    item.manifest.display_name,
                    item.manifest.description,
                    *[capability.value for capability in item.manifest.capabilities],
                )
            ).casefold()
        )

    def list_all_exact(self) -> tuple[ResolvedIntegration, ...]:
        results: list[ResolvedIntegration] = []
        for integration_id in sorted(self._entries):
            versions = [_version(value) for value in self._entries[integration_id]]
            versions.sort(key=cmp_to_key(_version_compare), reverse=True)
            for version in versions:
                resolved = self._resolve_exact(integration_id, version)
                if resolved is not None:
                    results.append(resolved)
        return tuple(results)

    def as_dict(self) -> dict[str, object]:
        return {
            "apiVersion": INTEGRATION_REGISTRY_API_VERSION,
            "integrations": [item.as_dict() for item in self.list_all_exact()],
        }

    def to_json(self) -> str:
        return _canonical_json(self.as_dict())

    @property
    def sha256(self) -> str:
        return "sha256:" + sha256(self.to_json().encode("utf-8")).hexdigest()

    def __len__(self) -> int:
        return sum(len(versions) for versions in self._entries.values())


def discover_integration_manifests(source: IntegrationSource) -> tuple[tuple[Path, str], ...]:
    """Discover manifest files without following symlinked directories."""

    root = source.root
    if root.is_symlink():
        raise _fail("SDAI-INTEGRATION-REG-006", f"Integration source root '{root}' must not be a symlink")
    if not root.exists() or not root.is_dir():
        raise _fail("SDAI-INTEGRATION-REG-006", f"Integration source root '{root}' must be an existing directory")
    root = root.resolve()
    discovered: list[tuple[Path, str]] = []
    for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        directory = Path(dirpath)
        safe_dirs: list[str] = []
        for name in sorted(dirnames, key=lambda value: (value.casefold(), value)):
            candidate = directory / name
            if not candidate.is_symlink():
                safe_dirs.append(name)
        dirnames[:] = safe_dirs
        for name in sorted(filenames, key=lambda value: (value.casefold(), value)):
            if not name.endswith(_MANIFEST_SUFFIXES):
                continue
            path = directory / name
            relative = path.relative_to(root).as_posix()
            discovered.append((path, _relative_manifest_path(relative)))
    discovered.sort(key=lambda item: (item[1].casefold(), item[1]))
    return tuple(discovered)


def _source_sort_key(source: IntegrationSource) -> tuple[int, str, str, str, str]:
    root_folded, root_exact = _portable_fs_key(source.root)
    return (
        source.layer.priority,
        source.source.casefold(),
        source.source,
        root_folded,
        root_exact,
    )


def register_integration_source(
    registry: IntegrationRegistry,
    source: IntegrationSource,
) -> tuple[IntegrationRegistration, ...]:
    """Atomically load/register all manifests from one source root."""

    loaded = tuple(
        (load_integration_manifest(path, root=source.root), relative)
        for path, relative in discover_integration_manifests(source)
    )
    staged = IntegrationRegistry()
    staged._entries = registry._copy_entries()
    registrations: list[IntegrationRegistration] = []
    for manifest, relative in loaded:
        registrations.append(
            staged.register(
                manifest,
                layer=source.layer,
                source=source.source,
                path=relative,
                locked=source.locked,
            )
        )
    registry._entries = staged._entries
    return tuple(registrations)


def build_integration_registry(sources: Iterable[IntegrationSource]) -> IntegrationRegistry:
    """Build a fresh registry in stable authority/source/path order.

    The returned canonical registry contains no absolute discovery-root paths, so
    equivalent source labels and manifest-relative paths produce the same registry
    JSON/hash on different machines. Any duplicate, conflicting exact identity, lock,
    manifest, or containment error aborts construction; no partial registry is returned.
    """

    normalized = tuple(sources)
    if not all(isinstance(source, IntegrationSource) for source in normalized):
        raise _fail("SDAI-INTEGRATION-REG-001", "all registry sources must be IntegrationSource values")
    registry = IntegrationRegistry()
    for source in sorted(normalized, key=_source_sort_key):
        register_integration_source(registry, source)
    return registry
