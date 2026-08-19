from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import threading
from typing import Mapping, Protocol

from sdai.audit_contracts import AuditProvenanceError
from sdai.audit_export import (
    AUDIT_EXPORT_API_VERSION,
    AUDIT_EXPORT_RECEIPT_API_VERSION,
    AuditExportError,
    AuditExportManifest,
    AuditExportPackage,
    build_audit_export_package,
    validate_audit_export_package,
)
from sdai.audit_ledger import AuditLedger


_SINK_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_RECEIPT_ID = re.compile(r"^audit-receipt-[0-9a-f]{64}$")
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_BINARY_FLAG = getattr(os, "O_BINARY", 0)


class AuditSinkError(RuntimeError):
    """Raised when immutable audit handoff cannot be completed or verified."""


def _fail(code: str, message: str) -> AuditSinkError:
    return AuditSinkError(f"{code}: {message}")


def _canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise _fail("SDAI-AUDIT-SINK-001", "audit sink value is not canonical JSON") from exc


def _hash_bytes(value: bytes) -> str:
    return "sha256:" + sha256(value).hexdigest()


def _receipt_id(sink_id: str, export_id: str, manifest_sha256: str) -> str:
    payload = {"sinkId": sink_id, "exportId": export_id, "manifestSha256": manifest_sha256}
    return "audit-receipt-" + sha256(_canonical_bytes(payload)).hexdigest()


def _validate_sink_id(value: object) -> str:
    if not isinstance(value, str) or _SINK_ID.fullmatch(value) is None:
        raise _fail("SDAI-AUDIT-SINK-002", "sink id must use lowercase letters, numbers, dot, underscore, or hyphen")
    return value


def _reject_symlink_components(path: Path) -> None:
    candidate = path if path.is_absolute() else Path.cwd() / path
    current = candidate
    while True:
        if current.is_symlink():
            raise _fail("SDAI-AUDIT-SINK-004", "local sink destination contains a symlink component")
        parent = current.parent
        if parent == current:
            break
        current = parent


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise _fail("SDAI-AUDIT-SINK-005", "unable to open sink directory for durability sync") from exc
    try:
        os.fsync(fd)
    except OSError as exc:
        raise _fail("SDAI-AUDIT-SINK-005", "unable to sync sink directory") from exc
    finally:
        os.close(fd)


def _write_all(fd: int, value: bytes) -> None:
    view = memoryview(value)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise _fail("SDAI-AUDIT-SINK-005", "operating system returned a short/zero sink write")
        view = view[written:]


def _write_exclusive(path: Path, value: bytes) -> None:
    if path.exists() or path.is_symlink():
        raise _fail("SDAI-AUDIT-SINK-004", f"sink staging path already exists: {path.name}")
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | _BINARY_FLAG
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as exc:
        raise _fail("SDAI-AUDIT-SINK-005", f"unable to create sink file: {path.name}") from exc
    try:
        _write_all(fd, value)
        os.fsync(fd)
    finally:
        os.close(fd)


def _remove_staging(path: Path) -> None:
    if not path.exists() and not path.is_symlink():
        return
    if path.is_symlink() or not path.is_dir():
        raise _fail("SDAI-AUDIT-SINK-004", "partial sink path is unsafe")
    for child in path.iterdir():
        if child.is_symlink():
            raise _fail("SDAI-AUDIT-SINK-004", "partial sink contains a symlink")
        if child.is_dir():
            raise _fail("SDAI-AUDIT-SINK-004", "partial sink contains an unexpected directory")
    shutil.rmtree(path)


