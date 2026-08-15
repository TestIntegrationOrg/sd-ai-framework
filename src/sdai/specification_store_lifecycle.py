from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import tempfile
from typing import Mapping

import yaml

from sdai.path_safety import ensure_within_project
from sdai.specification_store_references import (
    SPECIFICATION_STORE_REFERENCES_PATH,
    ResolvedSpecificationStoreReference,
    SpecificationStoreReference,
    SpecificationStoreReferenceError,
    SpecificationStoreReferenceSet,
    load_specification_store_references,
    resolve_specification_store_references,
)
from sdai.specification_stores import (
    SPECIFICATION_STORE_MANIFEST_PATH,
    SpecificationRoot,
    SpecificationStoreError,
    SpecificationStoreManifest,
    _is_filesystem_redirect,
    load_specification_store_manifest,
)


STORE_CREATE_RESULT_API_VERSION = "sdai.specification-store-create-result/v1"
STORE_REGISTER_RESULT_API_VERSION = "sdai.specification-store-register-result/v1"
STORE_LIST_API_VERSION = "sdai.specification-store-list/v1"
STORE_DOCTOR_API_VERSION = "sdai.specification-store-doctor/v1"
STORE_CONTEXT_API_VERSION = "sdai.specification-store-context/v1"


class StoreLifecycleError(RuntimeError):
    """Raised when a SpecificationStore lifecycle operation is unsafe."""


class StoreAutomationExit(IntEnum):
    SUCCESS = 0
    ERROR = 1
    UNHEALTHY = 2


def _fail(code: str, message: str) -> StoreLifecycleError:
    return StoreLifecycleError(f"{code}: {message}")


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
            "SDAI-STORE-LIFECYCLE-001",
            "lifecycle output must be canonical finite JSON",
        ) from exc


def _project_root(project_root: Path) -> Path:
    try:
        root = Path(project_root).resolve(strict=True)
    except (OSError, RuntimeError, TypeError, UnicodeError, ValueError) as exc:
        raise _fail(
            "SDAI-STORE-LIFECYCLE-002",
            "project root must be an explicit existing local directory",
        ) from exc
    if not root.is_dir() or not (root / ".sdai" / "config.yaml").is_file():
        raise _fail(
            "SDAI-STORE-LIFECYCLE-002",
            "project is not initialized; run `sdai init` first",
        )
    return root


def _path_scope(project_root: Path, store_root: Path) -> str:
    try:
        store_root.resolve(strict=True).relative_to(project_root)
        return "project"
    except ValueError:
        return "external"


def _reference_path(project_root: Path, store_root: Path) -> str:
    resolved = store_root.resolve(strict=True)
    try:
        return resolved.relative_to(project_root).as_posix()
    except ValueError:
        return str(resolved)


def _safe_yaml(value: Mapping[str, object]) -> str:
    try:
        rendered = yaml.safe_dump(
            dict(value),
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
        )
    except (TypeError, ValueError, yaml.YAMLError) as exc:
        raise _fail(
            "SDAI-STORE-LIFECYCLE-001",
            "SpecificationStore data could not be serialized safely",
        ) from exc
    return rendered.replace("\r\n", "\n").replace("\r", "\n")


def _manifest_yaml(manifest: SpecificationStoreManifest) -> str:
    return _safe_yaml(manifest.as_dict())


def _reference_yaml(reference_set: SpecificationStoreReferenceSet) -> str:
    return _safe_yaml(reference_set.as_dict())


def _digest_bytes(data: bytes) -> str:
    return "sha256:" + sha256(data).hexdigest()


def _write_utf8_atomic(
    path: Path,
    text: str,
    *,
    expected_sha256: str | None,
) -> None:
    data = text.encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    if _is_filesystem_redirect(path.parent, label="lifecycle output parent"):
        raise _fail(
            "SDAI-STORE-LIFECYCLE-003",
            "lifecycle output parent must not be a filesystem redirect",
        )
    if path.exists() and _is_filesystem_redirect(path, label="lifecycle output"):
        raise _fail(
            "SDAI-STORE-LIFECYCLE-003",
            "lifecycle output must not be a filesystem redirect",
        )
    try:
        if expected_sha256 is None:
            with path.open("xb") as stream:
                stream.write(data)
            return
        current = path.read_bytes()
        if _digest_bytes(current) != expected_sha256:
            raise _fail(
                "SDAI-STORE-LIFECYCLE-004",
                "managed lifecycle declaration changed during update",
            )
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            latest = path.read_bytes()
            if _digest_bytes(latest) != expected_sha256:
                raise _fail(
                    "SDAI-STORE-LIFECYCLE-004",
                    "managed lifecycle declaration changed during update",
                )
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()
    except StoreLifecycleError:
        raise
    except FileExistsError as exc:
        raise _fail(
            "SDAI-STORE-LIFECYCLE-003",
            "refusing to overwrite an unmanaged lifecycle declaration",
        ) from exc
    except OSError as exc:
        raise _fail(
            "SDAI-STORE-LIFECYCLE-003",
            "unable to write managed lifecycle data safely",
        ) from exc


