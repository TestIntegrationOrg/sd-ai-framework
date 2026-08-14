from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import tempfile
from typing import Mapping

from sdai.pack_catalog import ResolvedCatalogSet
from sdai.pack_integrity import build_pack_content_index
from sdai.pack_lock import PackLock, PackLockEntry
from sdai.pack_manifest import PackManifest, load_pack_manifest


PACK_INSTALL_STATE_API_VERSION = "sdai.pack-install-state/v1"
PACK_INSTALL_RECORD_API_VERSION = "sdai.pack-install-record/v1"
PACK_LINK_RECORD_API_VERSION = "sdai.pack-local-link/v1"
PACK_OPERATION_JOURNAL_API_VERSION = "sdai.pack-operation-journal/v1"


class PackLifecycleError(RuntimeError):
    pass


def _fail(code: str, message: str) -> PackLifecycleError:
    return PackLifecycleError(f"{code}: {message}")


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
        raise _fail("SDAI-PACK-LIFECYCLE-001", "state is not canonical finite JSON") from exc


def _hash_bytes(data: bytes) -> str:
    return "sha256:" + sha256(data).hexdigest()


def _valid_hash(value: object) -> bool:
    return (
        isinstance(value, str)
        and value.startswith("sha256:")
        and len(value) == 71
        and all(char in "0123456789abcdef" for char in value[7:])
    )


def _safe_relative(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise _fail("SDAI-PACK-LIFECYCLE-002", f"{label} is not a portable relative path")
    pure = PurePosixPath(value)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in value.split("/")):
        raise _fail("SDAI-PACK-LIFECYCLE-002", f"{label} is not a safe relative path")
    return pure.as_posix()


def _managed_path(root: Path, relative: str) -> Path:
    safe = _safe_relative(relative, label="managed path")
    if root.is_symlink():
        raise _fail("SDAI-PACK-LIFECYCLE-002", "project root must not be a symlink")
    resolved_root = root.resolve()
    current = root
    for part in PurePosixPath(safe).parts:
        current = current / part
        if current.is_symlink():
            raise _fail(
                "SDAI-PACK-LIFECYCLE-002",
                f"managed path '{safe}' contains a symlink component",
            )
    try:
        current.resolve(strict=False).relative_to(resolved_root)
    except ValueError as exc:
        raise _fail("SDAI-PACK-LIFECYCLE-002", f"managed path '{safe}' escapes project root") from exc
    return current


def _state_dir(root: Path) -> Path:
    return root / ".sdai" / "packs"


def install_state_path(root: Path) -> Path:
    return _state_dir(root) / "install-state.json"


def installed_lock_path(root: Path) -> Path:
    return _state_dir(root) / "pack-lock.json"


def operation_journal_path(root: Path) -> Path:
    return _state_dir(root) / "operation-journal.json"


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise _fail("SDAI-PACK-LIFECYCLE-003", f"state path '{path}' must not be a symlink")
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    except OSError as exc:
        raise _fail("SDAI-PACK-LIFECYCLE-003", f"unable to atomically write '{path}'") from exc
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


@dataclass(frozen=True)
class ManagedFile:
    path: str
    sha256: str
    source_path: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", _safe_relative(self.path, label="managed file path"))
        object.__setattr__(
            self,
            "source_path",
            _safe_relative(self.source_path, label="managed source path"),
        )
        if not _valid_hash(self.sha256):
            raise _fail("SDAI-PACK-LIFECYCLE-001", "managed file hash must be SHA-256")

    def as_dict(self) -> dict[str, str]:
        return {"path": self.path, "sha256": self.sha256, "sourcePath": self.source_path}

    @classmethod
    def from_dict(cls, value: object) -> "ManagedFile":
        if not isinstance(value, Mapping) or set(value) != {"path", "sha256", "sourcePath"}:
            raise _fail("SDAI-PACK-LIFECYCLE-001", "managed file contract is invalid")
        return cls(
            path=_safe_relative(value["path"], label="managed file path"),
            sha256=value["sha256"] if isinstance(value["sha256"], str) else "",
            source_path=_safe_relative(value["sourcePath"], label="managed source path"),
        )