@dataclass(frozen=True, slots=True)
class AuditExportReceipt:
    sink_id: str
    export_id: str
    manifest_sha256: str
    chunk_sha256: tuple[str, ...]
    status: str
    receipt_id: str
    receipt_sha256: str

    def _unsigned(self) -> dict[str, object]:
        return {
            "apiVersion": AUDIT_EXPORT_RECEIPT_API_VERSION,
            "sinkId": self.sink_id,
            "exportId": self.export_id,
            "manifestSha256": self.manifest_sha256,
            "chunkSha256": list(self.chunk_sha256),
            "status": self.status,
            "receiptId": self.receipt_id,
        }

    def __post_init__(self) -> None:
        _validate_sink_id(self.sink_id)
        if not isinstance(self.export_id, str) or not self.export_id.startswith("audit-export-"):
            raise _fail("SDAI-AUDIT-SINK-002", "receipt exportId is invalid")
        if _SHA256.fullmatch(self.manifest_sha256) is None:
            raise _fail("SDAI-AUDIT-SINK-002", "receipt manifestSha256 is invalid")
        if any(_SHA256.fullmatch(value) is None for value in self.chunk_sha256):
            raise _fail("SDAI-AUDIT-SINK-002", "receipt chunk SHA-256 identity is invalid")
        if self.status not in {"accepted", "already-present"}:
            raise _fail("SDAI-AUDIT-SINK-002", "receipt status is invalid")
        expected_id = _receipt_id(self.sink_id, self.export_id, self.manifest_sha256)
        if self.receipt_id != expected_id or _RECEIPT_ID.fullmatch(self.receipt_id) is None:
            raise _fail("SDAI-AUDIT-SINK-003", "receiptId does not match sink/export/manifest identity")
        if self.receipt_sha256 != _hash_bytes(_canonical_bytes(self._unsigned())):
            raise _fail("SDAI-AUDIT-SINK-003", "receiptSha256 does not match canonical receipt body")

    @classmethod
    def create(cls, *, sink_id: str, package: AuditExportPackage, status: str) -> "AuditExportReceipt":
        sink = _validate_sink_id(sink_id)
        receipt_id = _receipt_id(sink, package.manifest.export_id, package.manifest.manifest_sha256)
        unsigned = {
            "apiVersion": AUDIT_EXPORT_RECEIPT_API_VERSION,
            "sinkId": sink,
            "exportId": package.manifest.export_id,
            "manifestSha256": package.manifest.manifest_sha256,
            "chunkSha256": [chunk.sha256 for chunk in package.manifest.chunks],
            "status": status,
            "receiptId": receipt_id,
        }
        return cls(
            sink_id=sink,
            export_id=package.manifest.export_id,
            manifest_sha256=package.manifest.manifest_sha256,
            chunk_sha256=tuple(chunk.sha256 for chunk in package.manifest.chunks),
            status=status,
            receipt_id=receipt_id,
            receipt_sha256=_hash_bytes(_canonical_bytes(unsigned)),
        )

    def as_dict(self) -> dict[str, object]:
        body = self._unsigned()
        body["receiptSha256"] = self.receipt_sha256
        return body

    def to_json(self) -> str:
        return _canonical_bytes(self.as_dict()).decode("utf-8") + "\n"


class AuditExportSink(Protocol):
    @property
    def sink_id(self) -> str: ...

    def handoff(self, package: AuditExportPackage) -> AuditExportReceipt: ...


class AuditExportSinkRegistry:
    def __init__(self) -> None:
        self._sinks: dict[str, AuditExportSink] = {}

    def register(self, sink: AuditExportSink, *, replace: bool = False) -> None:
        sink_id = _validate_sink_id(sink.sink_id)
        if sink_id in self._sinks and not replace:
            raise _fail("SDAI-AUDIT-SINK-002", f"audit sink {sink_id!r} is already registered")
        self._sinks[sink_id] = sink

    def get(self, sink_id: str) -> AuditExportSink:
        key = _validate_sink_id(sink_id)
        if key not in self._sinks:
            raise _fail("SDAI-AUDIT-SINK-002", f"audit sink {key!r} is not registered")
        return self._sinks[key]

    def ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._sinks))