@dataclass(frozen=True)
class StoreCreateResult:
    identity: str
    manifest_sha256: str
    created: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "apiVersion": STORE_CREATE_RESULT_API_VERSION,
            "created": self.created,
            "identity": self.identity,
            "manifestSha256": self.manifest_sha256,
        }

    def to_json(self) -> str:
        return _canonical_json(self.as_dict())


@dataclass(frozen=True)
class StoreRegisterResult:
    identity: str
    manifest_sha256: str
    registered: bool
    path_scope: str

    def as_dict(self) -> dict[str, object]:
        return {
            "apiVersion": STORE_REGISTER_RESULT_API_VERSION,
            "declaration": SPECIFICATION_STORE_REFERENCES_PATH,
            "identity": self.identity,
            "manifestSha256": self.manifest_sha256,
            "pathScope": self.path_scope,
            "registered": self.registered,
        }

    def to_json(self) -> str:
        return _canonical_json(self.as_dict())


@dataclass(frozen=True)
class StoreListRecord:
    identity: str
    store: str
    version: str
    manifest_sha256: str
    snapshot_sha256: str
    path_scope: str
    ordinal: int

    @classmethod
    def from_resolved(
        cls,
        project_root: Path,
        resolved: ResolvedSpecificationStoreReference,
    ) -> "StoreListRecord":
        return cls(
            identity=resolved.identity,
            store=resolved.manifest.id,
            version=str(resolved.manifest.version),
            manifest_sha256=resolved.manifest.sha256,
            snapshot_sha256=resolved.snapshot.sha256,
            path_scope=_path_scope(project_root, resolved.root),
            ordinal=resolved.ordinal,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "identity": self.identity,
            "manifestSha256": self.manifest_sha256,
            "ordinal": self.ordinal,
            "pathScope": self.path_scope,
            "snapshotSha256": self.snapshot_sha256,
            "store": self.store,
            "version": self.version,
        }


@dataclass(frozen=True)
class StoreListResult:
    stores: tuple[StoreListRecord, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "apiVersion": STORE_LIST_API_VERSION,
            "stores": [item.as_dict() for item in self.stores],
        }

    def to_json(self) -> str:
        return _canonical_json(self.as_dict())


@dataclass(frozen=True)
class StoreDoctorFinding:
    level: str
    code: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "level": self.level, "message": self.message}


@dataclass(frozen=True)
class StoreDoctorResult:
    healthy: bool
    store_count: int
    findings: tuple[StoreDoctorFinding, ...]

    @property
    def exit_code(self) -> StoreAutomationExit:
        return StoreAutomationExit.SUCCESS if self.healthy else StoreAutomationExit.UNHEALTHY

    def as_dict(self) -> dict[str, object]:
        return {
            "apiVersion": STORE_DOCTOR_API_VERSION,
            "findings": [item.as_dict() for item in self.findings],
            "healthy": self.healthy,
            "storeCount": self.store_count,
        }

    def to_json(self) -> str:
        return _canonical_json(self.as_dict())


@dataclass(frozen=True)
class StoreContextRecord:
    identity: str
    manifest_sha256: str
    snapshot_sha256: str
    path_scope: str
    capabilities: tuple[str, ...]
    roots: tuple[tuple[str, str], ...]
    content: tuple[dict[str, object], ...]

    @classmethod
    def from_resolved(
        cls,
        project_root: Path,
        resolved: ResolvedSpecificationStoreReference,
    ) -> "StoreContextRecord":
        roots = tuple(
            (root.id, root.path) for root in resolved.manifest.specification_roots
        )
        content = tuple(entry.as_dict() for entry in resolved.snapshot.entries)
        return cls(
            identity=resolved.identity,
            manifest_sha256=resolved.manifest.sha256,
            snapshot_sha256=resolved.snapshot.sha256,
            path_scope=_path_scope(project_root, resolved.root),
            capabilities=resolved.manifest.capabilities,
            roots=roots,
            content=content,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "capabilities": list(self.capabilities),
            "content": list(self.content),
            "identity": self.identity,
            "manifestSha256": self.manifest_sha256,
            "pathScope": self.path_scope,
            "roots": {root_id: path for root_id, path in self.roots},
            "snapshotSha256": self.snapshot_sha256,
        }


