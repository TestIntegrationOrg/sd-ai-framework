from __future__ import annotations

from hashlib import sha256
import hmac
from pathlib import Path

import pytest

from sdai.pack_catalog import (
    PackCatalog,
    PackCatalogEntry,
    PackCatalogError,
    canonical_catalog_source,
    resolve_pack_catalogs,
)
from sdai.pack_integrity import (
    PackSignatureEvidence,
    build_pack_content_index,
    build_pack_signature_payload,
    verify_pack_signature,
)
from sdai.pack_manifest import PACK_MANIFEST_API_VERSION, PackManifest
from sdai.pack_trust_policy import (
    PackTrustPolicy,
    PackTrustPolicyError,
    evaluate_pack_trust,
    resolve_pack_trust_policy,
)


class HmacTestVerifier:
    def __init__(self, key: bytes) -> None:
        self.key = key

    def verify(self, *, key_id: str, payload: bytes, signature: bytes) -> bool:
        assert key_id == "publisher-key-1"
        expected = hmac.new(self.key, payload, sha256).digest()
        return hmac.compare_digest(expected, signature)


def _manifest() -> PackManifest:
    return PackManifest.from_dict(
        {
            "apiVersion": PACK_MANIFEST_API_VERSION,
            "id": "secure-coding",
            "publisher": "acme",
            "version": "1.0.0",
            "description": "Secure café rules Δ",
            "capabilities": ["skills"],
            "contentRoots": ["skills"],
            "dependencies": [],
            "compatibility": {"framework": ">=0.5.4,<1.0.0", "apis": []},
        }
    )


def _workspace(root: Path) -> PackManifest:
    (root / "skills").mkdir(parents=True)
    (root / "skills" / "review.md").write_text(
        "Review café Δ requirements.\n",
        encoding="utf-8",
        newline="\n",
    )
    return _manifest()


def _policy(catalog_source: str) -> PackTrustPolicy:
    return PackTrustPolicy.from_dict(
        {
            "apiVersion": "sdai.pack-trust-policy/v1",
            "requireSignatures": True,
            "allowedCatalogs": [catalog_source],
            "deniedCatalogs": [],
            "allowedPublishers": ["acme"],
            "deniedPublishers": [],
        }
    )


def test_real_signature_verification_flows_into_catalog_trust_decision(tmp_path: Path) -> None:
    manifest = _workspace(tmp_path)
    content = build_pack_content_index(tmp_path, manifest)
    payload = build_pack_signature_payload(manifest, content)
    key = b"test-publisher-key"
    evidence = PackSignatureEvidence.create(
        payload,
        algorithm="test-hmac-sha256",
        key_id="publisher-key-1",
        signature=hmac.new(key, payload.to_bytes(), sha256).digest(),
    )
    report = verify_pack_signature(
        tmp_path,
        manifest,
        evidence,
        {"test-hmac-sha256": HmacTestVerifier(key)},
    )

    catalog = PackCatalog.create(
        id="corp",
        source="catalog://corp",
        entries=(
            PackCatalogEntry(
                manifest=manifest,
                source="https://packages.example/acme/secure-coding/1.0.0",
                content_sha256=content.sha256,
            ),
        ),
    )
    resolved_catalog = resolve_pack_catalogs(organization=(catalog,)).catalogs[0]
    policy = resolve_pack_trust_policy(organization=_policy(catalog.source))

    decision = evaluate_pack_trust(
        policy,
        resolved_catalog,
        catalog.entries[0],
        signature_verification=report,
    )

    assert report.verified is True
    assert report.trust_status == "not-evaluated"
    assert decision.allowed is True
    assert decision.signature_verified is True
    assert decision.catalog_source == "catalog://corp"
    assert decision.publisher == "acme"
    assert decision.reasons == ()


def test_content_change_turns_previously_valid_signature_into_policy_block(tmp_path: Path) -> None:
    manifest = _workspace(tmp_path)
    content = build_pack_content_index(tmp_path, manifest)
    payload = build_pack_signature_payload(manifest, content)
    key = b"test-publisher-key"
    evidence = PackSignatureEvidence.create(
        payload,
        algorithm="test-hmac-sha256",
        key_id="publisher-key-1",
        signature=hmac.new(key, payload.to_bytes(), sha256).digest(),
    )
    catalog = PackCatalog.create(
        id="corp",
        source="catalog://corp",
        entries=(
            PackCatalogEntry(
                manifest=manifest,
                source="https://packages.example/acme/secure-coding/1.0.0",
                content_sha256=content.sha256,
            ),
        ),
    )
    resolved_catalog = resolve_pack_catalogs(organization=(catalog,)).catalogs[0]
    policy = resolve_pack_trust_policy(organization=_policy(catalog.source))

    (tmp_path / "skills" / "review.md").write_text(
        "Changed after signing.\n",
        encoding="utf-8",
        newline="\n",
    )
    stale_report = verify_pack_signature(
        tmp_path,
        manifest,
        evidence,
        {"test-hmac-sha256": HmacTestVerifier(key)},
    )
    decision = evaluate_pack_trust(
        policy,
        resolved_catalog,
        catalog.entries[0],
        signature_verification=stale_report,
    )

    assert stale_report.verified is False
    assert "content-stale" in stale_report.reasons
    assert decision.allowed is False
    assert decision.reasons == ("signature-not-current-valid",)


@pytest.mark.parametrize(
    "source",
    [
        "HTTPS://catalog.example/index.json",
        "https://Catalog.Example/index.json",
    ],
)
def test_catalog_source_identity_rejects_case_aliases(source: str) -> None:
    with pytest.raises(PackCatalogError, match="lowercase"):
        canonical_catalog_source(source)


def test_policy_normalizes_invalid_catalog_source_to_policy_error() -> None:
    raw = _policy("catalog://corp").as_dict()
    raw["allowedCatalogs"] = ["https://user:secret@example.com/catalog"]

    with pytest.raises(PackTrustPolicyError, match="invalid catalog source policy value"):
        PackTrustPolicy.from_dict(raw)
