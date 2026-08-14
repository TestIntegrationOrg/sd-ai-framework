from __future__ import annotations

import base64
from dataclasses import dataclass
from enum import Enum
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import re
from typing import Mapping, Protocol, runtime_checkable
import unicodedata

from sdai.pack_manifest import PackManifest, validate_pack_layout


PACK_CONTENT_API_VERSION = "sdai.pack-content/v1"
PACK_SIGNATURE_PAYLOAD_API_VERSION = "sdai.pack-signature-payload/v1"
PACK_SIGNATURE_API_VERSION = "sdai.pack-signature/v1"
PACK_SIGNATURE_VERIFICATION_API_VERSION = "sdai.pack-signature-verification/v1"


class PackIntegrityError(RuntimeError):
    pass


_HASH_PREFIX = "sha256:"
_ALGORITHM_RE = re.compile(r"^[a-z][a-z0-9]*(?:[-_.][a-z0-9]+)*$")
_SIGNATURE_KEYS = frozenset(
    {
        "apiVersion",
        "packIdentity",
        "publisher",
        "manifestSha256",
        "contentSha256",
        "payloadSha256",
        "algorithm",
        "keyId",
        "signature",
    }
)
_PAYLOAD_KEYS = frozenset(
    {"apiVersion", "packIdentity", "publisher", "manifestSha256", "contentSha256"}
)


def _fail(code: str, message: str) -> PackIntegrityError:
    return PackIntegrityError(f"{code}: {message}")


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
        raise _fail("SDAI-PACK-INTEGRITY-001", "value is not canonical finite JSON") from exc


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _fail("SDAI-PACK-INTEGRITY-001", f"JSON contains duplicate key '{key}'")
        result[key] = value
    return result