@dataclass(frozen=True)
class InstalledPack:
    identity: str
    coordinate: str
    mode: str
    source: str
    manifest_sha256: str
    content_sha256: str
    lock_sha256: str
    files: tuple[ManagedFile, ...]
    local_path: str | None = None
    preserved_paths: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.mode not in {"installed", "local-link"}:
            raise _fail("SDAI-PACK-LIFECYCLE-001", f"unsupported Pack mode '{self.mode}'")
        if not isinstance(self.coordinate, str) or not isinstance(self.identity, str) or not self.identity.startswith(self.coordinate + "@"):
            raise _fail("SDAI-PACK-LIFECYCLE-001", "Pack identity does not match coordinate")
        if not isinstance(self.source, str) or not self.source:
            raise _fail("SDAI-PACK-LIFECYCLE-001", "Pack source must be a non-empty string")
        for value, label in (
            (self.manifest_sha256, "manifestSha256"),
            (self.content_sha256, "contentSha256"),
            (self.lock_sha256, "lockSha256"),
        ):
            if not _valid_hash(value):
                raise _fail("SDAI-PACK-LIFECYCLE-001", f"{label} must be SHA-256")
        if self.files != tuple(sorted(self.files, key=lambda item: item.path)) or len({item.path for item in self.files}) != len(self.files):
            raise _fail("SDAI-PACK-LIFECYCLE-001", "Pack managed files must be unique and sorted")
        preserved = tuple(_safe_relative(item, label="preserved path") for item in self.preserved_paths)
        if preserved != tuple(sorted(set(preserved))):
            raise _fail("SDAI-PACK-LIFECYCLE-001", "preserved paths must be unique and sorted")
        if self.mode == "local-link" and not self.local_path:
            raise _fail("SDAI-PACK-LIFECYCLE-001", "local-link Pack requires localPath provenance")
        if self.mode == "installed" and self.local_path is not None:
            raise _fail("SDAI-PACK-LIFECYCLE-001", "installed Pack must not carry localPath provenance")

    def as_dict(self) -> dict[str, object]:
        return {
            "apiVersion": PACK_LINK_RECORD_API_VERSION if self.mode == "local-link" else PACK_INSTALL_RECORD_API_VERSION,
            "contentSha256": self.content_sha256,
            "coordinate": self.coordinate,
            "files": [item.as_dict() for item in self.files],
            "identity": self.identity,
            "localPath": self.local_path,
            "lockSha256": self.lock_sha256,
            "manifestSha256": self.manifest_sha256,
            "mode": self.mode,
            "preservedPaths": list(self.preserved_paths),
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, value: object) -> "InstalledPack":
        expected = {
            "apiVersion", "contentSha256", "coordinate", "files", "identity",
            "localPath", "lockSha256", "manifestSha256", "mode", "preservedPaths", "source",
        }
        if not isinstance(value, Mapping) or set(value) != expected:
            raise _fail("SDAI-PACK-LIFECYCLE-001", "installed Pack contract is invalid")
        mode = value["mode"]
        expected_api = PACK_LINK_RECORD_API_VERSION if mode == "local-link" else PACK_INSTALL_RECORD_API_VERSION
        if value["apiVersion"] != expected_api:
            raise _fail("SDAI-PACK-LIFECYCLE-001", "installed Pack apiVersion/mode mismatch")
        files = value["files"]
        preserved = value["preservedPaths"]
        if not isinstance(files, list) or not isinstance(preserved, list):
            raise _fail("SDAI-PACK-LIFECYCLE-001", "installed Pack collections are invalid")
        scalar_keys = ("identity", "coordinate", "mode", "source", "manifestSha256", "contentSha256", "lockSha256")
        if not all(isinstance(value[key], str) for key in scalar_keys):
            raise _fail("SDAI-PACK-LIFECYCLE-001", "installed Pack scalar fields are invalid")
        if not all(isinstance(item, str) for item in preserved):
            raise _fail("SDAI-PACK-LIFECYCLE-001", "preservedPaths must contain strings")
        local_path = value["localPath"]
        if local_path is not None and not isinstance(local_path, str):
            raise _fail("SDAI-PACK-LIFECYCLE-001", "localPath must be a string or null")
        return cls(
            identity=value["identity"],  # type: ignore[arg-type]
            coordinate=value["coordinate"],  # type: ignore[arg-type]
            mode=value["mode"],  # type: ignore[arg-type]
            source=value["source"],  # type: ignore[arg-type]
            manifest_sha256=value["manifestSha256"],  # type: ignore[arg-type]
            content_sha256=value["contentSha256"],  # type: ignore[arg-type]
            lock_sha256=value["lockSha256"],  # type: ignore[arg-type]
            files=tuple(ManagedFile.from_dict(item) for item in files),
            local_path=local_path,
            preserved_paths=tuple(preserved),
        )


