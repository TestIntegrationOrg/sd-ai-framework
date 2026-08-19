from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Iterable, Mapping

from sdai.audit_contracts import AuditProvenanceError
from sdai.audit_ledger import AuditLedger


AUDIT_EXPORT_API_VERSION = "sdai.audit-export/v1"
AUDIT_EXPORT_RECEIPT_API_VERSION = "sdai.audit-export-receipt/v1"
AUDIT_EXPORT_DEFAULT_CHUNK_BYTES = 1024 * 1024
AUDIT_EXPORT_MIN_CHUNK_BYTES = 1024
AUDIT_EXPORT_MAX_CHUNK_BYTES = 4 * 1024 * 1024
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_EXPORT_ID = re.compile(r"^audit-export-[0-9a-f]{64}$")


class AuditExportError(RuntimeError):
    """Raised when immutable audit export packaging or validation fails."""


def _fail(code: str, message: str) -> AuditExportError:
    return AuditExportError(f"{code}: {message}")


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
        raise _fail("SDAI-AUDIT-EXPORT-001", "audit export value is not canonical JSON") from exc


def _hash_bytes(value: bytes) -> str:
    return "sha256:" + sha256(value).hexdigest()


def _hash_json(value: object) -> str:
    return _hash_bytes(_canonical_bytes(value))


