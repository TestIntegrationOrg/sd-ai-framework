from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from hashlib import sha256
import json
from pathlib import Path
from typing import Iterable, Mapping

from sdai.pack_catalog import (
    PackCatalogEntry,
    ResolvedCatalog,
    canonical_catalog_source,
)
from sdai.pack_integrity import PackSignatureVerification


PACK_TRUST_POLICY_API_VERSION = "sdai.pack-trust-policy/v1"
PACK_TRUST_POLICY_RESOLVED_API_VERSION = "sdai.pack-trust-policy-resolved/v1"
PACK_TRUST_DECISION_API_VERSION = "sdai.pack-trust-decision/v1"


class PackTrustPolicyError(RuntimeError):
    pass


_POLICY_KEYS = frozenset(
    {
        "apiVersion",
        "requireSignatures",
        "allowedCatalogs",
        "deniedCatalogs",
        "allowedPublishers",
        "deniedPublishers",
    }
)
_IDENTIFIER_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-_.")
_HASH_PREFIX = "sha256:"


def _fail(code: str, message: str) -> PackTrustPolicyError:
    return PackTrustPolicyError(f"{code}: {message}")


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
        raise _fail("SDAI-PACK-POLICY-001", "policy is not canonical finite JSON") from exc


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _fail("SDAI-PACK-POLICY-001", f"policy JSON contains duplicate key '{key}'")
        result[key] = value
    return result


def _mapping(value: object, *, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise _fail("SDAI-PACK-POLICY-001", f"{label} must be a string-keyed mapping")
    return value


def _keys(value: Mapping[str, object], *, expected: frozenset[str], label: str) -> None:
    unknown = sorted(set(value) - expected)
    missing = sorted(expected - set(value))
    if unknown:
        raise _fail(
            "SDAI-PACK-POLICY-001",
            f"{label} contains unsupported field(s): {', '.join(unknown)}",
        )
    if missing:
        raise _fail(
            "SDAI-PACK-POLICY-001",
            f"{label} is missing required field(s): {', '.join(missing)}",
        )


def _publisher(value: object, *, label: str = "publisher") -> str:
    if not isinstance(value, str) or not value:
        raise _fail("SDAI-PACK-POLICY-001", f"{label} must be a non-empty string")
    if value[0] not in "abcdefghijklmnopqrstuvwxyz":
        raise _fail("SDAI-PACK-POLICY-001", f"{label} '{value}' is not a portable lowercase identifier")
    if any(char not in _IDENTIFIER_CHARS for char in value):
        raise _fail("SDAI-PACK-POLICY-001", f"{label} '{value}' is not a portable lowercase identifier")
    if value[-1] in "-_." or any(pair in value for pair in ("--", "__", "..", "-.", ".-", "-_", "_-", "._", "_.")):
        raise _fail("SDAI-PACK-POLICY-001", f"{label} '{value}' is not a portable lowercase identifier")
    return value


def _allowlist(
    value: object,
    *,
    label: str,
    validator,
) -> tuple[str, ...] | None:
    if not isinstance(value, list):
        raise _fail("SDAI-PACK-POLICY-001", f"{label} must be a list")
    if value == ["*"]:
        return None
    if "*" in value:
        raise _fail("SDAI-PACK-POLICY-001", f"{label} may use '*' only as its sole entry")
    parsed = tuple(sorted(validator(item) for item in value))
    if len(set(parsed)) != len(parsed):
        raise _fail("SDAI-PACK-POLICY-001", f"{label} must not contain duplicates")
    return parsed


def _denylist(value: object, *, label: str, validator) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise _fail("SDAI-PACK-POLICY-001", f"{label} must be a list")
    if "*" in value:
        raise _fail("SDAI-PACK-POLICY-001", f"{label} must use explicit identities; use an empty allowlist to deny all")
    parsed = tuple(sorted(validator(item) for item in value))
    if len(set(parsed)) != len(parsed):
        raise _fail("SDAI-PACK-POLICY-001", f"{label} must not contain duplicates")
    return parsed


def _intersect(left: tuple[str, ...] | None, right: tuple[str, ...] | None) -> tuple[str, ...] | None:
    if left is None:
        return right
    if right is None:
        return left
    return tuple(sorted(set(left) & set(right)))


def _allowed(value: str, allowlist: tuple[str, ...] | None, denylist: tuple[str, ...]) -> bool:
    if value in denylist:
        return False
    return allowlist is None or value in allowlist


@dataclass(frozen=True)
class PackTrustPolicy:
    require_signatures: bool
    allowed_catalogs: tuple[str, ...] | None
    denied_catalogs: tuple[str, ...]
    allowed_publishers: tuple[str, ...] | None
    denied_publishers: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.require_signatures, bool):
            raise _fail("SDAI-PACK-POLICY-001", "requireSignatures must be a boolean")
        if self.allowed_catalogs is not None and tuple(sorted(self.allowed_catalogs)) != self.allowed_catalogs:
            raise _fail("SDAI-PACK-POLICY-001", "allowedCatalogs must be canonical")
        if tuple(sorted(self.denied_catalogs)) != self.denied_catalogs:
            raise _fail("SDAI-PACK-POLICY-001", "deniedCatalogs must be canonical")
        if self.allowed_publishers is not None and tuple(sorted(self.allowed_publishers)) != self.allowed_publishers:
            raise _fail("SDAI-PACK-POLICY-001", "allowedPublishers must be canonical")
        if tuple(sorted(self.denied_publishers)) != self.denied_publishers:
            raise _fail("SDAI-PACK-POLICY-001", "deniedPublishers must be canonical")

    def as_dict(self) -> dict[str, object]:
        return {
            "allowedCatalogs": ["*"] if self.allowed_catalogs is None else list(self.allowed_catalogs),
            "allowedPublishers": ["*"] if self.allowed_publishers is None else list(self.allowed_publishers),
            "apiVersion": PACK_TRUST_POLICY_API_VERSION,
            "deniedCatalogs": list(self.denied_catalogs),
            "deniedPublishers": list(self.denied_publishers),
            "requireSignatures": self.require_signatures,
        }

    def to_json(self) -> str:
        return _canonical_json(self.as_dict())

    @property
    def sha256(self) -> str:
        return _HASH_PREFIX + sha256(self.to_json().encode("utf-8")).hexdigest()

    @classmethod
    def from_dict(cls, value: object) -> "PackTrustPolicy":
        raw = _mapping(value, label="Pack trust policy")
        _keys(raw, expected=_POLICY_KEYS, label="Pack trust policy")
        require_signatures = raw["requireSignatures"]
        if not isinstance(require_signatures, bool):
            raise _fail("SDAI-PACK-POLICY-001", "requireSignatures must be a boolean")
        return cls(
            require_signatures=require_signatures,
            allowed_catalogs=_allowlist(
                raw["allowedCatalogs"],
                label="allowedCatalogs",
                validator=canonical_catalog_source,
            ),
            denied_catalogs=_denylist(
                raw["deniedCatalogs"],
                label="deniedCatalogs",
                validator=canonical_catalog_source,
            ),
            allowed_publishers=_allowlist(
                raw["allowedPublishers"],
                label="allowedPublishers",
                validator=lambda item: _publisher(item),
            ),
            denied_publishers=_denylist(
                raw["deniedPublishers"],
                label="deniedPublishers",
                validator=lambda item: _publisher(item),
            ),
        )

    @classmethod
    def from_json(cls, value: str) -> "PackTrustPolicy":
        try:
            raw = json.loads(value, object_pairs_hook=_unique_json_object)
        except json.JSONDecodeError as exc:
            raise _fail("SDAI-PACK-POLICY-001", "Pack trust policy JSON is malformed") from exc
        return cls.from_dict(raw)


