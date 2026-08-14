from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
from typing import Mapping

from sdai.integration_manifest import IntegrationProjection, ProjectionKind
from sdai.integration_registry import ResolvedIntegration
from sdai.path_safety import PathSafetyError, ensure_within_project


INTEGRATION_INSTALL_STATE_API_VERSION = "sdai.integration-install-state/v1"
INTEGRATION_OPERATION_JOURNAL_API_VERSION = "sdai.integration-operation-journal/v1"
INTEGRATION_STATUS_API_VERSION = "sdai.integration-status/v1"

_STATE_RELATIVE = ".sdai/integrations/install-state.json"
_JOURNAL_RELATIVE = ".sdai/integrations/operation.json"
_INTERNAL_ROOT = PurePosixPath(".sdai/integrations")
_WINDOWS_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
)
_WINDOWS_FORBIDDEN = frozenset('<>:"|?*')


class IntegrationMaterializationError(RuntimeError):
    """Raised when native Integration projection cannot be completed safely."""


class IntegrationFileStatus(StrEnum):
    EXACT = "exact"
    MISSING = "missing"
    MODIFIED = "modified"
    STALE = "stale"
    UNMANAGED_CONFLICT = "unmanaged-conflict"
    BROKEN = "broken"


_STATUS_PRIORITY = {
    IntegrationFileStatus.EXACT: 0,
    IntegrationFileStatus.MISSING: 1,
    IntegrationFileStatus.STALE: 2,
    IntegrationFileStatus.MODIFIED: 3,
    IntegrationFileStatus.UNMANAGED_CONFLICT: 4,
    IntegrationFileStatus.BROKEN: 5,
}


def _fail(code: str, message: str) -> IntegrationMaterializationError:
    return IntegrationMaterializationError(f"{code}: {message}")


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
        raise _fail("SDAI-INTEGRATION-MAT-001", "materialization data is not canonical finite JSON") from exc


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _fail("SDAI-INTEGRATION-MAT-001", f"JSON contains duplicate key '{key}'")
        result[key] = value
    return result


def _hash_bytes(data: bytes) -> str:
    return "sha256:" + sha256(data).hexdigest()


