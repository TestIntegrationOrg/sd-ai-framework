from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import tempfile
from typing import Iterable, Mapping

from sdai.pack_catalog import PackCatalogEntry, ResolvedCatalogSet
from sdai.pack_integrity import build_pack_content_index
from sdai.pack_lock import PackLock, PackLockEntry
from sdai.pack_manifest import PackManifest, load_pack_manifest


PACK_INSTALL_STATE_API_VERSION = "sdai.pack-install-state/v1"
PACK_INSTALL_RECORD_API_VERSION = "sdai.pack-install-record/v1"
PACK_LINK_RECORD_API_VERSION = "sdai.pack-local-link/v1"


class PackLifecycleError(RuntimeError):
    pass


def _fail(code: str, message: str) -> PackLifecycleError:
    return PackLifecycleError(f"{code}: {message}")


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise _fail("SDAI-PACK-LIFECYCLE-001", "state is not canonical finite JSON") from exc


def _hash_bytes(data: bytes) -> str:
    return "sha256:" + sha256(data).hexdigest()


def _safe_relative(value: str, *, label: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise _fail("SDAI-PACK-LIFECYCLE-002", f"{label} is not a portable relative path")
    pure = PurePosixPath(value)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in value.split("/")):
        raise _fail("SDAI-PACK-LIFECYCLE-002", f"{label} is not a safe relative path")
    return pure.as_posix()


def _managed_path(root: Path, relative: str) -> Path:
    safe = _safe_relative(relative, label="managed path")
    target = root.joinpath(*PurePosixPath(safe).parts)
    if target.is_symlink():
        raise _fail("SDAI-PACK-LIFECYCLE-002", f"managed path '{safe}' must not be a symlink")
    resolved_root = root.resolve()
    resolved_target = target.resolve(strict=False)
    try:
        resolved_target.relative_to(resolved_root)
    except ValueError as exc:
        raise _fail("SDAI-PACK-LIFECYCLE-002", f"managed path '{safe}' escapes project root") from exc
    return target


def _state_dir(root: Path) -> Path:
    return root / ".sdai" / "packs"


def _state_path(root: Path) -> Path:
    return _state_dir(root) / "install-state.json"


def _lock_path(root: Path) -> Path:
    return _state_dir(root) / "pack-lock.json"


def _journal_path(root: Path) -> Path:
    return _state_dir(root) / "operation-journal.json"


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise _fail("SDAI-PACK-LIFECYCLE-003", f"state path '{path}' must not be a symlink")
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False) as handle:
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
            temporary.unlink(missing_ok=True)


