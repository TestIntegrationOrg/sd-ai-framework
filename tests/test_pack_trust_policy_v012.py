from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
from pathlib import Path

import pytest

from sdai.pack_catalog import PackCatalog, PackCatalogEntry, resolve_pack_catalogs
from sdai.pack_integrity import (
    IntegrityStatus,
    PackSignatureVerification,
    SignatureStatus,
)
from sdai.pack_manifest import PACK_MANIFEST_API_VERSION, PackManifest
from sdai.pack_trust_policy import (
    PackTrustPolicy,
    PackTrustPolicyError,
    PolicyScope,
    evaluate_pack_trust,
    load_pack_trust_policy,
    resolve_pack_trust_policy,
)


def _digest(seed: str) -> str:
    return "sha256:" + sha256(seed.encode("utf-8")).hexdigest()


def _entry(publisher: str = "acme", pack_id: str = "secure-coding") -> PackCatalogEntry:
    manifest = PackManifest.from_dict(
        {
            "apiVersion": PACK_MANIFEST_API_VERSION,
            "id": pack_id,
            "publisher": publisher,
            "version": "1.0.0",
            "description": f"{publisher}/{pack_id} trusted Pack",
            "capabilities": ["skills"],
            "contentRoots": ["skills"],
            "dependencies": [],
            "compatibility": {"framework": ">=0.5.4,<1.0.0", "apis": []},
        }
    )
    return PackCatalogEntry(
        manifest=manifest,
        source=f"https://packages.example/{publisher}/{pack_id}/1.0.0",
        content_sha256=_digest(manifest.identity),
    )


def _catalog(
    *,
    catalog_id: str = "corp",
    source: str = "catalog://corp",
    entry: PackCatalogEntry | None = None,
) -> PackCatalog:
    return PackCatalog.create(id=catalog_id, source=source, entries=[entry or _entry()])


def _policy(
    *,
    require_signatures: bool = False,
    allowed_catalogs: list[str] | None = None,
    denied_catalogs: list[str] | None = None,
    allowed_publishers: list[str] | None = None,
    denied_publishers: list[str] | None = None,
) -> PackTrustPolicy:
    return PackTrustPolicy.from_dict(
        {
            "apiVersion": "sdai.pack-trust-policy/v1",
            "requireSignatures": require_signatures,
            "allowedCatalogs": ["*"] if allowed_catalogs is None else allowed_catalogs,
            "deniedCatalogs": denied_catalogs or [],
            "allowedPublishers": ["*"] if allowed_publishers is None else allowed_publishers,
            "deniedPublishers": denied_publishers or [],
        }
    )


def _verification(entry: PackCatalogEntry, *, verified: bool = True) -> PackSignatureVerification:
    return PackSignatureVerification(
        pack_identity=entry.identity,
        publisher=entry.manifest.publisher,
        manifest_sha256=entry.manifest.sha256,
        content_sha256=entry.content_sha256,
        evidence_sha256=_digest("evidence:" + entry.identity),
        integrity_status=IntegrityStatus.CURRENT if verified else IntegrityStatus.STALE,
        publisher_bound=True,
        signature_status=SignatureStatus.VALID if verified else SignatureStatus.INVALID,
        verified=verified,
        trust_status="not-evaluated",
        reasons=() if verified else ("invalid-signature",),
    )


def test_policy_resolution_is_monotonic_and_preserves_scope_provenance() -> None:
    organization = _policy(
        require_signatures=True,
        allowed_catalogs=["catalog://corp", "catalog://partner"],
        allowed_publishers=["acme", "sdai"],
        denied_publishers=["blocked"],
    )
    repository = _policy(
        require_signatures=False,
        allowed_catalogs=["*"],
        allowed_publishers=["*"],
        denied_catalogs=["catalog://partner"],
    )
    user = _policy(
        allowed_catalogs=["catalog://corp"],
        allowed_publishers=["acme"],
    )

    resolved = resolve_pack_trust_policy(
        organization=organization,
        repository=repository,
        user=user,
    )

    assert resolved.require_signatures is True
    assert resolved.allowed_catalogs == ("catalog://corp",)
    assert resolved.denied_catalogs == ("catalog://partner",)
    assert resolved.allowed_publishers == ("acme",)
    assert resolved.denied_publishers == ("blocked",)
    assert [item.scope for item in resolved.provenance] == [
        PolicyScope.ORGANIZATION,
        PolicyScope.REPOSITORY,
        PolicyScope.USER,
    ]
    assert [item.policy_sha256 for item in resolved.provenance] == [
        organization.sha256,
        repository.sha256,
        user.sha256,
    ]