@dataclass(frozen=True)
class StoreContextResult:
    stores: tuple[StoreContextRecord, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "apiVersion": STORE_CONTEXT_API_VERSION,
            "declaration": SPECIFICATION_STORE_REFERENCES_PATH,
            "stores": [item.as_dict() for item in self.stores],
        }

    def to_json(self) -> str:
        return _canonical_json(self.as_dict())


def _default_manifest(
    store_id: str,
    version: str,
    description: str,
) -> SpecificationStoreManifest:
    return SpecificationStoreManifest(
        id=store_id,
        version=version,  # type: ignore[arg-type]
        description=description,
        specification_roots=(
            SpecificationRoot("changes", "specs/changes"),
            SpecificationRoot("current", "specs/current"),
        ),
        capabilities=("changes", "current-specifications"),
        metadata={},
    )


def create_store(
    destination: Path,
    store_id: str,
    version: str,
    *,
    description: str = "Local SpecificationStore",
) -> StoreCreateResult:
    manifest = _default_manifest(store_id, version, description)
    try:
        root = Path(destination)
    except (TypeError, ValueError) as exc:
        raise _fail("SDAI-STORE-CREATE-001", "destination must be a valid local path") from exc
    if root.exists() and _is_filesystem_redirect(root, label="SpecificationStore destination"):
        raise _fail(
            "SDAI-STORE-CREATE-002",
            "SpecificationStore destination must not be a filesystem redirect",
        )
    if root.exists() and not root.is_dir():
        raise _fail(
            "SDAI-STORE-CREATE-002",
            "SpecificationStore destination exists and is not a directory",
        )
    manifest_path = root / SPECIFICATION_STORE_MANIFEST_PATH
    if root.exists() and any(root.iterdir()):
        if not manifest_path.is_file():
            raise _fail(
                "SDAI-STORE-CREATE-003",
                "refusing to initialize a non-empty unmanaged destination",
            )
        try:
            existing = load_specification_store_manifest(root)
        except SpecificationStoreError as exc:
            raise _fail(
                "SDAI-STORE-CREATE-003",
                "refusing to overwrite an invalid or unmanaged existing store",
            ) from exc
        if existing.as_dict() != manifest.as_dict():
            raise _fail(
                "SDAI-STORE-CREATE-003",
                "refusing to overwrite an existing SpecificationStore with different canonical content",
            )
        return StoreCreateResult(existing.identity, existing.sha256, False)

    try:
        root.mkdir(parents=True, exist_ok=True)
        for specification_root in manifest.specification_roots:
            root.joinpath(*PurePosixPath(specification_root.path).parts).mkdir(
                parents=True,
                exist_ok=False,
            )
        manifest_path.parent.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise _fail(
            "SDAI-STORE-CREATE-003",
            "refusing to overwrite unmanaged destination content",
        ) from exc
    except OSError as exc:
        raise _fail(
            "SDAI-STORE-CREATE-002",
            "unable to create SpecificationStore destination safely",
        ) from exc
    _write_utf8_atomic(manifest_path, _manifest_yaml(manifest), expected_sha256=None)
    try:
        loaded = load_specification_store_manifest(root)
    except SpecificationStoreError as exc:
        raise _fail(
            "SDAI-STORE-CREATE-004",
            "created SpecificationStore failed canonical validation",
        ) from exc
    if loaded.as_dict() != manifest.as_dict():
        raise _fail(
            "SDAI-STORE-CREATE-004",
            "created SpecificationStore differs from the requested canonical manifest",
        )
    return StoreCreateResult(loaded.identity, loaded.sha256, True)


def _load_reference_set_if_present(
    project_root: Path,
) -> SpecificationStoreReferenceSet | None:
    declaration = project_root / SPECIFICATION_STORE_REFERENCES_PATH
    if not declaration.exists():
        return None
    try:
        return load_specification_store_references(project_root)
    except SpecificationStoreReferenceError as exc:
        raise _fail(
            "SDAI-STORE-REGISTER-003",
            "existing SpecificationStore declaration is invalid and will not be overwritten",
        ) from exc