@dataclass(frozen=True)
class ManagedFile:
    path: str
    sha256: str
    source_path: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", _safe_relative(self.path, label="managed file path"))
        object.__setattr__(self, "source_path", _safe_relative(self.source_path, label="managed source path"))
        if not self.sha256.startswith("sha256:") or len(self.sha256) != 71:
            raise _fail("SDAI-PACK-LIFECYCLE-001", "managed file hash must be SHA-256")

    def as_dict(self) -> dict[str, str]:
        return {"path": self.path, "sha256": self.sha256, "sourcePath": self.source_path}

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> "ManagedFile":
        if set(raw) != {"path", "sha256", "sourcePath"}:
            raise _fail("SDAI-PACK-LIFECYCLE-001", "managed file contains unsupported or missing fields")
        return cls(str(raw["path"]), str(raw["sha256"]), str(raw["sourcePath"]))


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

    def __post_init__(self) -> None:
        if self.mode not in {"installed", "local-link"}:
            raise _fail("SDAI-PACK-LIFECYCLE-001", f"unsupported Pack mode '{self.mode}'")
        ordered = tuple(sorted(self.files, key=lambda item: item.path))
        if ordered != self.files or len({item.path for item in self.files}) != len(self.files):
            raise _fail("SDAI-PACK-LIFECYCLE-001", "Pack managed files must be unique and sorted")
        if self.mode == "local-link" and not self.local_path:
            raise _fail("SDAI-PACK-LIFECYCLE-001", "local-link Pack requires localPath provenance")
        if self.mode == "installed" and self.local_path is not None:
            raise _fail("SDAI-PACK-LIFECYCLE-001", "installed Pack must not carry localPath provenance")

    def as_dict(self) -> dict[str, object]:
        return {
            "apiVersion": PACK_INSTALL_RECORD_API_VERSION if self.mode == "installed" else PACK_LINK_RECORD_API_VERSION,
            "contentSha256": self.content_sha256,
            "coordinate": self.coordinate,
            "files": [item.as_dict() for item in self.files],
            "identity": self.identity,
            "localPath": self.local_path,
            "lockSha256": self.lock_sha256,
            "manifestSha256": self.manifest_sha256,
            "mode": self.mode,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> "InstalledPack":
        expected = {"apiVersion", "contentSha256", "coordinate", "files", "identity", "localPath", "lockSha256", "manifestSha256", "mode", "source"}
        if set(raw) != expected:
            raise _fail("SDAI-PACK-LIFECYCLE-001", "installed Pack contains unsupported or missing fields")
        files = raw["files"]
        if not isinstance(files, list):
            raise _fail("SDAI-PACK-LIFECYCLE-001", "installed Pack files must be a list")
        return cls(
            identity=str(raw["identity"]), coordinate=str(raw["coordinate"]), mode=str(raw["mode"]),
            source=str(raw["source"]), manifest_sha256=str(raw["manifestSha256"]),
            content_sha256=str(raw["contentSha256"]), lock_sha256=str(raw["lockSha256"]),
            files=tuple(ManagedFile.from_dict(item) for item in files if isinstance(item, Mapping)),
            local_path=None if raw["localPath"] is None else str(raw["localPath"]),
        )


@dataclass(frozen=True)
class PackInstallState:
    packs: tuple[InstalledPack, ...] = ()

    def __post_init__(self) -> None:
        coordinates = [item.coordinate for item in self.packs]
        if coordinates != sorted(coordinates) or len(set(coordinates)) != len(coordinates):
            raise _fail("SDAI-PACK-LIFECYCLE-001", "installed Packs must contain one sorted record per coordinate")

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
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            raise _fail("SDAI-PACK-LIFECYCLE-001", "Pack install state JSON is malformed") from exc
        if not isinstance(raw, Mapping) or set(raw) != {"apiVersion", "packs"} or raw["apiVersion"] != PACK_INSTALL_STATE_API_VERSION:
            raise _fail("SDAI-PACK-LIFECYCLE-001", "Pack install state contract is invalid")
        packs = raw["packs"]
        if not isinstance(packs, list):
            raise _fail("SDAI-PACK-LIFECYCLE-001", "Pack install state packs must be a list")
        return cls(tuple(InstalledPack.from_dict(item) for item in packs if isinstance(item, Mapping)))


def load_install_state(root: Path) -> PackInstallState:
    path = _state_path(root)
    if not path.exists():
        return PackInstallState()
    if path.is_symlink() or not path.is_file():
        raise _fail("SDAI-PACK-LIFECYCLE-003", "Pack install state must be a regular file")
    try:
        return PackInstallState.from_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError) as exc:
        raise _fail("SDAI-PACK-LIFECYCLE-003", "unable to read Pack install state as UTF-8") from exc


def _write_state(root: Path, state: PackInstallState) -> None:
    _atomic_write(_state_path(root), state.to_text().encode("utf-8"))


def _record_journal(root: Path, operation: str, coordinate: str) -> None:
    payload = _canonical_json({"apiVersion": "sdai.pack-operation-journal/v1", "coordinate": coordinate, "operation": operation}) + "\n"
    _atomic_write(_journal_path(root), payload.encode("utf-8"))


def _clear_journal(root: Path) -> None:
    _journal_path(root).unlink(missing_ok=True)


def _destination_path(relative_source: str) -> str:
    # Materialize Pack content under .sdai/installed-packs to prevent collisions with user-owned repository files.
    return _safe_relative(f".sdai/installed-packs/{relative_source}", label="Pack destination")


def _source_files(pack_root: Path, manifest: PackManifest) -> tuple[tuple[str, bytes], ...]:
    index = build_pack_content_index(pack_root, manifest)
    files: list[tuple[str, bytes]] = []
    root = pack_root.resolve()
    for entry in index.entries:
        source = root.joinpath(*PurePosixPath(entry.path).parts)
        files.append((entry.path, source.read_bytes()))
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
        raise _fail("SDAI-PACK-LIFECYCLE-003", f"unable to inspect managed file '{managed.path}'") from exc
    return "clean" if actual == managed.sha256 else "modified"


def _replace_record(state: PackInstallState, record: InstalledPack | None, coordinate: str) -> PackInstallState:
    items = [item for item in state.packs if item.coordinate != coordinate]
    if record is not None:
        items.append(record)
    return PackInstallState(tuple(sorted(items, key=lambda item: item.coordinate)))