def _mapping(value: object, *, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise _fail("SDAI-PACK-INTEGRITY-001", f"{label} must be a string-keyed mapping")
    return value


def _keys(value: Mapping[str, object], *, expected: frozenset[str], label: str) -> None:
    unknown = sorted(set(value) - expected)
    missing = sorted(expected - set(value))
    if unknown:
        raise _fail(
            "SDAI-PACK-INTEGRITY-001",
            f"{label} contains unsupported field(s): {', '.join(unknown)}",
        )
    if missing:
        raise _fail(
            "SDAI-PACK-INTEGRITY-001",
            f"{label} is missing required field(s): {', '.join(missing)}",
        )


def _text(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _fail("SDAI-PACK-INTEGRITY-001", f"{label} must be a non-empty string")
    if "\x00" in value:
        raise _fail("SDAI-PACK-INTEGRITY-001", f"{label} must not contain NUL")
    return unicodedata.normalize("NFC", value.strip())


def _hash(value: object, *, label: str) -> str:
    text = _text(value, label=label)
    if not text.startswith(_HASH_PREFIX):
        raise _fail("SDAI-PACK-INTEGRITY-001", f"{label} must be a SHA-256 digest")
    digest = text[len(_HASH_PREFIX) :]
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise _fail("SDAI-PACK-INTEGRITY-001", f"{label} must be a lowercase SHA-256 digest")
    return text


def _algorithm(value: object) -> str:
    text = _text(value, label="signature algorithm")
    if not _ALGORITHM_RE.fullmatch(text):
        raise _fail(
            "SDAI-PACK-INTEGRITY-001",
            f"signature algorithm '{text}' is not a portable lowercase identifier",
        )
    return text


def _signature_bytes(value: object) -> bytes:
    text = _text(value, label="signature")
    try:
        decoded = base64.b64decode(text.encode("ascii"), validate=True)
    except (UnicodeEncodeError, ValueError) as exc:
        raise _fail("SDAI-PACK-INTEGRITY-001", "signature must be canonical Base64") from exc
    if not decoded:
        raise _fail("SDAI-PACK-INTEGRITY-001", "signature must not be empty")
    canonical = base64.b64encode(decoded).decode("ascii")
    if text != canonical:
        raise _fail("SDAI-PACK-INTEGRITY-001", "signature must use canonical padded Base64")
    return decoded


def _portable_file_path(relative: Path) -> str:
    value = unicodedata.normalize("NFC", relative.as_posix())
    pure = PurePosixPath(value)
    if pure.is_absolute() or not pure.parts:
        raise _fail("SDAI-PACK-INTEGRITY-002", f"Pack content path '{value}' is not relative")
    if any(part in {"", ".", ".."} for part in pure.parts):
        raise _fail("SDAI-PACK-INTEGRITY-002", f"Pack content path '{value}' is unsafe")
    if "\\" in value or "\x00" in value:
        raise _fail("SDAI-PACK-INTEGRITY-002", f"Pack content path '{value}' is not portable")
    return value


@dataclass(frozen=True)
class PackContentEntry:
    path: str
    sha256: str
    size: int

    def __post_init__(self) -> None:
        normalized = _portable_file_path(Path(self.path))
        object.__setattr__(self, "path", normalized)
        object.__setattr__(self, "sha256", _hash(self.sha256, label=f"content file '{normalized}' sha256"))
        if not isinstance(self.size, int) or isinstance(self.size, bool) or self.size < 0:
            raise _fail("SDAI-PACK-INTEGRITY-001", f"content file '{normalized}' size must be non-negative")

    def as_dict(self) -> dict[str, object]:
        return {"path": self.path, "sha256": self.sha256, "size": self.size}


@dataclass(frozen=True)
class PackContentIndex:
    entries: tuple[PackContentEntry, ...]

    def __post_init__(self) -> None:
        paths = [entry.path for entry in self.entries]
        if paths != sorted(paths):
            raise _fail("SDAI-PACK-INTEGRITY-001", "Pack content entries must be canonically sorted")
        if len(set(paths)) != len(paths):
            raise _fail("SDAI-PACK-INTEGRITY-001", "Pack content entries contain duplicate canonical paths")
        folded = [path.casefold() for path in paths]
        if len(set(folded)) != len(folded):
            raise _fail(
                "SDAI-PACK-INTEGRITY-002",
                "Pack content entries contain case-insensitive path collisions",
            )

    def as_dict(self) -> dict[str, object]:
        return {
            "apiVersion": PACK_CONTENT_API_VERSION,
            "files": [entry.as_dict() for entry in self.entries],
        }

    def to_json(self) -> str:
        return _canonical_json(self.as_dict())

    @property
    def sha256(self) -> str:
        return _HASH_PREFIX + sha256(self.to_json().encode("utf-8")).hexdigest()


def build_pack_content_index(pack_root: Path, manifest: PackManifest) -> PackContentIndex:
    validate_pack_layout(pack_root, manifest)
    root = pack_root.resolve()
    entries: list[PackContentEntry] = []
    seen_raw_paths: set[str] = set()

    for content_root in manifest.content_roots:
        declared_root = root / PurePosixPath(content_root)
        if declared_root.is_symlink():
            raise _fail("SDAI-PACK-INTEGRITY-002", f"declared content root '{content_root}' must not be a symlink")
        for current, directories, files in os.walk(declared_root, topdown=True, followlinks=False):
            current_path = Path(current)
            safe_directories: list[str] = []
            for directory in sorted(directories):
                child = current_path / directory
                if child.is_symlink():
                    raise _fail(
                        "SDAI-PACK-INTEGRITY-002",
                        f"Pack content directory '{child.relative_to(root).as_posix()}' must not be a symlink",
                    )
                safe_directories.append(directory)
            directories[:] = safe_directories

            for filename in sorted(files):
                file_path = current_path / filename
                if file_path.is_symlink():
                    raise _fail(
                        "SDAI-PACK-INTEGRITY-002",
                        f"Pack content file '{file_path.relative_to(root).as_posix()}' must not be a symlink",
                    )
                if not file_path.is_file():
                    raise _fail(
                        "SDAI-PACK-INTEGRITY-002",
                        f"Pack content entry '{file_path.relative_to(root).as_posix()}' is not a regular file",
                    )
                raw_relative = file_path.relative_to(root).as_posix()
                if raw_relative in seen_raw_paths:
                    continue
                seen_raw_paths.add(raw_relative)
                relative = _portable_file_path(Path(raw_relative))
                try:
                    data = file_path.read_bytes()
                except OSError as exc:
                    raise _fail(
                        "SDAI-PACK-INTEGRITY-002",
                        f"unable to read Pack content file '{relative}'",
                    ) from exc
                entries.append(
                    PackContentEntry(
                        path=relative,
                        sha256=_HASH_PREFIX + sha256(data).hexdigest(),
                        size=len(data),
                    )
                )

    entries.sort(key=lambda entry: entry.path)
    return PackContentIndex(tuple(entries))


@dataclass(frozen=True)
class PackSignaturePayload:
    pack_identity: str
    publisher: str
    manifest_sha256: str
    content_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "pack_identity", _text(self.pack_identity, label="packIdentity"))
        object.__setattr__(self, "publisher", _text(self.publisher, label="publisher"))
        object.__setattr__(self, "manifest_sha256", _hash(self.manifest_sha256, label="manifestSha256"))
        object.__setattr__(self, "content_sha256", _hash(self.content_sha256, label="contentSha256"))

    def as_dict(self) -> dict[str, object]:
        return {
            "apiVersion": PACK_SIGNATURE_PAYLOAD_API_VERSION,
            "contentSha256": self.content_sha256,
            "manifestSha256": self.manifest_sha256,
            "packIdentity": self.pack_identity,
            "publisher": self.publisher,
        }

    def to_json(self) -> str:
        return _canonical_json(self.as_dict())

    def to_bytes(self) -> bytes:
        return self.to_json().encode("utf-8")

    @property
    def sha256(self) -> str:
        return _HASH_PREFIX + sha256(self.to_bytes()).hexdigest()

    @classmethod
    def from_dict(cls, value: object) -> "PackSignaturePayload":
        raw = _mapping(value, label="signature payload")
        _keys(raw, expected=_PAYLOAD_KEYS, label="signature payload")
        if raw["apiVersion"] != PACK_SIGNATURE_PAYLOAD_API_VERSION:
            raise _fail("SDAI-PACK-INTEGRITY-001", "unsupported signature payload apiVersion")
        return cls(
            pack_identity=raw["packIdentity"],  # type: ignore[arg-type]
            publisher=raw["publisher"],  # type: ignore[arg-type]
            manifest_sha256=raw["manifestSha256"],  # type: ignore[arg-type]
            content_sha256=raw["contentSha256"],  # type: ignore[arg-type]
        )


def build_pack_signature_payload(
    manifest: PackManifest,
    content: PackContentIndex,
) -> PackSignaturePayload:
    return PackSignaturePayload(
        pack_identity=manifest.identity,
        publisher=manifest.publisher,
        manifest_sha256=manifest.sha256,
        content_sha256=content.sha256,
    )


@dataclass(frozen=True)
class PackSignatureEvidence:
    pack_identity: str
    publisher: str
    manifest_sha256: str
    content_sha256: str
    payload_sha256: str
    algorithm: str
    key_id: str
    signature: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "pack_identity", _text(self.pack_identity, label="packIdentity"))
        object.__setattr__(self, "publisher", _text(self.publisher, label="publisher"))
        object.__setattr__(self, "manifest_sha256", _hash(self.manifest_sha256, label="manifestSha256"))
        object.__setattr__(self, "content_sha256", _hash(self.content_sha256, label="contentSha256"))
        object.__setattr__(self, "payload_sha256", _hash(self.payload_sha256, label="payloadSha256"))
        object.__setattr__(self, "algorithm", _algorithm(self.algorithm))
        object.__setattr__(self, "key_id", _text(self.key_id, label="keyId"))
        signature_bytes = _signature_bytes(self.signature)
        object.__setattr__(self, "signature", base64.b64encode(signature_bytes).decode("ascii"))
        if self.payload().sha256 != self.payload_sha256:
            raise _fail("SDAI-PACK-INTEGRITY-001", "payloadSha256 does not match signed payload fields")

    @classmethod
    def create(
        cls,
        payload: PackSignaturePayload,
        *,
        algorithm: str,
        key_id: str,
        signature: bytes,
    ) -> "PackSignatureEvidence":
        if not isinstance(signature, bytes) or not signature:
            raise _fail("SDAI-PACK-INTEGRITY-001", "signature bytes must not be empty")
        return cls(
            pack_identity=payload.pack_identity,
            publisher=payload.publisher,
            manifest_sha256=payload.manifest_sha256,
            content_sha256=payload.content_sha256,
            payload_sha256=payload.sha256,
            algorithm=algorithm,
            key_id=key_id,
            signature=base64.b64encode(signature).decode("ascii"),
        )

    def payload(self) -> PackSignaturePayload:
        return PackSignaturePayload(
            pack_identity=self.pack_identity,
            publisher=self.publisher,
            manifest_sha256=self.manifest_sha256,
            content_sha256=self.content_sha256,
        )

    @property
    def signature_bytes(self) -> bytes:
        return _signature_bytes(self.signature)

    def as_dict(self) -> dict[str, object]:
        return {
            "algorithm": self.algorithm,
            "apiVersion": PACK_SIGNATURE_API_VERSION,
            "contentSha256": self.content_sha256,
            "keyId": self.key_id,
            "manifestSha256": self.manifest_sha256,
            "packIdentity": self.pack_identity,
            "payloadSha256": self.payload_sha256,
            "publisher": self.publisher,
            "signature": self.signature,
        }

    def to_json(self) -> str:
        return _canonical_json(self.as_dict())

    @property
    def sha256(self) -> str:
        return _HASH_PREFIX + sha256(self.to_json().encode("utf-8")).hexdigest()

    @classmethod
    def from_dict(cls, value: object) -> "PackSignatureEvidence":
        raw = _mapping(value, label="Pack signature evidence")
        _keys(raw, expected=_SIGNATURE_KEYS, label="Pack signature evidence")
        if raw["apiVersion"] != PACK_SIGNATURE_API_VERSION:
            raise _fail(
                "SDAI-PACK-INTEGRITY-001",
                f"unsupported apiVersion '{raw['apiVersion']}', expected '{PACK_SIGNATURE_API_VERSION}'",
            )
        return cls(
            pack_identity=raw["packIdentity"],  # type: ignore[arg-type]
            publisher=raw["publisher"],  # type: ignore[arg-type]
            manifest_sha256=raw["manifestSha256"],  # type: ignore[arg-type]
            content_sha256=raw["contentSha256"],  # type: ignore[arg-type]
            payload_sha256=raw["payloadSha256"],  # type: ignore[arg-type]
            algorithm=raw["algorithm"],  # type: ignore[arg-type]
            key_id=raw["keyId"],  # type: ignore[arg-type]
            signature=raw["signature"],  # type: ignore[arg-type]
        )

    @classmethod
    def from_json(cls, value: str) -> "PackSignatureEvidence":
        try:
            raw = json.loads(value, object_pairs_hook=_unique_json_object)
        except json.JSONDecodeError as exc:
            raise _fail("SDAI-PACK-INTEGRITY-001", "Pack signature JSON is malformed") from exc
        return cls.from_dict(raw)