class PolicyScope(str, Enum):
    ORGANIZATION = "organization"
    REPOSITORY = "repository"
    USER = "user"


@dataclass(frozen=True)
class PolicyProvenance:
    scope: PolicyScope
    policy_sha256: str

    def as_dict(self) -> dict[str, object]:
        return {"policySha256": self.policy_sha256, "scope": self.scope.value}


@dataclass(frozen=True)
class ResolvedPackTrustPolicy:
    require_signatures: bool
    allowed_catalogs: tuple[str, ...] | None
    denied_catalogs: tuple[str, ...]
    allowed_publishers: tuple[str, ...] | None
    denied_publishers: tuple[str, ...]
    provenance: tuple[PolicyProvenance, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "allowedCatalogs": ["*"] if self.allowed_catalogs is None else list(self.allowed_catalogs),
            "allowedPublishers": ["*"] if self.allowed_publishers is None else list(self.allowed_publishers),
            "apiVersion": PACK_TRUST_POLICY_RESOLVED_API_VERSION,
            "deniedCatalogs": list(self.denied_catalogs),
            "deniedPublishers": list(self.denied_publishers),
            "provenance": [item.as_dict() for item in self.provenance],
            "requireSignatures": self.require_signatures,
        }

    def to_json(self) -> str:
        return _canonical_json(self.as_dict())

    @property
    def sha256(self) -> str:
        return _HASH_PREFIX + sha256(self.to_json().encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PackTrustDecision:
    pack_identity: str
    publisher: str
    catalog_id: str
    catalog_source: str
    catalog_sha256: str
    catalog_provenance: tuple[str, ...]
    policy_sha256: str
    signature_required: bool
    signature_verified: bool
    allowed: bool
    reasons: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "allowed": self.allowed,
            "apiVersion": PACK_TRUST_DECISION_API_VERSION,
            "catalogId": self.catalog_id,
            "catalogProvenance": list(self.catalog_provenance),
            "catalogSha256": self.catalog_sha256,
            "catalogSource": self.catalog_source,
            "packIdentity": self.pack_identity,
            "policySha256": self.policy_sha256,
            "publisher": self.publisher,
            "reasons": list(self.reasons),
            "signatureRequired": self.signature_required,
            "signatureVerified": self.signature_verified,
        }

    def to_json(self) -> str:
        return _canonical_json(self.as_dict())