def _require_sha(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise _fail("SDAI-AUDIT-EXPORT-002", f"{label} must be a lowercase SHA-256 identity")
    return value


def _require_int(value: object, *, label: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise _fail("SDAI-AUDIT-EXPORT-002", f"{label} must be an integer >= {minimum}")
    return value


def _export_id(feature_id: str, head_sha256: str, export_sha256: str) -> str:
    identity = {
        "featureId": feature_id,
        "ledgerHeadSha256": head_sha256,
        "exportSha256": export_sha256,
    }
    return "audit-export-" + sha256(_canonical_bytes(identity)).hexdigest()


def _validate_canonical_jsonl(value: bytes, *, event_count: int) -> None:
    if not value:
        if event_count != 0:
            raise _fail("SDAI-AUDIT-EXPORT-004", "eventCount does not match canonical JSONL record count")
        return
    if not value.endswith(b"\n"):
        raise _fail("SDAI-AUDIT-EXPORT-004", "canonical audit JSONL must end with LF")
    records = value[:-1].split(b"\n")
    if len(records) != event_count:
        raise _fail("SDAI-AUDIT-EXPORT-004", "eventCount does not match canonical JSONL record count")
    for record in records:
        if not record:
            raise _fail("SDAI-AUDIT-EXPORT-004", "canonical audit JSONL contains an empty record")
        try:
            decoded = record.decode("utf-8")
            parsed = json.loads(decoded)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise _fail("SDAI-AUDIT-EXPORT-004", "canonical audit JSONL contains an invalid record") from exc
        if _canonical_bytes(parsed) != record:
            raise _fail("SDAI-AUDIT-EXPORT-004", "canonical audit JSONL contains a non-canonical record")


@dataclass(frozen=True, slots=True)
class AuditExportChunk:
    index: int
    name: str
    offset: int
    byte_length: int
    sha256: str

    def __post_init__(self) -> None:
        if self.index < 0:
            raise _fail("SDAI-AUDIT-EXPORT-002", "chunk index must be >= 0")
        expected_name = f"chunk-{self.index:06d}.bin"
        if self.name != expected_name:
            raise _fail("SDAI-AUDIT-EXPORT-002", f"chunk name must be {expected_name!r}")
        if self.offset < 0 or self.byte_length < 0:
            raise _fail("SDAI-AUDIT-EXPORT-002", "chunk offset/length must be >= 0")
        _require_sha(self.sha256, label="chunk sha256")

    def as_dict(self) -> dict[str, object]:
        return {
            "index": self.index,
            "name": self.name,
            "offset": self.offset,
            "byteLength": self.byte_length,
            "sha256": self.sha256,
        }

    @classmethod
    def from_mapping(cls, value: object) -> "AuditExportChunk":
        if not isinstance(value, Mapping):
            raise _fail("SDAI-AUDIT-EXPORT-002", "chunk must be a mapping")
        expected = {"index", "name", "offset", "byteLength", "sha256"}
        if set(value) != expected:
            raise _fail("SDAI-AUDIT-EXPORT-002", "chunk fields do not match sdai.audit-export/v1")
        return cls(
            index=_require_int(value["index"], label="chunk index"),
            name=str(value["name"]),
            offset=_require_int(value["offset"], label="chunk offset"),
            byte_length=_require_int(value["byteLength"], label="chunk byteLength"),
            sha256=_require_sha(value["sha256"], label="chunk sha256"),
        )


@dataclass(frozen=True, slots=True)
class AuditExportManifest:
    export_id: str
    feature_id: str
    event_count: int
    ledger_head_sha256: str
    export_sha256: str
    byte_length: int
    chunk_size: int
    chunks: tuple[AuditExportChunk, ...]
    manifest_sha256: str

    def _unsigned(self) -> dict[str, object]:
        return {
            "apiVersion": AUDIT_EXPORT_API_VERSION,
            "exportId": self.export_id,
            "featureId": self.feature_id,
            "eventCount": self.event_count,
            "ledgerHeadSha256": self.ledger_head_sha256,
            "exportSha256": self.export_sha256,
            "byteLength": self.byte_length,
            "chunkSize": self.chunk_size,
            "chunkCount": len(self.chunks),
            "chunks": [chunk.as_dict() for chunk in self.chunks],
        }

    def __post_init__(self) -> None:
        if _EXPORT_ID.fullmatch(self.export_id) is None:
            raise _fail("SDAI-AUDIT-EXPORT-002", "exportId is invalid")
        if not isinstance(self.feature_id, str) or not self.feature_id:
            raise _fail("SDAI-AUDIT-EXPORT-002", "featureId is required")
        _require_int(self.event_count, label="eventCount")
        _require_sha(self.ledger_head_sha256, label="ledgerHeadSha256")
        _require_sha(self.export_sha256, label="exportSha256")
        _require_int(self.byte_length, label="byteLength")
        if not AUDIT_EXPORT_MIN_CHUNK_BYTES <= self.chunk_size <= AUDIT_EXPORT_MAX_CHUNK_BYTES:
            raise _fail(
                "SDAI-AUDIT-EXPORT-002",
                f"chunkSize must be between {AUDIT_EXPORT_MIN_CHUNK_BYTES} and {AUDIT_EXPORT_MAX_CHUNK_BYTES}",
            )
        _require_sha(self.manifest_sha256, label="manifestSha256")
        expected_id = _export_id(self.feature_id, self.ledger_head_sha256, self.export_sha256)
        if self.export_id != expected_id:
            raise _fail("SDAI-AUDIT-EXPORT-003", "exportId does not match feature/head/export identity")
        if self.manifest_sha256 != _hash_json(self._unsigned()):
            raise _fail("SDAI-AUDIT-EXPORT-003", "manifestSha256 does not match canonical manifest body")
        offset = 0
        for index, chunk in enumerate(self.chunks):
            if chunk.index != index:
                raise _fail("SDAI-AUDIT-EXPORT-003", "chunks must use contiguous zero-based indexes")
            if chunk.offset != offset:
                raise _fail("SDAI-AUDIT-EXPORT-003", "chunk offsets are not contiguous")
            if chunk.byte_length <= 0:
                raise _fail("SDAI-AUDIT-EXPORT-003", "non-empty export chunks must have positive byteLength")
            if chunk.byte_length > self.chunk_size:
                raise _fail("SDAI-AUDIT-EXPORT-003", "chunk exceeds manifest chunkSize")
            offset += chunk.byte_length
        if offset != self.byte_length:
            raise _fail("SDAI-AUDIT-EXPORT-003", "chunk byte lengths do not cover the manifest byteLength")
        if self.byte_length == 0 and self.chunks:
            raise _fail("SDAI-AUDIT-EXPORT-003", "empty export must not contain chunks")
        if self.byte_length > 0 and not self.chunks:
            raise _fail("SDAI-AUDIT-EXPORT-003", "non-empty export must contain at least one chunk")

    def as_dict(self) -> dict[str, object]:
        body = self._unsigned()
        body["manifestSha256"] = self.manifest_sha256
        return body

    def to_json(self) -> str:
        return _canonical_bytes(self.as_dict()).decode("utf-8") + "\n"

    @classmethod
    def create(
        cls,
        *,
        feature_id: str,
        event_count: int,
        ledger_head_sha256: str,
        export_sha256: str,
        byte_length: int,
        chunk_size: int,
        chunks: tuple[AuditExportChunk, ...],
    ) -> "AuditExportManifest":
        export_id = _export_id(feature_id, ledger_head_sha256, export_sha256)
        unsigned = {
            "apiVersion": AUDIT_EXPORT_API_VERSION,
            "exportId": export_id,
            "featureId": feature_id,
            "eventCount": event_count,
            "ledgerHeadSha256": ledger_head_sha256,
            "exportSha256": export_sha256,
            "byteLength": byte_length,
            "chunkSize": chunk_size,
            "chunkCount": len(chunks),
            "chunks": [chunk.as_dict() for chunk in chunks],
        }
        return cls(
            export_id=export_id,
            feature_id=feature_id,
            event_count=event_count,
            ledger_head_sha256=ledger_head_sha256,
            export_sha256=export_sha256,
            byte_length=byte_length,
            chunk_size=chunk_size,
            chunks=chunks,
            manifest_sha256=_hash_json(unsigned),
        )

    @classmethod
    def from_json(cls, value: str | bytes) -> "AuditExportManifest":
        try:
            raw = json.loads(value)
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError) as exc:
            raise _fail("SDAI-AUDIT-EXPORT-002", "audit export manifest is invalid JSON") from exc
        if not isinstance(raw, Mapping):
            raise _fail("SDAI-AUDIT-EXPORT-002", "audit export manifest must be a mapping")
        expected = {
            "apiVersion", "exportId", "featureId", "eventCount", "ledgerHeadSha256",
            "exportSha256", "byteLength", "chunkSize", "chunkCount", "chunks", "manifestSha256",
        }
        if set(raw) != expected or raw.get("apiVersion") != AUDIT_EXPORT_API_VERSION:
            raise _fail("SDAI-AUDIT-EXPORT-002", "manifest fields/version do not match sdai.audit-export/v1")
        chunks_raw = raw.get("chunks")
        if not isinstance(chunks_raw, list):
            raise _fail("SDAI-AUDIT-EXPORT-002", "manifest chunks must be a list")
        chunks = tuple(AuditExportChunk.from_mapping(item) for item in chunks_raw)
        chunk_count = _require_int(raw.get("chunkCount"), label="chunkCount")
        if chunk_count != len(chunks):
            raise _fail("SDAI-AUDIT-EXPORT-003", "chunkCount does not match chunks")
        return cls(
            export_id=str(raw["exportId"]),
            feature_id=str(raw["featureId"]),
            event_count=_require_int(raw["eventCount"], label="eventCount"),
            ledger_head_sha256=_require_sha(raw["ledgerHeadSha256"], label="ledgerHeadSha256"),
            export_sha256=_require_sha(raw["exportSha256"], label="exportSha256"),
            byte_length=_require_int(raw["byteLength"], label="byteLength"),
            chunk_size=_require_int(raw["chunkSize"], label="chunkSize", minimum=1),
            chunks=chunks,
            manifest_sha256=_require_sha(raw["manifestSha256"], label="manifestSha256"),
        )


@dataclass(frozen=True, slots=True)
class AuditExportPackage:
    manifest: AuditExportManifest
    chunk_bytes: tuple[bytes, ...]

    def __post_init__(self) -> None:
        validate_audit_export_package(self)

    def iter_chunks(self) -> Iterable[tuple[AuditExportChunk, bytes]]:
        return zip(self.manifest.chunks, self.chunk_bytes, strict=True)


def validate_audit_export_package(package: AuditExportPackage) -> None:
    manifest = package.manifest
    if len(package.chunk_bytes) != len(manifest.chunks):
        raise _fail("SDAI-AUDIT-EXPORT-004", "package chunk count does not match manifest")
    assembled = bytearray()
    for descriptor, content in zip(manifest.chunks, package.chunk_bytes, strict=True):
        if len(content) != descriptor.byte_length:
            raise _fail("SDAI-AUDIT-EXPORT-004", f"chunk {descriptor.index} byte length mismatch")
        if _hash_bytes(content) != descriptor.sha256:
            raise _fail("SDAI-AUDIT-EXPORT-004", f"chunk {descriptor.index} SHA-256 mismatch")
        assembled.extend(content)
    assembled_bytes = bytes(assembled)
    if len(assembled_bytes) != manifest.byte_length:
        raise _fail("SDAI-AUDIT-EXPORT-004", "assembled export byte length mismatch")
    if _hash_bytes(assembled_bytes) != manifest.export_sha256:
        raise _fail("SDAI-AUDIT-EXPORT-004", "assembled export SHA-256 mismatch")
    _validate_canonical_jsonl(assembled_bytes, event_count=manifest.event_count)


def build_audit_export_package(
    project_root: Path,
    feature_id: str,
    *,
    chunk_size: int = AUDIT_EXPORT_DEFAULT_CHUNK_BYTES,
) -> AuditExportPackage:
    if isinstance(chunk_size, bool) or not isinstance(chunk_size, int):
        raise _fail("SDAI-AUDIT-EXPORT-002", "chunk size must be an integer")
    if not AUDIT_EXPORT_MIN_CHUNK_BYTES <= chunk_size <= AUDIT_EXPORT_MAX_CHUNK_BYTES:
        raise _fail(
            "SDAI-AUDIT-EXPORT-002",
            f"chunk size must be between {AUDIT_EXPORT_MIN_CHUNK_BYTES} and {AUDIT_EXPORT_MAX_CHUNK_BYTES}",
        )
    try:
        ledger = AuditLedger(project_root, feature_id)
        before = ledger.verify()
        export = ledger.export_jsonl()
        after = ledger.verify()
    except AuditProvenanceError:
        raise
    if (
        before.event_count != after.event_count
        or before.head_sha256 != after.head_sha256
        or before.export_sha256 != after.export_sha256
    ):
        raise _fail("SDAI-AUDIT-EXPORT-005", "audit ledger changed while immutable export was being packaged")
    export_sha = _hash_bytes(export)
    if export_sha != before.export_sha256:
        raise _fail("SDAI-AUDIT-EXPORT-005", "canonical export bytes do not match verified ledger export SHA-256")
    _validate_canonical_jsonl(export, event_count=before.event_count)

    chunks: list[AuditExportChunk] = []
    contents: list[bytes] = []
    for index, offset in enumerate(range(0, len(export), chunk_size)):
        content = export[offset : offset + chunk_size]
        chunks.append(
            AuditExportChunk(
                index=index,
                name=f"chunk-{index:06d}.bin",
                offset=offset,
                byte_length=len(content),
                sha256=_hash_bytes(content),
            )
        )
        contents.append(content)
    manifest = AuditExportManifest.create(
        feature_id=ledger.feature_id,
        event_count=before.event_count,
        ledger_head_sha256=before.head_sha256,
        export_sha256=before.export_sha256,
        byte_length=len(export),
        chunk_size=chunk_size,
        chunks=tuple(chunks),
    )
    return AuditExportPackage(manifest, tuple(contents))


__all__ = [
    "AUDIT_EXPORT_API_VERSION",
    "AUDIT_EXPORT_RECEIPT_API_VERSION",
    "AUDIT_EXPORT_DEFAULT_CHUNK_BYTES",
    "AuditExportChunk",
    "AuditExportError",
    "AuditExportManifest",
    "AuditExportPackage",
    "build_audit_export_package",
    "validate_audit_export_package",
]