def register_store(project_root: Path, store_root: Path) -> StoreRegisterResult:
    root = _project_root(project_root)
    try:
        resolved_store = Path(store_root).resolve(strict=True)
        manifest = load_specification_store_manifest(resolved_store)
    except (OSError, RuntimeError, UnicodeError, ValueError, SpecificationStoreError) as exc:
        raise _fail(
            "SDAI-STORE-REGISTER-001",
            "store registration requires an explicit valid existing SpecificationStore",
        ) from exc
    declared_path = _reference_path(root, resolved_store)
    existing = _load_reference_set_if_present(root)
    if existing is not None:
        for reference in existing.references:
            if reference.identity != manifest.identity:
                continue
            try:
                existing_root = Path(reference.path)
                if not existing_root.is_absolute():
                    existing_root = root / existing_root
                same_root = existing_root.resolve(strict=True) == resolved_store
            except (OSError, RuntimeError, UnicodeError, ValueError):
                same_root = False
            if same_root:
                return StoreRegisterResult(
                    manifest.identity,
                    manifest.sha256,
                    False,
                    _path_scope(root, resolved_store),
                )
            raise _fail(
                "SDAI-STORE-REGISTER-002",
                "store identity is already registered to a different explicit path",
            )
        references = existing.references
        expected_sha256 = existing.source_sha256
    else:
        references = ()
        expected_sha256 = None
    candidate = SpecificationStoreReference(
        store=manifest.id,
        version=manifest.version,
        path=declared_path,
    )
    try:
        updated = SpecificationStoreReferenceSet(
            references=(*references, candidate),
            source_sha256="sha256:" + "0" * 64,
        )
    except SpecificationStoreReferenceError as exc:
        raise _fail(
            "SDAI-STORE-REGISTER-002",
            "registration conflicts with an existing store identity or path",
        ) from exc
    declaration = ensure_within_project(
        root,
        root / SPECIFICATION_STORE_REFERENCES_PATH,
        label="SpecificationStore declaration",
    )
    _write_utf8_atomic(
        declaration,
        _reference_yaml(updated),
        expected_sha256=expected_sha256,
    )
    # Reload through the strict contract before reporting success. Resolution is
    # deliberately read-only and proves the registered path/manifest is usable.
    try:
        reloaded = load_specification_store_references(root)
        resolved = resolve_specification_store_references(root)
    except SpecificationStoreReferenceError as exc:
        raise _fail(
            "SDAI-STORE-REGISTER-004",
            "written registration failed strict read-only resolution",
        ) from exc
    if reloaded.references != updated.references or resolved.get(manifest.id, manifest.version) is None:
        raise _fail(
            "SDAI-STORE-REGISTER-004",
            "written registration did not round-trip deterministically",
        )
    return StoreRegisterResult(
        manifest.identity,
        manifest.sha256,
        True,
        _path_scope(root, resolved_store),
    )


def list_stores(project_root: Path) -> StoreListResult:
    root = _project_root(project_root)
    if not (root / SPECIFICATION_STORE_REFERENCES_PATH).exists():
        return StoreListResult(())
    try:
        resolved = resolve_specification_store_references(root)
    except SpecificationStoreReferenceError as exc:
        raise _fail(
            "SDAI-STORE-LIST-001",
            "SpecificationStore references could not be resolved safely",
        ) from exc
    records = tuple(
        StoreListRecord.from_resolved(root, item) for item in resolved.references
    )
    return StoreListResult(records)


def doctor_stores(project_root: Path) -> StoreDoctorResult:
    root = _project_root(project_root)
    declaration = root / SPECIFICATION_STORE_REFERENCES_PATH
    if not declaration.exists():
        return StoreDoctorResult(
            healthy=True,
            store_count=0,
            findings=(
                StoreDoctorFinding(
                    "warning",
                    "SDAI-STORE-DOCTOR-001",
                    "no SpecificationStore references are registered",
                ),
            ),
        )
    try:
        resolved = resolve_specification_store_references(root)
        for reference in resolved.references:
            reference.verify_unchanged()
    except SpecificationStoreReferenceError:
        return StoreDoctorResult(
            healthy=False,
            store_count=0,
            findings=(
                StoreDoctorFinding(
                    "error",
                    "SDAI-STORE-DOCTOR-002",
                    "SpecificationStore references are stale, unsafe, or invalid",
                ),
            ),
        )
    return StoreDoctorResult(
        healthy=True,
        store_count=len(resolved.references),
        findings=(),
    )


def export_store_context(
    project_root: Path,
    *,
    store: str | None = None,
    version: str | None = None,
) -> StoreContextResult:
    root = _project_root(project_root)
    declaration = root / SPECIFICATION_STORE_REFERENCES_PATH
    if not declaration.exists():
        return StoreContextResult(())
    try:
        resolved = resolve_specification_store_references(root)
        if store is None:
            if version is not None:
                raise _fail(
                    "SDAI-STORE-CONTEXT-001",
                    "--version requires an explicit store id",
                )
            selected = resolved.references
        else:
            exact = resolved.get(store, version)
            if exact is None:
                raise _fail(
                    "SDAI-STORE-CONTEXT-002",
                    "requested SpecificationStore is not registered",
                )
            selected = (exact,)
    except StoreLifecycleError:
        raise
    except SpecificationStoreReferenceError as exc:
        raise _fail(
            "SDAI-STORE-CONTEXT-003",
            "SpecificationStore context could not be resolved safely",
        ) from exc
    return StoreContextResult(
        tuple(StoreContextRecord.from_resolved(root, item) for item in selected)
    )