def install_from_local(root: Path, pack_root: Path, lock: PackLock, lock_entry: PackLockEntry, *, local_link: bool = False) -> InstalledPack:
    manifest = load_pack_manifest(pack_root / "pack.yaml", pack_root=pack_root)
    if manifest.identity != lock_entry.identity:
        raise _fail("SDAI-PACK-LIFECYCLE-004", f"local Pack identity '{manifest.identity}' does not match lock '{lock_entry.identity}'")
    content_index = build_pack_content_index(pack_root, manifest)
    if content_index.sha256 != lock_entry.content_sha256:
        raise _fail("SDAI-PACK-LIFECYCLE-004", "local Pack content does not match exact lock content hash")
    if manifest.sha256 != lock_entry.manifest_sha256:
        raise _fail("SDAI-PACK-LIFECYCLE-004", "local Pack manifest does not match exact lock manifest hash")

    state = load_install_state(root)
    previous = next((item for item in state.packs if item.coordinate == lock_entry.coordinate), None)
    source_files = _source_files(pack_root, manifest)
    new_files: list[ManagedFile] = []
    source_prefix = f"{manifest.publisher}/{manifest.id}/{manifest.version}"

    _record_journal(root, "local-link" if local_link else "install", lock_entry.coordinate)
    try:
        previous_by_path = {item.path: item for item in previous.files} if previous else {}
        for source_relative, data in source_files:
            destination_relative = _destination_path(f"{source_prefix}/{source_relative}")
            destination = _managed_path(root, destination_relative)
            prior_file = previous_by_path.get(destination_relative)
            if destination.exists():
                if prior_file is None:
                    raise _fail("SDAI-PACK-LIFECYCLE-005", f"refusing to overwrite unmanaged file '{destination_relative}'")
                status = _verify_existing_file(root, prior_file)
                if status == "modified":
                    raise _fail("SDAI-PACK-LIFECYCLE-005", f"refusing to overwrite user-modified managed file '{destination_relative}'")
            destination.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(destination, data)
            new_files.append(ManagedFile(destination_relative, _hash_bytes(data), source_relative))

        # Remove obsolete clean managed files only; preserve user edits.
        new_paths = {item.path for item in new_files}
        if previous is not None:
            for old in previous.files:
                if old.path in new_paths:
                    continue
                if _verify_existing_file(root, old) == "clean":
                    _managed_path(root, old.path).unlink(missing_ok=True)

        record = InstalledPack(
            identity=manifest.identity,
            coordinate=manifest.coordinate,
            mode="local-link" if local_link else "installed",
            source="local-link:" + pack_root.resolve().as_posix() if local_link else lock_entry.source,
            manifest_sha256=manifest.sha256,
            content_sha256=content_index.sha256,
            lock_sha256=lock.sha256,
            files=tuple(sorted(new_files, key=lambda item: item.path)),
            local_path=pack_root.resolve().as_posix() if local_link else None,
        )
        _write_state(root, _replace_record(state, record, record.coordinate))
        _atomic_write(_lock_path(root), lock.to_text().encode("utf-8"))
        return record
    finally:
        _clear_journal(root)


def remove_pack(root: Path, coordinate: str) -> tuple[str, ...]:
    state = load_install_state(root)
    record = next((item for item in state.packs if item.coordinate == coordinate), None)
    if record is None:
        return ()
    preserved: list[str] = []
    _record_journal(root, "remove", coordinate)
    try:
        for managed in record.files:
            status = _verify_existing_file(root, managed)
            if status == "clean":
                _managed_path(root, managed.path).unlink(missing_ok=True)
            elif status == "modified":
                preserved.append(managed.path)
        _write_state(root, _replace_record(state, None, coordinate))
        return tuple(sorted(preserved))
    finally:
        _clear_journal(root)


def outdated_packs(state: PackInstallState, lock: PackLock) -> tuple[InstalledPack, ...]:
    exact = {item.coordinate: item for item in lock.packages}
    result: list[InstalledPack] = []
    for installed in state.packs:
        target = exact.get(installed.coordinate)
        if target is None or installed.identity != target.identity or installed.content_sha256 != target.content_sha256 or installed.lock_sha256 != lock.sha256:
            result.append(installed)
    return tuple(result)


def search_catalogs(catalogs: ResolvedCatalogSet, query: str) -> tuple[dict[str, object], ...]:
    return tuple({"catalog": resolved.catalog.id, "catalogSource": resolved.catalog.source, "identity": entry.identity, "description": entry.manifest.description} for resolved, entry in catalogs.search(query))


def catalog_info(catalogs: ResolvedCatalogSet, coordinate: str) -> tuple[dict[str, object], ...]:
    if coordinate.count("/") != 1:
        raise _fail("SDAI-PACK-LIFECYCLE-006", "Pack coordinate must be publisher/id")
    publisher, pack_id = coordinate.split("/", 1)
    rows: list[dict[str, object]] = []
    for resolved in catalogs.catalogs:
        for entry in resolved.catalog.info(publisher, pack_id):
            rows.append({"catalog": resolved.catalog.id, "catalogSource": resolved.catalog.source, "identity": entry.identity, "manifest": entry.manifest.as_dict(), "contentSha256": entry.content_sha256, "source": entry.source})
    return tuple(rows)