@runtime_checkable
class SignatureVerifier(Protocol):
    def verify(
        self,
        *,
        key_id: str,
        payload: bytes,
        signature: bytes,
    ) -> bool:
        ...


class SignatureStatus(str, Enum):
    VALID = "valid"
    INVALID = "invalid"
    UNSUPPORTED = "unsupported"
    ERROR = "error"
    NOT_CHECKED = "not-checked"


class IntegrityStatus(str, Enum):
    CURRENT = "current"
    STALE = "stale"


@dataclass(frozen=True)
class PackSignatureVerification:
    pack_identity: str
    publisher: str
    manifest_sha256: str
    content_sha256: str
    evidence_sha256: str
    integrity_status: IntegrityStatus
    publisher_bound: bool
    signature_status: SignatureStatus
    verified: bool
    trust_status: str
    reasons: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "apiVersion": PACK_SIGNATURE_VERIFICATION_API_VERSION,
            "contentSha256": self.content_sha256,
            "evidenceSha256": self.evidence_sha256,
            "integrityStatus": self.integrity_status.value,
            "manifestSha256": self.manifest_sha256,
            "packIdentity": self.pack_identity,
            "publisher": self.publisher,
            "publisherBound": self.publisher_bound,
            "reasons": list(self.reasons),
            "signatureStatus": self.signature_status.value,
            "trustStatus": self.trust_status,
            "verified": self.verified,
        }

    def to_json(self) -> str:
        return _canonical_json(self.as_dict())


