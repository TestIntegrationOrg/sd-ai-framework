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


def _valid_hash(value: str) -> bool:
    return (
        value.startswith("sha256:")
        and len(value) == 71
        and all(char in "0123456789abcdef" for char in value[7:])
    )


def _safe_relative(value: str, *, label: str) -> str:
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
    resolved_target = current.resolve(strict=False)
    try:
        resolved_target.relative_to(resolved_root)
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
    def from_dict(cls, raw: Mapping[str, object]) -> "ManagedFile":
        if set(raw) != {"path", "sha256", "sourcePath"}:
            raise _fail(
                "SDAI-PACK-LIFECYCLE-001",
                "managed file contains unsupported or missing fields",
            )
        if not all(isinstance(raw[key], str) for key in ("path", "sha256", "sourcePath")):
            raise _fail("SDAI-PACK-LIFECYCLE-001", "managed file fields must be strings")
        return cls(raw["path"], raw["sha256"], raw["sourcePath"])  # type: ignore[arg-type]


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
        if not self.identity.startswith(self.coordinate + "@"):
            raise _fail("SDAI-PACK-LIFECYCLE-001", "Pack identity does not match coordinate")
        for value, label in (
            (self.manifest_sha256, "manifestSha256"),
            (self.content_sha256, "contentSha256"),
            (self.lock_sha256, "lockSha256"),
        ):
            if not _valid_hash(value):
                raise _fail("SDAI-PACK-LIFECYCLE-001", f"{label} must be SHA-256")
        ordered = tuple(sorted(self.files, key=lambda item: item.path))
        if ordered != self.files or len({item.path for item in self.files}) != len(self.files):
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
            "apiVersion": (
                PACK_INSTALL_RECORD_API_VERSION
                if self.mode == "installed"
                else PACK_LINK_RECORD_API_VERSION
            ),
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
    def from_dict(cls, raw: Mapping[str, object]) -> "InstalledPack":
        expected = {
            "apiVersion",
            "contentSha256",
            "coordinate",
            "files",
            "identity",
            "localPath",
            "lockSha256",
            "manifestSha256",
            "mode",
            "preservedPaths",
            "source",
        }
        if set(raw) != expected:
            raise _fail(
                "SDAI-PACK-LIFECYCLE-001",
                "installed Pack contains unsupported or missing fields",
            )
        mode = raw["mode"]
        expected_api = (
            PACK_INSTALL_RECORD_API_VERSION if mode == "installed" else PACK_LINK_RECORD_API_VERSION
        )
        if raw["apiVersion"] != expected_api:
            raise _fail("SDAI-PACK-LIFECYCLE-001", "installed Pack apiVersion/mode mismatch")
        files = raw["files"]
        preserved = raw["preservedPaths"]
        if not isinstance(files, list) or not all(isinstance(item, Mapping) for item in files):
            raise _fail("SDAI-PACK-LIFECYCLE-001", "installed Pack files must be objects")
        if not isinstance(preserved, list) or not all(isinstance(item, str) for item in preserved):
            raise _fail("SDAI-PACK-LIFECYCLE-001", "preservedPaths must be strings")
        string_fields = ("identity", "coordinate", "mode", "source", "manifestSha256", "contentSha256", "lockSha256")
        if not all(isinstance(raw[key], str) for key in string_fields):
            raise _fail("SDAI-PACK-LIFECYCLE-001", "installed Pack scalar fields are invalid")
        local_path = raw["localPath"]
        if local_path is not None and not isinstance(local_path, str):
            raise _fail("SDAI-PACK-LIFECYCLE-001", "localPath must be a string or null")
        return cls(
            identity=raw["identity"],  # type: ignore[arg-type]
            coordinate=raw["coordinate"],  # type: ignore[arg-type]
            mode=raw["mode"],  # type: ignore[arg-type]
            source=raw["source"],  # type: ignore[arg-type]
            manifest_sha256=raw["manifestSha256"],  # type: ignore[arg-type]
            content_sha256=raw["contentSha256"],  # type: ignore[arg-type]
            lock_sha256=raw["lockSha256"],  # type: ignore[arg-type]
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
            raise _fail(
                "SDAI-PACK-LIFECYCLE-001",
                "installed Packs must contain one sorted record per coordinate",
            )

    def as_dict(self) -> dict[str, object]:
        return {
            "apiVersion": PACK_INSTALL_STATE_API_VERSION,
            "packs": [item.as_dict() for item in self.packs],
        }

    def to_text(self) -> str:
        return _canonical_json(self.as_dict()) + "\n"

    @property
    def sha256(self) -> str:
        return _hash_bytes(self.to_text().encode("utf-8"))

    @classmethod
    def from_json(cls, text: str) -> "PackInstallState":
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            raise _fail("SDAI-PACK-LIFECYCLE-001", "Pack install state JSON is malformed") from exc
        if (
            not isinstance(raw, Mapping)
            or set(raw) != {"apiVersion", "packs"}
            or raw["apiVersion"] != PACK_INSTALL_STATE_API_VERSION
        ):
            raise _fail("SDAI-PACK-LIFECYCLE-001", "Pack install state contract is invalid")
        packs = raw["packs"]
        if not isinstance(packs, list) or not all(isinstance(item, Mapping) for item in packs):
            raise _fail("SDAI-PACK-LIFECYCLE-001", "Pack install state packs must be objects")
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
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            raise _fail("SDAI-PACK-LIFECYCLE-007", "operation journal JSON is malformed") from exc
        expected = {"apiVersion", "coordinate", "files", "identity", "operation"}
        if not isinstance(raw, Mapping) or set(raw) != expected or raw["apiVersion"] != PACK_OPERATION_JOURNAL_API_VERSION:
            raise _fail("SDAI-PACK-LIFECYCLE-007", "operation journal contract is invalid")
        files = raw["files"]
        if not isinstance(files, list) or not all(isinstance(item, Mapping) for item in files):
            raise _fail("SDAI-PACK-LIFECYCLE-007", "operation journal files are invalid")
        if not all(isinstance(raw[key], str) for key in ("operation", "coordinate", "identity")):
            raise _fail("SDAI-PACK-LIFECYCLE-007", "operation journal scalar fields are invalid")
        return cls(
            operation=raw["operation"],  # type: ignore[arg-type]
            coordinate=raw["coordinate"],  # type: ignore[arg-type]
            identity=raw["identity"],  # type: ignore[arg-type]
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
    return _safe_relative(
        f".sdai/installed-packs/{relative_source}",
        label="Pack destination",
    )


def _source_files(pack_root: Path, manifest: PackManifest) -> tuple[tuple[str, bytes], ...]:
    index = build_pack_content_index(pack_root, manifest)
    files: list[tuple[str, bytes]] = []
    resolved_root = pack_root.resolve()
    for entry in index.entries:
        source = resolved_root.joinpath(*PurePosixPath(entry.path).parts)
        try:
            data = source.read_bytes()
        except OSError as exc:
            raise _fail(
                "SDAI-PACK-LIFECYCLE-003",
                f"unable to read Pack source file '{entry.path}'",
            ) from exc
        if _hash_bytes(data) != entry.sha256:
            raise _fail("SDAI-PACK-LIFECYCLE-004", f"Pack source changed while installing '{entry.path}'")
        files.append((entry.path, data))
    return tuple(files)


def _verify_existing_file(root: Path, managed: ManagedFile) -> str:
    path = _managed_path(root, managed.path)
    if not path.exists():
        return "missing"
    if not path.is_file():
        return "modified"
    try:
        actual = _hash_bytes(path.read_bytes())
    except OSError as exc:
        raise _fail(
            "SDAI-PACK-LIFECYCLE-003",
            f"unable to inspect managed file '{managed.path}'",
        ) from exc
    return "clean" if actual == managed.sha256 else "modified"


def _replace_record(
    state: PackInstallState,
    record: InstalledPack | None,
    coordinate: str,
) -> PackInstallState:
    items = [item for item in state.packs if item.coordinate != coordinate]
    if record is not None:
        items.append(record)
    return PackInstallState(tuple(sorted(items, key=lambda item: item.coordinate)))


def _find_lock_entry(lock: PackLock, coordinate: str) -> PackLockEntry:
    matches = [entry for entry in lock.packages if entry.coordinate == coordinate]
    if len(matches) != 1:
        raise _fail(
            "SDAI-PACK-LIFECYCLE-004",
            f"Pack coordinate '{coordinate}' is not present exactly once in the lock",
        )
    return matches[0]


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
        raise _fail(
            "SDAI-PACK-LIFECYCLE-004",
            f"local Pack identity '{manifest.identity}' does not match lock '{lock_entry.identity}'",
        )
    content_index = build_pack_content_index(pack_root, manifest)
    if content_index.sha256 != lock_entry.content_sha256:
        raise _fail(
            "SDAI-PACK-LIFECYCLE-004",
            "local Pack content does not match exact lock content hash",
        )
    if manifest.sha256 != lock_entry.manifest_sha256:
        raise _fail(
            "SDAI-PACK-LIFECYCLE-004",
            "local Pack manifest does not match exact lock manifest hash",
        )

    state = load_install_state(root)
    previous = next((item for item in state.packs if item.coordinate == coordinate), None)
    source_files = _source_files(pack_root, manifest)
    source_prefix = f"{manifest.publisher}/{manifest.id}/{manifest.version}"
    planned = tuple(
        sorted(
            (
                ManagedFile(
                    _destination_path(f"{source_prefix}/{source_relative}"),
                    _hash_bytes(data),
                    source_relative,
                )
                for source_relative, data in source_files
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

    journal = OperationJournal(
        operation="local-link" if local_link else ("update" if previous else "install"),
        coordinate=coordinate,
        identity=manifest.identity,
        files=planned,
    )
    _write_journal(root, journal)
    previous_by_path = {item.path: item for item in previous.files} if previous else {}
    planned_by_path = {item.path: item for item in planned}
    data_by_source = dict(source_files)
    preserved: set[str] = set(previous.preserved_paths if previous else ())

    for managed in planned:
        destination = _managed_path(root, managed.path)
        prior = previous_by_path.get(managed.path)
        if destination.exists():
            if prior is not None:
                if _verify_existing_file(root, prior) == "modified":
                    raise _fail(
                        "SDAI-PACK-LIFECYCLE-005",
                        f"refusing to overwrite user-modified managed file '{managed.path}'",
                    )
            elif stale is not None:
                try:
                    actual = _hash_bytes(destination.read_bytes()) if destination.is_file() else ""
                except OSError as exc:
                    raise _fail("SDAI-PACK-LIFECYCLE-003", f"unable to recover '{managed.path}'") from exc
                if actual != managed.sha256:
                    raise _fail(
                        "SDAI-PACK-LIFECYCLE-005",
                        f"interrupted output '{managed.path}' no longer matches planned bytes",
                    )
            else:
                raise _fail(
                    "SDAI-PACK-LIFECYCLE-005",
                    f"refusing to overwrite unmanaged file '{managed.path}'",
                )
        destination.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(destination, data_by_source[managed.source_path])

    if previous is not None:
        for old in previous.files:
            if old.path in planned_by_path:
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
        source=(
            "local-link:" + pack_root.resolve().as_posix()
            if local_link
            else lock_entry.source
        ),
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
    if record is None:
        stale = _load_journal(root)
        if stale is not None and stale.coordinate == coordinate and stale.operation == "remove":
            _clear_journal(root)
        return ()

    stale = _load_journal(root)
    if stale is not None and (stale.coordinate != coordinate or stale.operation != "remove"):
        raise _fail(
            "SDAI-PACK-LIFECYCLE-007",
            f"incomplete Pack operation for '{stale.coordinate}' must be recovered first",
        )
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
    result: list[InstalledPack] = []
    for installed in state.packs:
        target = exact.get(installed.coordinate)
        if (
            target is None
            or installed.identity != target.identity
            or installed.manifest_sha256 != target.manifest_sha256
            or installed.content_sha256 != target.content_sha256
            or installed.lock_sha256 != lock.sha256
        ):
            result.append(installed)
    return tuple(result)


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