@dataclass(frozen=True)
class PackInstallState:
    packs: tuple[InstalledPack, ...] = ()

    def __post_init__(self) -> None:
        coordinates = [item.coordinate for item in self.packs]
        if coordinates != sorted(coordinates) or len(set(coordinates)) != len(coordinates):
            raise _fail("SDAI-PACK-LIFECYCLE-001", "installed Packs must be unique and sorted")

    def as_dict(self) -> dict[str, object]:
        return {"apiVersion": PACK_INSTALL_STATE_API_VERSION, "packs": [item.as_dict() for item in self.packs]}

    def to_text(self) -> str:
        return _canonical_json(self.as_dict()) + "\n"

    @property
    def sha256(self) -> str:
        return _hash_bytes(self.to_text().encode("utf-8"))

    @classmethod
    def from_json(cls, text: str) -> "PackInstallState":
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            raise _fail("SDAI-PACK-LIFECYCLE-001", "Pack install state JSON is malformed") from exc
        if not isinstance(value, Mapping) or set(value) != {"apiVersion", "packs"} or value["apiVersion"] != PACK_INSTALL_STATE_API_VERSION:
            raise _fail("SDAI-PACK-LIFECYCLE-001", "Pack install state contract is invalid")
        packs = value["packs"]
        if not isinstance(packs, list):
            raise _fail("SDAI-PACK-LIFECYCLE-001", "Pack install state packs must be a list")
        return cls(tuple(InstalledPack.from_dict(item) for item in packs))


@dataclass(frozen=True)
class OperationJournal:
    operation: str
    coordinate: str
    identity: str
    files: tuple[ManagedFile, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "apiVersion": PACK_OPERATION_JOURNAL_API_VERSION,
            "coordinate": self.coordinate,
            "files": [item.as_dict() for item in self.files],
            "identity": self.identity,
            "operation": self.operation,
        }

    def to_text(self) -> str:
        return _canonical_json(self.as_dict()) + "\n"

    @classmethod
    def from_json(cls, text: str) -> "OperationJournal":
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            raise _fail("SDAI-PACK-LIFECYCLE-007", "operation journal JSON is malformed") from exc
        expected = {"apiVersion", "coordinate", "files", "identity", "operation"}
        if not isinstance(value, Mapping) or set(value) != expected or value["apiVersion"] != PACK_OPERATION_JOURNAL_API_VERSION:
            raise _fail("SDAI-PACK-LIFECYCLE-007", "operation journal contract is invalid")
        files = value["files"]
        if not isinstance(files, list) or not all(isinstance(value[key], str) for key in ("operation", "coordinate", "identity")):
            raise _fail("SDAI-PACK-LIFECYCLE-007", "operation journal fields are invalid")
        return cls(
            operation=value["operation"],  # type: ignore[arg-type]
            coordinate=value["coordinate"],  # type: ignore[arg-type]
            identity=value["identity"],  # type: ignore[arg-type]
            files=tuple(ManagedFile.from_dict(item) for item in files),
        )