class LocalFilesystemAuditSink:
    """Offline reference sink proving the extension/handoff contract."""

    def __init__(self, destination: Path, *, sink_id: str = "local-filesystem") -> None:
        self._sink_id = _validate_sink_id(sink_id)
        expanded = destination.expanduser()
        _reject_symlink_components(expanded)
        if expanded.exists() and not expanded.is_dir():
            raise _fail("SDAI-AUDIT-SINK-004", "local sink destination must be a directory")
        expanded.mkdir(parents=True, exist_ok=True)
        _reject_symlink_components(expanded)
        self.destination = expanded.resolve()

    @property
    def sink_id(self) -> str:
        return self._sink_id

    def _final_path(self, package: AuditExportPackage) -> Path:
        return self.destination / package.manifest.export_id

    def _legacy_staging_path(self, package: AuditExportPackage) -> Path:
        return self.destination / f".partial-{package.manifest.export_id}"

    def _staging_path(self, package: AuditExportPackage) -> Path:
        return self.destination / (
            f".partial-{package.manifest.export_id}-{os.getpid()}-{threading.get_ident()}"
        )

    def _load_existing(self, final: Path) -> AuditExportPackage:
        if final.is_symlink() or not final.is_dir():
            raise _fail("SDAI-AUDIT-SINK-004", "existing export path is unsafe")
        manifest_path = final / "manifest.json"
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise _fail("SDAI-AUDIT-SINK-004", "existing export manifest is missing or unsafe")
        try:
            raw_manifest = manifest_path.read_bytes()
            manifest = AuditExportManifest.from_json(raw_manifest)
        except (OSError, AuditExportError) as exc:
            raise _fail("SDAI-AUDIT-SINK-004", "existing export manifest is invalid") from exc
        if raw_manifest != manifest.to_json().encode("utf-8"):
            raise _fail("SDAI-AUDIT-SINK-004", "existing export manifest is not canonical JSON")
        chunks: list[bytes] = []
        expected_names = {"manifest.json", *(chunk.name for chunk in manifest.chunks)}
        actual_names: set[str] = set()
        for child in final.iterdir():
            if child.is_symlink() or not child.is_file():
                raise _fail("SDAI-AUDIT-SINK-004", "existing export contains unsafe entries")
            actual_names.add(child.name)
        if actual_names != expected_names:
            raise _fail("SDAI-AUDIT-SINK-004", "existing export file set does not match manifest")
        for chunk in manifest.chunks:
            try:
                chunks.append((final / chunk.name).read_bytes())
            except OSError as exc:
                raise _fail("SDAI-AUDIT-SINK-004", f"unable to read existing chunk {chunk.index}") from exc
        try:
            return AuditExportPackage(manifest, tuple(chunks))
        except AuditExportError as exc:
            raise _fail("SDAI-AUDIT-SINK-004", "existing export package failed integrity validation") from exc

    def _assert_same(self, current: AuditExportPackage, expected: AuditExportPackage) -> None:
        if current.manifest.to_json() != expected.manifest.to_json():
            raise _fail("SDAI-AUDIT-SINK-004", "existing export manifest differs from requested immutable export")
        if current.chunk_bytes != expected.chunk_bytes:
            raise _fail("SDAI-AUDIT-SINK-004", "existing export chunks differ from requested immutable export")

    def handoff(self, package: AuditExportPackage) -> AuditExportReceipt:
        validate_audit_export_package(package)
        final = self._final_path(package)
        staging = self._staging_path(package)
        legacy_staging = self._legacy_staging_path(package)
        if final.exists() or final.is_symlink():
            existing = self._load_existing(final)
            self._assert_same(existing, package)
            return AuditExportReceipt.create(sink_id=self.sink_id, package=package, status="already-present")

        # Recover only the legacy fixed staging directory used before concurrent
        # publication gained per-process/thread staging. Current writers never use
        # this path, so removing a safe leftover cannot race another current handoff.
        _remove_staging(legacy_staging)
        _remove_staging(staging)
        staging.mkdir(mode=0o700)
        published = False
        try:
            _write_exclusive(staging / "manifest.json", package.manifest.to_json().encode("utf-8"))
            for chunk, content in package.iter_chunks():
                _write_exclusive(staging / chunk.name, content)
            _fsync_directory(staging)
            if final.exists() or final.is_symlink():
                existing = self._load_existing(final)
                self._assert_same(existing, package)
                return AuditExportReceipt.create(sink_id=self.sink_id, package=package, status="already-present")
            try:
                staging.rename(final)
                published = True
            except OSError as exc:
                if final.exists() or final.is_symlink():
                    existing = self._load_existing(final)
                    self._assert_same(existing, package)
                    return AuditExportReceipt.create(sink_id=self.sink_id, package=package, status="already-present")
                raise _fail("SDAI-AUDIT-SINK-005", "unable to publish immutable export directory") from exc
            _fsync_directory(self.destination)
        finally:
            if not published and (staging.exists() or staging.is_symlink()):
                _remove_staging(staging)

        existing = self._load_existing(final)
        self._assert_same(existing, package)
        return AuditExportReceipt.create(sink_id=self.sink_id, package=package, status="accepted")