def test_lower_scope_cannot_reallow_organization_denial_or_disable_signature_requirement() -> None:
    organization = _policy(
        require_signatures=True,
        allowed_catalogs=["catalog://corp"],
        allowed_publishers=["acme"],
        denied_publishers=["blocked"],
    )
    permissive_user = _policy(
        require_signatures=False,
        allowed_catalogs=["*"],
        allowed_publishers=["*"],
    )

    resolved = resolve_pack_trust_policy(organization=organization, user=permissive_user)

    assert resolved.require_signatures is True
    assert resolved.allowed_catalogs == ("catalog://corp",)
    assert resolved.allowed_publishers == ("acme",)
    assert resolved.denied_publishers == ("blocked",)


def test_lower_scope_can_further_restrict_to_empty_allowlist() -> None:
    organization = _policy(
        allowed_catalogs=["catalog://corp"],
        allowed_publishers=["acme", "sdai"],
    )
    repository = _policy(
        allowed_catalogs=[],
        allowed_publishers=["acme"],
    )

    resolved = resolve_pack_trust_policy(organization=organization, repository=repository)

    assert resolved.allowed_catalogs == ()
    assert resolved.allowed_publishers == ("acme",)


def test_trust_decision_allows_current_valid_signature_from_allowed_catalog_and_publisher() -> None:
    catalog = _catalog()
    resolved_catalog = resolve_pack_catalogs(organization=[catalog]).catalogs[0]
    entry = catalog.entries[0]
    policy = resolve_pack_trust_policy(
        organization=_policy(
            require_signatures=True,
            allowed_catalogs=["catalog://corp"],
            allowed_publishers=["acme"],
        )
    )

    decision = evaluate_pack_trust(
        policy,
        resolved_catalog,
        entry,
        signature_verification=_verification(entry),
    )

    assert decision.allowed is True
    assert decision.signature_required is True
    assert decision.signature_verified is True
    assert decision.reasons == ()
    assert decision.catalog_provenance == ("organization",)
    assert decision.catalog_sha256 == catalog.sha256
    assert decision.policy_sha256 == policy.sha256


def test_required_signature_missing_or_noncurrent_fails_closed() -> None:
    catalog = _catalog()
    resolved_catalog = resolve_pack_catalogs(organization=[catalog]).catalogs[0]
    entry = catalog.entries[0]
    policy = resolve_pack_trust_policy(organization=_policy(require_signatures=True))

    missing = evaluate_pack_trust(policy, resolved_catalog, entry)
    invalid = evaluate_pack_trust(
        policy,
        resolved_catalog,
        entry,
        signature_verification=_verification(entry, verified=False),
    )

    assert missing.allowed is False
    assert missing.reasons == ("signature-required",)
    assert invalid.allowed is False
    assert invalid.reasons == ("signature-not-current-valid",)


def test_supplied_invalid_signature_is_not_silently_ignored_when_signatures_are_optional() -> None:
    catalog = _catalog()
    resolved_catalog = resolve_pack_catalogs(repository=[catalog]).catalogs[0]
    entry = catalog.entries[0]
    policy = resolve_pack_trust_policy(repository=_policy(require_signatures=False))

    decision = evaluate_pack_trust(
        policy,
        resolved_catalog,
        entry,
        signature_verification=_verification(entry, verified=False),
    )

    assert decision.allowed is False
    assert decision.reasons == ("signature-not-current-valid",)


def test_catalog_and_publisher_allow_deny_rules_are_enforced_by_exact_source_identity() -> None:
    catalog = _catalog()
    resolved_catalog = resolve_pack_catalogs(repository=[catalog]).catalogs[0]
    entry = catalog.entries[0]

    catalog_blocked = evaluate_pack_trust(
        resolve_pack_trust_policy(organization=_policy(allowed_catalogs=["catalog://other"])),
        resolved_catalog,
        entry,
    )
    publisher_blocked = evaluate_pack_trust(
        resolve_pack_trust_policy(organization=_policy(denied_publishers=["acme"])),
        resolved_catalog,
        entry,
    )

    assert catalog_blocked.allowed is False
    assert catalog_blocked.reasons == ("catalog-not-allowed",)
    assert publisher_blocked.allowed is False
    assert publisher_blocked.reasons == ("publisher-denied",)