def load_install_state(root: Path) -> PackInstallState:
    path = install_state_path(root)
    if not path.exists():
        return PackInstallState()
    if path.is_symlink() or not path.is_file():
        raise _fail("SDAI-PACK-LIFECYCLE-003", "Pack install state must be a regular file")
    try:
        return PackInstallState.from_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError) as exc:
        raise _fail("SDAI-PACK-LIFECYCLE-003", "unable to read Pack install state as UTF-8") from exc


def _load_journal(root: Path) -> OperationJournal | None:
    path = operation_journal_path(root)
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise _fail("SDAI-PACK-LIFECYCLE-007", "operation journal must be a regular file")
    try:
        return OperationJournal.from_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError) as exc:
        raise _fail("SDAI-PACK-LIFECYCLE-007", "unable to read operation journal as UTF-8") from exc


def _write_state(root: Path, state: PackInstallState) -> None:
    _atomic_write(install_state_path(root), state.to_text().encode("utf-8"))


def _write_journal(root: Path, journal: OperationJournal) -> None:
    _atomic_write(operation_journal_path(root), journal.to_text().encode("utf-8"))


def _clear_journal(root: Path) -> None:
    try:
        operation_journal_path(root).unlink(missing_ok=True)
    except OSError as exc:
        raise _fail("SDAI-PACK-LIFECYCLE-007", "unable to clear operation journal") from exc


def _destination_path(relative_source: str) -> str:
    return _safe_relative(f".sdai/installed-packs/{relative_source}", label="Pack destination")


def _source_files(pack_root: Path, manifest: PackManifest) -> tuple[tuple[str, bytes], ...]:
    index = build_pack_content_index(pack_root, manifest)
    root = pack_root.resolve()
    result: list[tuple[str, bytes]] = []
    for entry in index.entries:
        source = root.joinpath(*PurePosixPath(entry.path).parts)
        try:
            data = source.read_bytes()
        except OSError as exc:
            raise _fail("SDAI-PACK-LIFECYCLE-003", f"unable to read Pack source file '{entry.path}'") from exc
        if _hash_bytes(data) != entry.sha256:
            raise _fail("SDAI-PACK-LIFECYCLE-004", f"Pack source changed while installing '{entry.path}'")
        result.append((entry.path, data))
    return tuple(result)


def _verify_existing_file(root: Path, managed: ManagedFile) -> str:
    path = _managed_path(root, managed.path)
    if not path.exists():
        return "missing"
    if not path.is_file():
        return "modified"
    try:
        return "clean" if _hash_bytes(path.read_bytes()) == managed.sha256 else "modified"
    except OSError as exc:
        raise _fail("SDAI-PACK-LIFECYCLE-003", f"unable to inspect managed file '{managed.path}'") from exc


def _replace_record(state: PackInstallState, record: InstalledPack | None, coordinate: str) -> PackInstallState:
    items = [item for item in state.packs if item.coordinate != coordinate]
    if record is not None:
        items.append(record)
    return PackInstallState(tuple(sorted(items, key=lambda item: item.coordinate)))


def _find_lock_entry(lock: PackLock, coordinate: str) -> PackLockEntry:
    matches = [entry for entry in lock.packages if entry.coordinate == coordinate]
    if len(matches) != 1:
        raise _fail("SDAI-PACK-LIFECYCLE-004", f"Pack coordinate '{coordinate}' is not present exactly once in the lock")
    return matches[0]