def _verify_receipt(receipt: AuditExportReceipt, sink: AuditExportSink, package: AuditExportPackage) -> None:
    if receipt.sink_id != sink.sink_id:
        raise _fail("SDAI-AUDIT-SINK-003", "sink receipt identifies a different sink")
    if receipt.export_id != package.manifest.export_id:
        raise _fail("SDAI-AUDIT-SINK-003", "sink receipt identifies a different export")
    if receipt.manifest_sha256 != package.manifest.manifest_sha256:
        raise _fail("SDAI-AUDIT-SINK-003", "sink receipt manifest SHA does not match package")
    expected_chunks = tuple(chunk.sha256 for chunk in package.manifest.chunks)
    if receipt.chunk_sha256 != expected_chunks:
        raise _fail("SDAI-AUDIT-SINK-003", "sink receipt chunk hashes do not match package")


def handoff_audit_export(
    project_root: Path,
    feature_id: str,
    sink: AuditExportSink,
    *,
    package: AuditExportPackage | None = None,
    chunk_size: int | None = None,
) -> AuditExportReceipt:
    """Build/verify one immutable export and hand it to a sink exactly once."""
    _validate_sink_id(sink.sink_id)
    kwargs = {} if chunk_size is None else {"chunk_size": chunk_size}
    export_package = package or build_audit_export_package(project_root, feature_id, **kwargs)
    validate_audit_export_package(export_package)
    try:
        ledger = AuditLedger(project_root, feature_id)
        before = ledger.verify()
    except AuditProvenanceError:
        raise
    manifest = export_package.manifest
    if (
        ledger.feature_id != manifest.feature_id
        or before.event_count != manifest.event_count
        or before.head_sha256 != manifest.ledger_head_sha256
        or before.export_sha256 != manifest.export_sha256
    ):
        raise _fail("SDAI-AUDIT-SINK-006", "current audit ledger does not match immutable export package")

    receipt = sink.handoff(export_package)
    _verify_receipt(receipt, sink, export_package)

    try:
        after = ledger.verify()
    except AuditProvenanceError:
        raise
    if (
        after.event_count != before.event_count
        or after.head_sha256 != before.head_sha256
        or after.export_sha256 != before.export_sha256
    ):
        raise _fail("SDAI-AUDIT-SINK-006", "audit ledger changed during immutable sink handoff")
    return receipt


__all__ = [
    "AuditExportReceipt",
    "AuditExportSink",
    "AuditExportSinkRegistry",
    "AuditSinkError",
    "LocalFilesystemAuditSink",
    "handoff_audit_export",
]