def test_signature_report_must_match_exact_catalog_entry_truth() -> None:
    catalog = _catalog()
    resolved_catalog = resolve_pack_catalogs(organization=[catalog]).catalogs[0]
    entry = catalog.entries[0]
    other = _entry(pack_id="other")
    policy = resolve_pack_trust_policy(organization=_policy(require_signatures=True))

    decision = evaluate_pack_trust(
        policy,
        resolved_catalog,
        entry,
        signature_verification=_verification(other),
    )

    assert decision.allowed is False
    assert decision.reasons == ("signature-report-entry-mismatch",)


def test_policy_rejects_entry_not_owned_by_resolved_catalog() -> None:
    catalog = _catalog()
    resolved_catalog = resolve_pack_catalogs(organization=[catalog]).catalogs[0]
    outsider = _entry(pack_id="outsider")
    policy = resolve_pack_trust_policy(organization=_policy())

    with pytest.raises(PackTrustPolicyError, match="not present in catalog"):
        evaluate_pack_trust(policy, resolved_catalog, outsider)


def test_policy_round_trip_is_canonical_and_strict() -> None:
    first = _policy(
        require_signatures=True,
        allowed_catalogs=["catalog://partner", "catalog://corp"],
        denied_catalogs=["catalog://blocked"],
        allowed_publishers=["sdai", "acme"],
        denied_publishers=["evil"],
    )
    raw = deepcopy(first.as_dict())
    raw["allowedCatalogs"] = list(reversed(raw["allowedCatalogs"]))  # type: ignore[index]
    raw["allowedPublishers"] = list(reversed(raw["allowedPublishers"]))  # type: ignore[index]
    second = PackTrustPolicy.from_dict(raw)

    assert first.to_json() == second.to_json()
    assert first.sha256 == second.sha256

    duplicate = first.to_json().replace(
        '"requireSignatures":true',
        '"requireSignatures":true,"requireSignatures":true',
    )
    with pytest.raises(PackTrustPolicyError, match="duplicate key 'requireSignatures'"):
        PackTrustPolicy.from_json(duplicate)

    malformed = deepcopy(first.as_dict())
    malformed["extra"] = True
    with pytest.raises(PackTrustPolicyError, match="unsupported field"):
        PackTrustPolicy.from_dict(malformed)


@pytest.mark.parametrize(
    "field,value",
    [
        ("allowedCatalogs", ["*", "catalog://corp"]),
        ("deniedCatalogs", ["*"]),
        ("allowedPublishers", ["*", "acme"]),
        ("deniedPublishers", ["*"]),
        ("allowedPublishers", ["ACME"]),
        ("allowedCatalogs", ["https://user:secret@example.com/catalog"]),
    ],
)
def test_policy_rejects_ambiguous_or_unsafe_trust_rules(field: str, value: list[str]) -> None:
    raw = _policy().as_dict()
    raw[field] = value

    with pytest.raises((PackTrustPolicyError, RuntimeError)):
        PackTrustPolicy.from_dict(raw)


def test_policy_loader_is_read_only_and_rejects_missing_and_symlink_paths(tmp_path: Path) -> None:
    policy = _policy(require_signatures=True, allowed_publishers=["acme"])
    path = tmp_path / "policy.json"
    path.write_text(policy.to_json(), encoding="utf-8", newline="\n")
    before = path.read_bytes()

    assert load_pack_trust_policy(path).sha256 == policy.sha256
    assert path.read_bytes() == before

    with pytest.raises(PackTrustPolicyError, match="does not exist"):
        load_pack_trust_policy(tmp_path / "missing.json")

    link = tmp_path / "linked-policy.json"
    try:
        link.symlink_to(path.name)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable on this platform: {exc}")
    with pytest.raises(PackTrustPolicyError, match="must not be a symlink"):
        load_pack_trust_policy(link)