def verify_pack_signature(
    pack_root: Path,
    manifest: PackManifest,
    evidence: PackSignatureEvidence,
    verifiers: Mapping[str, SignatureVerifier],
) -> PackSignatureVerification:
    content = build_pack_content_index(pack_root, manifest)
    reasons: list[str] = []

    manifest_current = evidence.manifest_sha256 == manifest.sha256
    content_current = evidence.content_sha256 == content.sha256
    identity_current = evidence.pack_identity == manifest.identity
    publisher_bound = evidence.publisher == manifest.publisher
    integrity_current = manifest_current and content_current and identity_current

    if not identity_current:
        reasons.append("pack-identity-mismatch")
    if not publisher_bound:
        reasons.append("publisher-mismatch")
    if not manifest_current:
        reasons.append("manifest-stale")
    if not content_current:
        reasons.append("content-stale")

    signature_status = SignatureStatus.NOT_CHECKED
    verifier = verifiers.get(evidence.algorithm)
    if verifier is None:
        signature_status = SignatureStatus.UNSUPPORTED
        reasons.append("unsupported-algorithm")
    else:
        try:
            valid = verifier.verify(
                key_id=evidence.key_id,
                payload=evidence.payload().to_bytes(),
                signature=evidence.signature_bytes,
            )
        except Exception:
            signature_status = SignatureStatus.ERROR
            reasons.append("verifier-error")
        else:
            signature_status = SignatureStatus.VALID if valid else SignatureStatus.INVALID
            if not valid:
                reasons.append("invalid-signature")

    verified = (
        integrity_current
        and publisher_bound
        and signature_status is SignatureStatus.VALID
    )
    return PackSignatureVerification(
        pack_identity=manifest.identity,
        publisher=manifest.publisher,
        manifest_sha256=manifest.sha256,
        content_sha256=content.sha256,
        evidence_sha256=evidence.sha256,
        integrity_status=IntegrityStatus.CURRENT if integrity_current else IntegrityStatus.STALE,
        publisher_bound=publisher_bound,
        signature_status=signature_status,
        verified=verified,
        trust_status="not-evaluated",
        reasons=tuple(sorted(set(reasons))),
    )


def load_pack_signature_evidence(path: Path) -> PackSignatureEvidence:
    if path.is_symlink():
        raise _fail("SDAI-PACK-INTEGRITY-002", "Pack signature evidence path must not be a symlink")
    if not path.is_file():
        raise _fail("SDAI-PACK-INTEGRITY-001", f"Pack signature evidence '{path}' does not exist")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise _fail(
            "SDAI-PACK-INTEGRITY-001",
            f"unable to read Pack signature evidence '{path}' as UTF-8",
        ) from exc
    return PackSignatureEvidence.from_json(text)