def _preflight_destinations(
    root: Path,
    planned: tuple[ManagedFile, ...],
    previous: InstalledPack | None,
    stale: OperationJournal | None,
) -> None:
    previous_by_path = {item.path: item for item in previous.files} if previous else {}
    for managed in planned:
        destination = _managed_path(root, managed.path)
        if not destination.exists():
            continue
        prior = previous_by_path.get(managed.path)
        if prior is not None:
            if _verify_existing_file(root, prior) == "modified":
                raise _fail(
                    "SDAI-PACK-LIFECYCLE-005",
                    f"refusing to overwrite user-modified managed file '{managed.path}'",
                )
            continue
        if stale is None:
            raise _fail(
                "SDAI-PACK-LIFECYCLE-005",
                f"refusing to overwrite unmanaged file '{managed.path}'",
            )
        try:
            actual = _hash_bytes(destination.read_bytes()) if destination.is_file() else ""
        except OSError as exc:
            raise _fail("SDAI-PACK-LIFECYCLE-003", f"unable to recover '{managed.path}'") from exc
        if actual != managed.sha256:
            raise _fail(
                "SDAI-PACK-LIFECYCLE-005",
                f"interrupted output '{managed.path}' no longer matches planned bytes",
            )


def install_from_local(
    root: Path,
    pack_root: Path,
    lock: PackLock,
    coordinate: str,
    *,
    local_link: bool = False,
    manifest_name: str = "pack.yaml",
) -> InstalledPack:
    lock_entry = _find_lock_entry(lock, coordinate)
    manifest = load_pack_manifest(pack_root / manifest_name, pack_root=pack_root)
    if manifest.identity != lock_entry.identity:
        raise _fail("SDAI-PACK-LIFECYCLE-004", f"local Pack identity '{manifest.identity}' does not match lock '{lock_entry.identity}'")
    content_index = build_pack_content_index(pack_root, manifest)
    if content_index.sha256 != lock_entry.content_sha256:
        raise _fail("SDAI-PACK-LIFECYCLE-004", "local Pack content does not match exact lock content hash")
    if manifest.sha256 != lock_entry.manifest_sha256:
        raise _fail("SDAI-PACK-LIFECYCLE-004", "local Pack manifest does not match exact lock manifest hash")

    state = load_install_state(root)
    previous = next((item for item in state.packs if item.coordinate == coordinate), None)
    source_files = _source_files(pack_root, manifest)
    source_prefix = f"{manifest.publisher}/{manifest.id}/{manifest.version}"
    planned = tuple(
        sorted(
            (
                ManagedFile(
                    _destination_path(f"{source_prefix}/{source_path}"),
                    _hash_bytes(data),
                    source_path,
                )
                for source_path, data in source_files
            ),
            key=lambda item: item.path,
        )
    )

    stale = _load_journal(root)
    if stale is not None and (
        stale.coordinate != coordinate
        or stale.identity != manifest.identity
        or stale.files != planned
        or stale.operation not in {"install", "update", "local-link"}
    ):
        raise _fail(
            "SDAI-PACK-LIFECYCLE-007",
            f"incomplete Pack operation for '{stale.coordinate}' requires recovery before '{coordinate}'",
        )

    # Crucially, new operations validate all existing destinations before creating a
    # journal. A later retry may therefore adopt byte-identical output only when the
    # journal pre-dated those bytes; an originally unmanaged file can never acquire
    # ownership merely because a failed first attempt wrote a journal beside it.
    _preflight_destinations(root, planned, previous, stale)
    journal = OperationJournal(
        operation="local-link" if local_link else ("update" if previous else "install"),
        coordinate=coordinate,
        identity=manifest.identity,
        files=planned,
    )
    _write_journal(root, journal)

    data_by_source = dict(source_files)
    for managed in planned:
        destination = _managed_path(root, managed.path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(destination, data_by_source[managed.source_path])

    planned_paths = {item.path for item in planned}
    preserved: set[str] = set(previous.preserved_paths if previous else ())
    if previous is not None:
        for old in previous.files:
            if old.path in planned_paths:
                continue
            status = _verify_existing_file(root, old)
            if status == "clean":
                try:
                    _managed_path(root, old.path).unlink(missing_ok=True)
                except OSError as exc:
                    raise _fail("SDAI-PACK-LIFECYCLE-003", f"unable to remove obsolete '{old.path}'") from exc
            elif status == "modified":
                preserved.add(old.path)

    record = InstalledPack(
        identity=manifest.identity,
        coordinate=manifest.coordinate,
        mode="local-link" if local_link else "installed",
        source="local-link:" + pack_root.resolve().as_posix() if local_link else lock_entry.source,
        manifest_sha256=manifest.sha256,
        content_sha256=content_index.sha256,
        lock_sha256=lock.sha256,
        files=planned,
        local_path=pack_root.resolve().as_posix() if local_link else None,
        preserved_paths=tuple(sorted(preserved)),
    )
    _write_state(root, _replace_record(state, record, coordinate))
    _atomic_write(installed_lock_path(root), lock.to_text().encode("utf-8"))
    _clear_journal(root)
    return record


def remove_pack(root: Path, coordinate: str) -> tuple[str, ...]:
    state = load_install_state(root)
    record = next((item for item in state.packs if item.coordinate == coordinate), None)
    stale = _load_journal(root)
    if record is None:
        if stale is not None and stale.coordinate == coordinate and stale.operation == "remove":
            _clear_journal(root)
        return ()
    if stale is not None and (stale.coordinate != coordinate or stale.operation != "remove"):
        raise _fail("SDAI-PACK-LIFECYCLE-007", f"incomplete Pack operation for '{stale.coordinate}' must be recovered first")
    _write_journal(root, OperationJournal("remove", coordinate, record.identity, record.files))
    preserved: set[str] = set(record.preserved_paths)
    for managed in record.files:
        status = _verify_existing_file(root, managed)
        if status == "clean":
            try:
                _managed_path(root, managed.path).unlink(missing_ok=True)
            except OSError as exc:
                raise _fail("SDAI-PACK-LIFECYCLE-003", f"unable to remove '{managed.path}'") from exc
        elif status == "modified":
            preserved.add(managed.path)
    _write_state(root, _replace_record(state, None, coordinate))
    _clear_journal(root)
    return tuple(sorted(preserved))


def outdated_packs(state: PackInstallState, lock: PackLock) -> tuple[InstalledPack, ...]:
    exact = {item.coordinate: item for item in lock.packages}
    return tuple(
        installed
        for installed in state.packs
        if (
            (target := exact.get(installed.coordinate)) is None
            or installed.identity != target.identity
            or installed.manifest_sha256 != target.manifest_sha256
            or installed.content_sha256 != target.content_sha256
            or installed.lock_sha256 != lock.sha256
        )
    )


def search_catalogs(catalogs: ResolvedCatalogSet, query: str) -> tuple[dict[str, object], ...]:
    rows = [
        {
            "catalog": resolved.catalog.id,
            "catalogSource": resolved.catalog.source,
            "description": entry.manifest.description,
            "identity": entry.identity,
        }
        for resolved, entry in catalogs.search(query)
    ]
    return tuple(sorted(rows, key=lambda row: (str(row["identity"]), str(row["catalogSource"]))))


def catalog_info(catalogs: ResolvedCatalogSet, coordinate: str) -> tuple[dict[str, object], ...]:
    if coordinate.count("/") != 1:
        raise _fail("SDAI-PACK-LIFECYCLE-006", "Pack coordinate must be publisher/id")
    publisher, pack_id = coordinate.split("/", 1)
    rows: list[dict[str, object]] = []
    for resolved in catalogs.catalogs:
        for entry in resolved.catalog.info(publisher, pack_id):
            rows.append(
                {
                    "catalog": resolved.catalog.id,
                    "catalogSource": resolved.catalog.source,
                    "contentSha256": entry.content_sha256,
                    "identity": entry.identity,
                    "manifest": entry.manifest.as_dict(),
                    "source": entry.source,
                }
            )
    return tuple(sorted(rows, key=lambda row: (str(row["identity"]), str(row["catalogSource"]))))