def _validate_sha(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value.startswith("sha256:"):
        raise _fail("SDAI-INTEGRATION-MAT-001", f"{label} must be a SHA-256 digest")
    digest = value.removeprefix("sha256:")
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise _fail("SDAI-INTEGRATION-MAT-001", f"{label} must be a lowercase SHA-256 digest")
    return value


def _safe_relative(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise _fail("SDAI-INTEGRATION-MAT-001", f"{label} must be a portable project-relative path")
    if value != value.strip():
        raise _fail("SDAI-INTEGRATION-MAT-001", f"{label} must not contain surrounding whitespace")
    path = PurePosixPath(value)
    parts = value.split("/")
    if path.is_absolute() or any(part in {"", ".", ".."} for part in parts):
        raise _fail("SDAI-INTEGRATION-MAT-001", f"{label} must be a portable project-relative path")
    for part in parts:
        if part != part.strip() or any(ord(char) < 32 for char in part):
            raise _fail("SDAI-INTEGRATION-MAT-001", f"{label} contains a non-portable path segment")
        if any(char in _WINDOWS_FORBIDDEN for char in part) or part.endswith("."):
            raise _fail("SDAI-INTEGRATION-MAT-001", f"{label} is not portable across filesystems")
        if part.split(".", 1)[0].upper() in _WINDOWS_RESERVED:
            raise _fail("SDAI-INTEGRATION-MAT-001", f"{label} uses a reserved Windows path segment")
    return path.as_posix()


def _path_overlap(left: str, right: str) -> bool:
    a = PurePosixPath(left).parts
    b = PurePosixPath(right).parts
    common = min(len(a), len(b))
    return a[:common] == b[:common]


def _project_path(root: Path, relative: str, *, label: str, allow_missing_leaf: bool = True) -> Path:
    root = root.resolve()
    relative = _safe_relative(relative, label=label)
    candidate = root.joinpath(*PurePosixPath(relative).parts)
    try:
        ensure_within_project(root, candidate, label=label)
    except PathSafetyError as exc:
        raise _fail("SDAI-INTEGRATION-MAT-003", f"{label} escapes the project root") from exc
    current = root
    parts = PurePosixPath(relative).parts
    for index, part in enumerate(parts):
        current = current / part
        if current.is_symlink():
            raise _fail("SDAI-INTEGRATION-MAT-003", f"{label} contains symlink component '{part}'")
        if index < len(parts) - 1 and current.exists() and not current.is_dir():
            raise _fail("SDAI-INTEGRATION-MAT-003", f"{label} ancestor '{part}' is not a directory")
    if not allow_missing_leaf and not candidate.exists():
        raise _fail("SDAI-INTEGRATION-MAT-003", f"{label} does not exist")
    return candidate


def install_state_path(root: Path) -> Path:
    return _project_path(root, _STATE_RELATIVE, label="Integration install state")


def operation_journal_path(root: Path) -> Path:
    return _project_path(root, _JOURNAL_RELATIVE, label="Integration operation journal")


def _ensure_parent(path: Path, root: Path) -> None:
    root = root.resolve()
    missing: list[Path] = []
    current = path.parent
    while current != root and not current.exists():
        missing.append(current)
        current = current.parent
    if current.is_symlink() or (current.exists() and not current.is_dir()):
        raise _fail("SDAI-INTEGRATION-MAT-003", f"unsafe parent for '{path}'")
    for directory in reversed(missing):
        try:
            directory.mkdir()
        except FileExistsError:
            pass
        if directory.is_symlink() or not directory.is_dir():
            raise _fail("SDAI-INTEGRATION-MAT-003", f"unsafe created parent '{directory}'")


def _atomic_write(path: Path, data: bytes, *, root: Path | None = None) -> None:
    if path.is_symlink():
        raise _fail("SDAI-INTEGRATION-MAT-003", f"refusing to replace symlink '{path}'")
    if root is None:
        path.parent.mkdir(parents=True, exist_ok=True)
    else:
        _ensure_parent(path, root)
    if path.parent.is_symlink():
        raise _fail("SDAI-INTEGRATION-MAT-003", f"refusing to write through symlink parent '{path.parent}'")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except FileExistsError as exc:
        raise _fail("SDAI-INTEGRATION-MAT-003", f"temporary atomic-write path already exists for '{path}'") from exc
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise _fail("SDAI-INTEGRATION-MAT-003", f"atomic write failed for '{path}'") from exc


@dataclass(frozen=True)
class ManagedIntegrationFile:
    kind: ProjectionKind
    source_path: str
    path: str
    sha256: str

    def __post_init__(self) -> None:
        try:
            kind = ProjectionKind(self.kind)
        except ValueError as exc:
            raise _fail("SDAI-INTEGRATION-MAT-001", "managed file kind is invalid") from exc
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "source_path", _safe_relative(self.source_path, label="managed source path"))
        object.__setattr__(self, "path", _safe_relative(self.path, label="managed destination path"))
        object.__setattr__(self, "sha256", _validate_sha(self.sha256, label="managed file sha256"))

    def as_dict(self) -> dict[str, object]:
        return {"kind": self.kind.value, "path": self.path, "sha256": self.sha256, "sourcePath": self.source_path}

    @classmethod
    def from_dict(cls, value: object) -> "ManagedIntegrationFile":
        if not isinstance(value, Mapping) or set(value) != {"kind", "path", "sha256", "sourcePath"}:
            raise _fail("SDAI-INTEGRATION-MAT-001", "managed file contract is invalid")
        return cls(value["kind"], value["sourcePath"], value["path"], value["sha256"])  # type: ignore[arg-type]


@dataclass(frozen=True)
class InstalledIntegration:
    id: str
    identity: str
    version: str
    manifest_sha256: str
    provenance_layer: str
    provenance_source: str
    provenance_path: str
    files: tuple[ManagedIntegrationFile, ...]
    preserved_paths: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not all(isinstance(value, str) and value for value in (self.id, self.identity, self.version, self.provenance_layer, self.provenance_source)):
            raise _fail("SDAI-INTEGRATION-MAT-001", "installed Integration identity/provenance is invalid")
        object.__setattr__(self, "manifest_sha256", _validate_sha(self.manifest_sha256, label="installed manifest sha256"))
        object.__setattr__(self, "provenance_path", _safe_relative(self.provenance_path, label="installed provenance path"))
        ordered = tuple(sorted(self.files, key=lambda item: item.path))
        if len({item.path for item in ordered}) != len(ordered):
            raise _fail("SDAI-INTEGRATION-MAT-001", "installed Integration files contain duplicate destinations")
        object.__setattr__(self, "files", ordered)
        preserved = tuple(sorted(_safe_relative(item, label="preserved path") for item in self.preserved_paths))
        if len(set(preserved)) != len(preserved):
            raise _fail("SDAI-INTEGRATION-MAT-001", "preserved paths contain duplicates")
        object.__setattr__(self, "preserved_paths", preserved)

    def as_dict(self) -> dict[str, object]:
        return {
            "files": [item.as_dict() for item in self.files],
            "id": self.id,
            "identity": self.identity,
            "manifestSha256": self.manifest_sha256,
            "preservedPaths": list(self.preserved_paths),
            "provenance": {"layer": self.provenance_layer, "path": self.provenance_path, "source": self.provenance_source},
            "version": self.version,
        }

    @classmethod
    def from_dict(cls, value: object) -> "InstalledIntegration":
        expected = {"files", "id", "identity", "manifestSha256", "preservedPaths", "provenance", "version"}
        if not isinstance(value, Mapping) or set(value) != expected:
            raise _fail("SDAI-INTEGRATION-MAT-001", "installed Integration contract is invalid")
        provenance = value["provenance"]
        files = value["files"]
        preserved = value["preservedPaths"]
        if not isinstance(provenance, Mapping) or set(provenance) != {"layer", "path", "source"}:
            raise _fail("SDAI-INTEGRATION-MAT-001", "installed Integration provenance is invalid")
        if not isinstance(files, list) or not isinstance(preserved, list):
            raise _fail("SDAI-INTEGRATION-MAT-001", "installed Integration file lists are invalid")
        return cls(
            id=value["id"],  # type: ignore[arg-type]
            identity=value["identity"],  # type: ignore[arg-type]
            version=value["version"],  # type: ignore[arg-type]
            manifest_sha256=value["manifestSha256"],  # type: ignore[arg-type]
            provenance_layer=provenance["layer"],  # type: ignore[arg-type]
            provenance_source=provenance["source"],  # type: ignore[arg-type]
            provenance_path=provenance["path"],  # type: ignore[arg-type]
            files=tuple(ManagedIntegrationFile.from_dict(item) for item in files),
            preserved_paths=tuple(preserved),  # type: ignore[arg-type]
        )


@dataclass(frozen=True)
class IntegrationInstallState:
    integrations: tuple[InstalledIntegration, ...] = ()

    def __post_init__(self) -> None:
        ordered = tuple(sorted(self.integrations, key=lambda item: item.id))
        if len({item.id for item in ordered}) != len(ordered):
            raise _fail("SDAI-INTEGRATION-MAT-001", "install state contains duplicate Integration ids")
        object.__setattr__(self, "integrations", ordered)

    def as_dict(self) -> dict[str, object]:
        return {"apiVersion": INTEGRATION_INSTALL_STATE_API_VERSION, "integrations": [item.as_dict() for item in self.integrations]}

    def to_text(self) -> str:
        return _canonical_json(self.as_dict()) + "\n"

    @property
    def sha256(self) -> str:
        return _hash_bytes(self.to_text().encode("utf-8"))

    @classmethod
    def from_json(cls, text: str) -> "IntegrationInstallState":
        try:
            value = json.loads(text, object_pairs_hook=_unique_json_object)
        except json.JSONDecodeError as exc:
            raise _fail("SDAI-INTEGRATION-MAT-001", "install state JSON is malformed") from exc
        if not isinstance(value, Mapping) or set(value) != {"apiVersion", "integrations"} or value["apiVersion"] != INTEGRATION_INSTALL_STATE_API_VERSION:
            raise _fail("SDAI-INTEGRATION-MAT-001", "install state contract is invalid")
        integrations = value["integrations"]
        if not isinstance(integrations, list):
            raise _fail("SDAI-INTEGRATION-MAT-001", "install state integrations must be a list")
        return cls(tuple(InstalledIntegration.from_dict(item) for item in integrations))


@dataclass(frozen=True)
class PlannedDelete:
    path: str
    expected_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", _safe_relative(self.path, label="planned delete path"))
        object.__setattr__(self, "expected_sha256", _validate_sha(self.expected_sha256, label="planned delete sha256"))

    def as_dict(self) -> dict[str, object]:
        return {"expectedSha256": self.expected_sha256, "path": self.path}

    @classmethod
    def from_dict(cls, value: object) -> "PlannedDelete":
        if not isinstance(value, Mapping) or set(value) != {"expectedSha256", "path"}:
            raise _fail("SDAI-INTEGRATION-MAT-007", "planned delete contract is invalid")
        return cls(value["path"], value["expectedSha256"])  # type: ignore[arg-type]


@dataclass(frozen=True)
class IntegrationOperationJournal:
    operation: str
    integration_id: str
    identity: str
    manifest_sha256: str
    writes: tuple[ManagedIntegrationFile, ...]
    deletes: tuple[PlannedDelete, ...]

    def __post_init__(self) -> None:
        if self.operation not in {"install", "upgrade", "repair", "remove"}:
            raise _fail("SDAI-INTEGRATION-MAT-007", "operation journal operation is invalid")
        if not all(isinstance(value, str) and value for value in (self.integration_id, self.identity)):
            raise _fail("SDAI-INTEGRATION-MAT-007", "operation journal identity is invalid")
        object.__setattr__(self, "manifest_sha256", _validate_sha(self.manifest_sha256, label="journal manifest sha256"))
        writes = tuple(sorted(self.writes, key=lambda item: item.path))
        deletes = tuple(sorted(self.deletes, key=lambda item: item.path))
        if len({item.path for item in writes}) != len(writes) or len({item.path for item in deletes}) != len(deletes):
            raise _fail("SDAI-INTEGRATION-MAT-007", "operation journal contains duplicate paths")
        object.__setattr__(self, "writes", writes)
        object.__setattr__(self, "deletes", deletes)

    def as_dict(self) -> dict[str, object]:
        return {
            "apiVersion": INTEGRATION_OPERATION_JOURNAL_API_VERSION,
            "deletes": [item.as_dict() for item in self.deletes],
            "identity": self.identity,
            "integrationId": self.integration_id,
            "manifestSha256": self.manifest_sha256,
            "operation": self.operation,
            "writes": [item.as_dict() for item in self.writes],
        }

    def to_text(self) -> str:
        return _canonical_json(self.as_dict()) + "\n"

    @classmethod
    def from_json(cls, text: str) -> "IntegrationOperationJournal":
        try:
            value = json.loads(text, object_pairs_hook=_unique_json_object)
        except json.JSONDecodeError as exc:
            raise _fail("SDAI-INTEGRATION-MAT-007", "operation journal JSON is malformed") from exc
        expected = {"apiVersion", "deletes", "identity", "integrationId", "manifestSha256", "operation", "writes"}
        if not isinstance(value, Mapping) or set(value) != expected or value["apiVersion"] != INTEGRATION_OPERATION_JOURNAL_API_VERSION:
            raise _fail("SDAI-INTEGRATION-MAT-007", "operation journal contract is invalid")
        if not isinstance(value["writes"], list) or not isinstance(value["deletes"], list):
            raise _fail("SDAI-INTEGRATION-MAT-007", "operation journal lists are invalid")
        return cls(
            operation=value["operation"],  # type: ignore[arg-type]
            integration_id=value["integrationId"],  # type: ignore[arg-type]
            identity=value["identity"],  # type: ignore[arg-type]
            manifest_sha256=value["manifestSha256"],  # type: ignore[arg-type]
            writes=tuple(ManagedIntegrationFile.from_dict(item) for item in value["writes"]),  # type: ignore[arg-type]
            deletes=tuple(PlannedDelete.from_dict(item) for item in value["deletes"]),  # type: ignore[arg-type]
        )


@dataclass(frozen=True)
class IntegrationStatusFinding:
    path: str | None
    status: IntegrationFileStatus
    expected_sha256: str | None
    actual_sha256: str | None
    detail: str

    def as_dict(self) -> dict[str, object]:
        return {
            "actualSha256": self.actual_sha256,
            "detail": self.detail,
            "expectedSha256": self.expected_sha256,
            "path": self.path,
            "status": self.status.value,
        }


@dataclass(frozen=True)
class IntegrationStatusReport:
    integration_id: str
    desired_identity: str
    desired_manifest_sha256: str
    installed_identity: str | None
    installed_manifest_sha256: str | None
    status: IntegrationFileStatus
    findings: tuple[IntegrationStatusFinding, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "apiVersion": INTEGRATION_STATUS_API_VERSION,
            "desiredIdentity": self.desired_identity,
            "desiredManifestSha256": self.desired_manifest_sha256,
            "findings": [item.as_dict() for item in self.findings],
            "installedIdentity": self.installed_identity,
            "installedManifestSha256": self.installed_manifest_sha256,
            "integrationId": self.integration_id,
            "status": self.status.value,
        }

    def to_json(self) -> str:
        return _canonical_json(self.as_dict())


@dataclass(frozen=True)
class _DesiredFile:
    managed: ManagedIntegrationFile
    data: bytes


def load_install_state(root: Path) -> IntegrationInstallState:
    path = install_state_path(root)
    if not path.exists():
        return IntegrationInstallState()
    if path.is_symlink() or not path.is_file():
        raise _fail("SDAI-INTEGRATION-MAT-003", "Integration install state must be a regular file")
    try:
        return IntegrationInstallState.from_json(path.read_text(encoding="utf-8", errors="strict"))
    except (OSError, UnicodeError) as exc:
        raise _fail("SDAI-INTEGRATION-MAT-003", "unable to read Integration install state as UTF-8") from exc


def _load_journal(root: Path) -> IntegrationOperationJournal | None:
    path = operation_journal_path(root)
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise _fail("SDAI-INTEGRATION-MAT-007", "Integration operation journal must be a regular file")
    try:
        return IntegrationOperationJournal.from_json(path.read_text(encoding="utf-8", errors="strict"))
    except (OSError, UnicodeError) as exc:
        raise _fail("SDAI-INTEGRATION-MAT-007", "unable to read Integration operation journal as UTF-8") from exc


def _write_state(root: Path, state: IntegrationInstallState) -> None:
    _atomic_write(install_state_path(root), state.to_text().encode("utf-8"), root=root)


def _write_journal(root: Path, journal: IntegrationOperationJournal) -> None:
    _atomic_write(operation_journal_path(root), journal.to_text().encode("utf-8"), root=root)


def _clear_journal(root: Path) -> None:
    try:
        operation_journal_path(root).unlink(missing_ok=True)
    except OSError as exc:
        raise _fail("SDAI-INTEGRATION-MAT-007", "unable to clear Integration operation journal") from exc


def _find_installed(state: IntegrationInstallState, integration_id: str) -> InstalledIntegration | None:
    return next((item for item in state.integrations if item.id == integration_id), None)


def _replace_installed(state: IntegrationInstallState, record: InstalledIntegration | None, integration_id: str) -> IntegrationInstallState:
    items = [item for item in state.integrations if item.id != integration_id]
    if record is not None:
        items.append(record)
    return IntegrationInstallState(tuple(items))


def _validate_projection_runtime(root: Path, projection: IntegrationProjection) -> tuple[Path, Path]:
    source = _project_path(root, projection.source, label=f"{projection.kind.value} projection source", allow_missing_leaf=False)
    target = _project_path(root, projection.target, label=f"{projection.kind.value} projection target")
    if _path_overlap(projection.target, _INTERNAL_ROOT.as_posix()):
        raise _fail("SDAI-INTEGRATION-MAT-002", f"projection target '{projection.target}' overlaps SDAI Integration state")
    source_rel = source.relative_to(root.resolve()).as_posix()
    target_rel = target.relative_to(root.resolve()).as_posix()
    if _path_overlap(source_rel, target_rel):
        raise _fail("SDAI-INTEGRATION-MAT-002", f"projection source '{projection.source}' and target '{projection.target}' overlap")
    return source, target


def _read_regular_file(path: Path, *, label: str) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise _fail("SDAI-INTEGRATION-MAT-002", f"{label} must be a regular non-symlink file")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise _fail("SDAI-INTEGRATION-MAT-002", f"unable to read {label}") from exc


def _projection_files(root: Path, projection: IntegrationProjection) -> tuple[_DesiredFile, ...]:
    source, _ = _validate_projection_runtime(root, projection)
    if source.is_file():
        data = _read_regular_file(source, label=f"projection source '{projection.source}'")
        managed = ManagedIntegrationFile(projection.kind, projection.source, projection.target, _hash_bytes(data))
        return (_DesiredFile(managed, data),)
    if not source.is_dir():
        raise _fail("SDAI-INTEGRATION-MAT-002", f"projection source '{projection.source}' must be a file or directory")

    result: list[_DesiredFile] = []
    for dirpath, dirnames, filenames in os.walk(source, topdown=True, followlinks=False):
        directory = Path(dirpath)
        for name in tuple(dirnames):
            child = directory / name
            if child.is_symlink():
                raise _fail("SDAI-INTEGRATION-MAT-002", f"projection source contains symlink directory '{child.relative_to(root)}'")
        dirnames[:] = sorted(dirnames, key=lambda value: (value.casefold(), value))
        for name in sorted(filenames, key=lambda value: (value.casefold(), value)):
            child = directory / name
            if child.is_symlink() or not child.is_file():
                raise _fail("SDAI-INTEGRATION-MAT-002", f"projection source contains non-regular file '{child.relative_to(root)}'")
            relative = child.relative_to(source).as_posix()
            source_path = PurePosixPath(projection.source, relative).as_posix()
            target_path = PurePosixPath(projection.target, relative).as_posix()
            data = _read_regular_file(child, label=f"projection source '{source_path}'")
            result.append(_DesiredFile(ManagedIntegrationFile(projection.kind, source_path, target_path, _hash_bytes(data)), data))
    return tuple(result)


def _desired_files(root: Path, resolved: ResolvedIntegration) -> tuple[_DesiredFile, ...]:
    desired: list[_DesiredFile] = []
    for projection in resolved.manifest.projections:
        desired.extend(_projection_files(root, projection))
    desired.sort(key=lambda item: item.managed.path)
    paths = [item.managed.path for item in desired]
    if len(set(paths)) != len(paths):
        raise _fail("SDAI-INTEGRATION-MAT-002", "projection expansion produced duplicate destination paths")
    return tuple(desired)


def _file_hash(root: Path, relative: str) -> tuple[str, str | None]:
    try:
        path = _project_path(root, relative, label="managed Integration destination")
    except IntegrationMaterializationError:
        return "broken", None
    if not path.exists():
        return "missing", None
    if path.is_symlink() or not path.is_file():
        return "broken", None
    try:
        return "present", _hash_bytes(path.read_bytes())
    except OSError:
        return "broken", None


def integration_status(root: Path, resolved: ResolvedIntegration) -> IntegrationStatusReport:
    state = load_install_state(root)
    previous = _find_installed(state, resolved.id)
    try:
        desired = _desired_files(root, resolved)
    except IntegrationMaterializationError as exc:
        finding = IntegrationStatusFinding(None, IntegrationFileStatus.BROKEN, None, None, str(exc))
        return IntegrationStatusReport(
            resolved.id,
            resolved.identity,
            resolved.manifest_sha256,
            None if previous is None else previous.identity,
            None if previous is None else previous.manifest_sha256,
            IntegrationFileStatus.BROKEN,
            (finding,),
        )

    desired_by_path = {item.managed.path: item.managed for item in desired}
    previous_by_path = {item.path: item for item in previous.files} if previous else {}
    findings: list[IntegrationStatusFinding] = []
    for path in sorted(desired_by_path):
        target = desired_by_path[path]
        old = previous_by_path.get(path)
        presence, actual = _file_hash(root, path)
        if presence == "broken":
            status, detail = IntegrationFileStatus.BROKEN, "destination is unsafe, unreadable, or not a regular file"
        elif presence == "missing":
            status, detail = IntegrationFileStatus.MISSING, "desired native file is missing"
        elif old is None:
            status, detail = IntegrationFileStatus.UNMANAGED_CONFLICT, "destination exists but is not owned by this Integration"
        elif actual != old.sha256:
            status, detail = IntegrationFileStatus.MODIFIED, "managed destination differs from last materialized bytes"
        elif actual != target.sha256:
            status, detail = IntegrationFileStatus.STALE, "managed destination is clean but desired source bytes changed"
        else:
            status, detail = IntegrationFileStatus.EXACT, "destination matches desired managed bytes"
        findings.append(IntegrationStatusFinding(path, status, target.sha256, actual, detail))

    if previous is not None:
        for path in sorted(set(previous_by_path) - set(desired_by_path)):
            old = previous_by_path[path]
            presence, actual = _file_hash(root, path)
            if presence == "broken":
                status, detail = IntegrationFileStatus.BROKEN, "obsolete managed destination is unsafe or unreadable"
            elif presence == "present" and actual != old.sha256:
                status, detail = IntegrationFileStatus.MODIFIED, "obsolete managed destination was user/tool modified and must be preserved"
            else:
                status, detail = IntegrationFileStatus.STALE, "managed destination is obsolete under the desired Integration"
            findings.append(IntegrationStatusFinding(path, status, None, actual, detail))

    metadata_stale = previous is not None and (
        previous.identity != resolved.identity or previous.manifest_sha256 != resolved.manifest_sha256
    )
    if previous is None and not findings:
        overall = IntegrationFileStatus.MISSING
    else:
        overall = max(
            (item.status for item in findings),
            key=lambda status: _STATUS_PRIORITY[status],
            default=IntegrationFileStatus.EXACT,
        )
        if metadata_stale and _STATUS_PRIORITY[overall] < _STATUS_PRIORITY[IntegrationFileStatus.STALE]:
            overall = IntegrationFileStatus.STALE
    return IntegrationStatusReport(
        resolved.id,
        resolved.identity,
        resolved.manifest_sha256,
        None if previous is None else previous.identity,
        None if previous is None else previous.manifest_sha256,
        overall,
        tuple(findings),
    )


def _planned_operation(
    resolved: ResolvedIntegration,
    previous: InstalledIntegration | None,
    desired: tuple[_DesiredFile, ...],
    operation: str,
) -> IntegrationOperationJournal:
    desired_paths = {item.managed.path for item in desired}
    deletes = tuple(
        PlannedDelete(item.path, item.sha256)
        for item in (previous.files if previous else ())
        if item.path not in desired_paths
    )
    return IntegrationOperationJournal(
        operation=operation,
        integration_id=resolved.id,
        identity=resolved.identity,
        manifest_sha256=resolved.manifest_sha256,
        writes=tuple(item.managed for item in desired),
        deletes=deletes,
    )


def _journal_already_committed(
    previous: InstalledIntegration | None,
    resolved: ResolvedIntegration,
    desired: tuple[_DesiredFile, ...],
    stale: IntegrationOperationJournal,
) -> bool:
    if previous is None or stale.operation == "remove":
        return False
    return (
        stale.integration_id == resolved.id
        and stale.identity == resolved.identity
        and stale.manifest_sha256 == resolved.manifest_sha256
        and previous.identity == resolved.identity
        and previous.manifest_sha256 == resolved.manifest_sha256
        and previous.files == tuple(item.managed for item in desired)
        and stale.writes == previous.files
    )


def _preflight(
    root: Path,
    previous: InstalledIntegration | None,
    desired: tuple[_DesiredFile, ...],
    journal: IntegrationOperationJournal,
    stale: IntegrationOperationJournal | None,
) -> tuple[str, ...]:
    previous_by_path = {item.path: item for item in previous.files} if previous else {}
    planned_by_path = {item.managed.path: item.managed for item in desired}
    preserved: set[str] = set(previous.preserved_paths if previous else ())

    if stale is not None and stale != journal:
        raise _fail(
            "SDAI-INTEGRATION-MAT-007",
            f"incomplete Integration operation for '{stale.integration_id}' requires recovery before '{journal.integration_id}'",
        )

    for path, target in planned_by_path.items():
        presence, actual = _file_hash(root, path)
        if presence == "broken":
            raise _fail("SDAI-INTEGRATION-MAT-003", f"destination '{path}' is unsafe or not a regular file")
        if presence == "missing":
            continue
        old = previous_by_path.get(path)
        if old is not None:
            if actual == old.sha256:
                continue
            if stale is not None and actual == target.sha256:
                continue
            raise _fail("SDAI-INTEGRATION-MAT-005", f"refusing to overwrite user-modified managed file '{path}'")
        if stale is None:
            raise _fail("SDAI-INTEGRATION-MAT-005", f"refusing to overwrite unmanaged file '{path}'")
        if actual != target.sha256:
            raise _fail("SDAI-INTEGRATION-MAT-005", f"interrupted output '{path}' no longer matches planned bytes")

    desired_paths = set(planned_by_path)
    for old in previous.files if previous else ():
        if old.path in desired_paths:
            continue
        presence, actual = _file_hash(root, old.path)
        if presence == "broken":
            raise _fail("SDAI-INTEGRATION-MAT-003", f"obsolete destination '{old.path}' is unsafe")
        if presence == "present" and actual != old.sha256:
            preserved.add(old.path)
    return tuple(sorted(preserved))


def _record_for(
    resolved: ResolvedIntegration,
    desired: tuple[_DesiredFile, ...],
    preserved: tuple[str, ...],
) -> InstalledIntegration:
    provenance = resolved.selected_provenance
    return InstalledIntegration(
        id=resolved.id,
        identity=resolved.identity,
        version=str(resolved.version),
        manifest_sha256=resolved.manifest_sha256,
        provenance_layer=provenance.layer.value,
        provenance_source=provenance.source,
        provenance_path=provenance.path,
        files=tuple(item.managed for item in desired),
        preserved_paths=preserved,
    )


def _has_blocking_modified_destination(report: IntegrationStatusReport) -> bool:
    return any(
        finding.status == IntegrationFileStatus.MODIFIED and finding.expected_sha256 is not None
        for finding in report.findings
    )


def _apply_materialization(root: Path, resolved: ResolvedIntegration, *, requested_operation: str) -> InstalledIntegration:
    state = load_install_state(root)
    previous = _find_installed(state, resolved.id)
    desired = _desired_files(root, resolved)
    stale = _load_journal(root)
    operation = "install" if previous is None else ("repair" if requested_operation == "repair" else "upgrade")
    journal = _planned_operation(resolved, previous, desired, operation)

    if stale is not None and _journal_already_committed(previous, resolved, desired, stale):
        _clear_journal(root)
        assert previous is not None
        return previous

    report = integration_status(root, resolved)
    if stale is None:
        if report.status in {IntegrationFileStatus.BROKEN, IntegrationFileStatus.UNMANAGED_CONFLICT}:
            raise _fail("SDAI-INTEGRATION-MAT-005", f"cannot {requested_operation} Integration while status is '{report.status.value}'")
        if _has_blocking_modified_destination(report):
            raise _fail("SDAI-INTEGRATION-MAT-005", "cannot overwrite user-modified managed destination")
        if report.status == IntegrationFileStatus.EXACT and previous is not None:
            return previous

    preserved = _preflight(root, previous, desired, journal, stale)
    _write_journal(root, journal)
    data_by_path = {item.managed.path: item.data for item in desired}
    for managed in journal.writes:
        destination = _project_path(root, managed.path, label="managed Integration destination")
        _atomic_write(destination, data_by_path[managed.path], root=root)

    for deletion in journal.deletes:
        presence, actual = _file_hash(root, deletion.path)
        if presence == "missing":
            continue
        if presence == "present" and actual == deletion.expected_sha256:
            try:
                _project_path(root, deletion.path, label="obsolete managed Integration destination").unlink(missing_ok=True)
            except OSError as exc:
                raise _fail("SDAI-INTEGRATION-MAT-003", f"unable to remove obsolete managed file '{deletion.path}'") from exc

    record = _record_for(resolved, desired, preserved)
    _write_state(root, _replace_installed(state, record, resolved.id))
    _clear_journal(root)
    return record


def materialize_integration(root: Path, resolved: ResolvedIntegration) -> InstalledIntegration:
    """Install or safely upgrade one resolved Integration's native projections."""
    return _apply_materialization(root, resolved, requested_operation="upgrade")


def repair_integration(root: Path, resolved: ResolvedIntegration) -> InstalledIntegration:
    """Repair missing/stale clean files; never overwrite modified/unmanaged content."""
    return _apply_materialization(root, resolved, requested_operation="repair")


def remove_integration(root: Path, integration_id: str) -> tuple[str, ...]:
    state = load_install_state(root)
    previous = _find_installed(state, integration_id)
    stale = _load_journal(root)
    if previous is None:
        if stale is not None and stale.integration_id == integration_id and stale.operation == "remove":
            _clear_journal(root)
        elif stale is not None:
            raise _fail("SDAI-INTEGRATION-MAT-007", f"incomplete Integration operation for '{stale.integration_id}' must be recovered first")
        return ()

    journal = IntegrationOperationJournal(
        operation="remove",
        integration_id=previous.id,
        identity=previous.identity,
        manifest_sha256=previous.manifest_sha256,
        writes=(),
        deletes=tuple(PlannedDelete(item.path, item.sha256) for item in previous.files),
    )
    if stale is not None and stale != journal:
        raise _fail("SDAI-INTEGRATION-MAT-007", f"incomplete Integration operation for '{stale.integration_id}' must be recovered first")
    _write_journal(root, journal)

    preserved: set[str] = set(previous.preserved_paths)
    for deletion in journal.deletes:
        presence, actual = _file_hash(root, deletion.path)
        if presence == "broken":
            preserved.add(deletion.path)
        elif presence == "present" and actual == deletion.expected_sha256:
            try:
                _project_path(root, deletion.path, label="managed Integration destination").unlink(missing_ok=True)
            except OSError as exc:
                raise _fail("SDAI-INTEGRATION-MAT-003", f"unable to remove managed file '{deletion.path}'") from exc
        elif presence == "present":
            preserved.add(deletion.path)

    _write_state(root, _replace_installed(state, None, integration_id))
    _clear_journal(root)
    return tuple(sorted(preserved))