def resolve_pack_trust_policy(
    *,
    organization: PackTrustPolicy | None = None,
    repository: PackTrustPolicy | None = None,
    user: PackTrustPolicy | None = None,
) -> ResolvedPackTrustPolicy:
    require_signatures = False
    allowed_catalogs: tuple[str, ...] | None = None
    denied_catalogs: set[str] = set()
    allowed_publishers: tuple[str, ...] | None = None
    denied_publishers: set[str] = set()
    provenance: list[PolicyProvenance] = []

    for scope, policy in (
        (PolicyScope.ORGANIZATION, organization),
        (PolicyScope.REPOSITORY, repository),
        (PolicyScope.USER, user),
    ):
        if policy is None:
            continue
        require_signatures = require_signatures or policy.require_signatures
        allowed_catalogs = _intersect(allowed_catalogs, policy.allowed_catalogs)
        denied_catalogs.update(policy.denied_catalogs)
        allowed_publishers = _intersect(allowed_publishers, policy.allowed_publishers)
        denied_publishers.update(policy.denied_publishers)
        provenance.append(PolicyProvenance(scope, policy.sha256))

    return ResolvedPackTrustPolicy(
        require_signatures=require_signatures,
        allowed_catalogs=allowed_catalogs,
        denied_catalogs=tuple(sorted(denied_catalogs)),
        allowed_publishers=allowed_publishers,
        denied_publishers=tuple(sorted(denied_publishers)),
        provenance=tuple(provenance),
    )


def evaluate_pack_trust(
    policy: ResolvedPackTrustPolicy,
    resolved_catalog: ResolvedCatalog,
    entry: PackCatalogEntry,
    *,
    signature_verification: PackSignatureVerification | None = None,
) -> PackTrustDecision:
    if not any(candidate.as_dict() == entry.as_dict() for candidate in resolved_catalog.catalog.entries):
        raise _fail(
            "SDAI-PACK-POLICY-002",
            f"Pack entry '{entry.identity}' is not present in catalog '{resolved_catalog.catalog.id}'",
        )

    reasons: list[str] = []
    catalog_source = resolved_catalog.catalog.source
    publisher = entry.manifest.publisher

    if catalog_source in policy.denied_catalogs:
        reasons.append("catalog-denied")
    elif policy.allowed_catalogs is not None and catalog_source not in policy.allowed_catalogs:
        reasons.append("catalog-not-allowed")

    if publisher in policy.denied_publishers:
        reasons.append("publisher-denied")
    elif policy.allowed_publishers is not None and publisher not in policy.allowed_publishers:
        reasons.append("publisher-not-allowed")

    signature_verified = False
    if signature_verification is None:
        if policy.require_signatures:
            reasons.append("signature-required")
    else:
        signature_matches_entry = (
            signature_verification.pack_identity == entry.identity
            and signature_verification.publisher == publisher
            and signature_verification.manifest_sha256 == entry.manifest.sha256
            and signature_verification.content_sha256 == entry.content_sha256
        )
        if not signature_matches_entry:
            reasons.append("signature-report-entry-mismatch")
        elif not signature_verification.verified:
            reasons.append("signature-not-current-valid")
        else:
            signature_verified = True

    allowed = not reasons
    return PackTrustDecision(
        pack_identity=entry.identity,
        publisher=publisher,
        catalog_id=resolved_catalog.catalog.id,
        catalog_source=catalog_source,
        catalog_sha256=resolved_catalog.catalog.sha256,
        catalog_provenance=tuple(scope.value for scope in resolved_catalog.provenance),
        policy_sha256=policy.sha256,
        signature_required=policy.require_signatures,
        signature_verified=signature_verified,
        allowed=allowed,
        reasons=tuple(sorted(set(reasons))),
    )


def load_pack_trust_policy(path: Path) -> PackTrustPolicy:
    if path.is_symlink():
        raise _fail("SDAI-PACK-POLICY-003", "Pack trust policy path must not be a symlink")
    if not path.is_file():
        raise _fail("SDAI-PACK-POLICY-003", f"Pack trust policy '{path}' does not exist or is not a file")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise _fail("SDAI-PACK-POLICY-003", f"unable to read Pack trust policy '{path}' as UTF-8") from exc
    return PackTrustPolicy.from_json(text)
