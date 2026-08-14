from __future__ import annotations

from hashlib import sha256
import hmac
from pathlib import Path

import pytest
import yaml

from sdai.pack_catalog import PackCatalog, PackCatalogEntry, resolve_pack_catalogs
from sdai.pack_certification import (
    CertificationRequirement,
    PackCertificationPolicy,
    PackEvalCaseResult,
    PackEvalEvidence,
    ProducerMetadata,
    resolve_pack_certification_policy,
)
from sdai.pack_eval_suite import PackEvalCaseSpec, PackEvalSuite, evaluate_pack_certification_suite
from sdai.pack_integrity import (
    PackSignatureEvidence,
    build_pack_content_index,
    build_pack_signature_payload,
    verify_pack_signature,
)
from sdai.pack_lifecycle import install_from_local, load_install_state, remove_pack
from sdai.pack_lock import PackCandidate, PackLockError, resolve_pack_lock
from sdai.pack_manifest import PACK_MANIFEST_API_VERSION, PackManifest, load_pack_manifest
from sdai.pack_trust_policy import PackTrustPolicy, evaluate_pack_trust, resolve_pack_trust_policy


class HmacTestVerifier:
    def __init__(self, key: bytes) -> None:
        self.key = key

    def verify(self, *, key_id: str, payload: bytes, signature: bytes) -> bool:
        if key_id != "publisher-key-1":
            return False
        expected = hmac.new(self.key, payload, sha256).digest()
        return hmac.compare_digest(expected, signature)


def _write_pack(root: Path, *, version: str = "1.2.3") -> PackManifest:
    (root / "skills" / "café").mkdir(parents=True)
    (root / "skills" / "café" / "review.md").write_text(
        "Review café Δ requirements.\n", encoding="utf-8", newline="\n"
    )
    raw = {
        "apiVersion": PACK_MANIFEST_API_VERSION,
        "id": "secure-coding",
        "publisher": "acme",
        "version": version,
        "description": "Secure café coding Δ Pack",
        "capabilities": ["skills"],
        "contentRoots": ["skills"],
        "dependencies": [],
        "compatibility": {"framework": ">=0.5.4,<1.0.0", "apis": ["sdai.pack-manifest/v1"]},
    }
    (root / "pack.yaml").write_text(
        yaml.safe_dump(raw, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
        newline="\n",
    )
    return load_pack_manifest(root / "pack.yaml")


def _candidate(root: Path) -> PackCandidate:
    manifest = load_pack_manifest(root / "pack.yaml")
    content = build_pack_content_index(root, manifest)
    return PackCandidate(
        manifest=manifest,
        source=f"https://packs.example.test/{manifest.publisher}/{manifest.id}/{manifest.version}",
        content_sha256=content.sha256,
    )


def _trust_policy() -> PackTrustPolicy:
    return PackTrustPolicy.from_dict(
        {
            "apiVersion": "sdai.pack-trust-policy/v1",
            "requireSignatures": True,
            "allowedCatalogs": ["catalog://corp"],
            "deniedCatalogs": [],
            "allowedPublishers": ["acme"],
            "deniedPublishers": [],
        }
    )


def _cert_policy() -> PackCertificationPolicy:
    return PackCertificationPolicy(
        require_certification=True,
        default=CertificationRequirement(
            minimum_score_basis_points=9000,
            required_dimensions=(("quality", 9000),),
            required_cases=("quality-case",),
        ),
    )


def test_exact_signed_trusted_certified_pack_installs_and_removes_without_semantic_drift(tmp_path: Path) -> None:
    project = tmp_path / "project"
    artifact = tmp_path / "éditeur-café-pack"
    (project / ".sdai").mkdir(parents=True)
    (project / ".sdai" / "config.yaml").write_text("provider: mock\n", encoding="utf-8")
    manifest = _write_pack(artifact)
    candidate = _candidate(artifact)

    # 1. Resolver/lock authority chooses the exact current Pack candidate.
    lock = resolve_pack_lock([candidate], [])
    assert lock.roots == (manifest.identity,)
    assert lock.packages[0].identity == manifest.identity

    # 2. Content and publisher signature are bound to exact manifest/content hashes.
    content = build_pack_content_index(artifact, manifest)
    payload = build_pack_signature_payload(manifest, content)
    key = b"publisher-test-key"
    evidence = PackSignatureEvidence.create(
        payload,
        algorithm="test-hmac-sha256",
        key_id="publisher-key-1",
        signature=hmac.new(key, payload.to_bytes(), sha256).digest(),
    )
    verification = verify_pack_signature(
        artifact,
        manifest,
        evidence,
        {"test-hmac-sha256": HmacTestVerifier(key)},
    )
    assert verification.verified is True

    # 3. Trusted organization catalog + non-weakening trust policy allows this exact artifact.
    catalog = PackCatalog.create(
        id="corp",
        source="catalog://corp",
        entries=(PackCatalogEntry(manifest=manifest, source=candidate.source, content_sha256=content.sha256),),
    )
    resolved_catalog = resolve_pack_catalogs(organization=(catalog,)).catalogs[0]
    trust = evaluate_pack_trust(
        resolve_pack_trust_policy(organization=_trust_policy()),
        resolved_catalog,
        catalog.entries[0],
        signature_verification=verification,
    )
    assert trust.allowed is True
    assert trust.signature_verified is True

    # 4. Current eval suite and policy certify provider-independent facts for the exact Pack.
    suite = PackEvalSuite(
        (PackEvalCaseSpec("quality-case", "quality", "sha256:" + "a" * 64),)
    )
    cert_policy = resolve_pack_certification_policy(organization=_cert_policy())
    eval_evidence = PackEvalEvidence(
        pack_identity=manifest.identity,
        manifest_sha256=manifest.sha256,
        content_sha256=content.sha256,
        policy_sha256=cert_policy.sha256,
        suite_sha256=suite.sha256,
        cases=(
            PackEvalCaseResult(
                "quality-case", "quality", "sha256:" + "a" * 64, 9500, True
            ),
        ),
        producer=ProducerMetadata("provider-a", "model-a", "compat-gate"),
    )
    certification = evaluate_pack_certification_suite(
        manifest, content.sha256, cert_policy, suite, eval_evidence
    )
    assert certification.status == "certified"

    # 5. Lifecycle materializes only the exact locked bytes and remains idempotent.
    installed = install_from_local(project, artifact, lock, manifest.coordinate)
    repeated = install_from_local(project, artifact, lock, manifest.coordinate)
    assert repeated == installed
    assert load_install_state(project).packs == (installed,)
    assert installed.identity == manifest.identity
    assert installed.content_sha256 == content.sha256
    managed_review = next(item for item in installed.files if item.source_path.endswith("review.md"))
    assert (project / managed_review.path).read_text(encoding="utf-8") == "Review café Δ requirements.\n"

    # 6. Removal preserves user edits instead of deleting framework-owned history blindly.
    modified = project / managed_review.path
    modified.write_text("User-owned Δ edit after install.\n", encoding="utf-8", newline="\n")
    preserved = remove_pack(project, manifest.coordinate)
    assert managed_review.path in preserved
    assert modified.read_text(encoding="utf-8") == "User-owned Δ edit after install.\n"
    assert load_install_state(project).packs == ()


def test_signed_pack_gate_rejects_tamper_untrusted_publisher_stale_eval_and_resolution_failures(tmp_path: Path) -> None:
    artifact = tmp_path / "pack"
    manifest = _write_pack(artifact)
    candidate = _candidate(artifact)
    content = build_pack_content_index(artifact, manifest)
    payload = build_pack_signature_payload(manifest, content)
    key = b"publisher-test-key"
    evidence = PackSignatureEvidence.create(
        payload,
        algorithm="test-hmac-sha256",
        key_id="publisher-key-1",
        signature=hmac.new(key, payload.to_bytes(), sha256).digest(),
    )

    # Tampering after signing makes signature evidence stale and trust fails closed.
    (artifact / "skills" / "café" / "review.md").write_text(
        "tampered after signing\n", encoding="utf-8", newline="\n"
    )
    stale_signature = verify_pack_signature(
        artifact,
        manifest,
        evidence,
        {"test-hmac-sha256": HmacTestVerifier(key)},
    )
    catalog = PackCatalog.create(
        id="corp",
        source="catalog://corp",
        entries=(PackCatalogEntry(manifest=manifest, source=candidate.source, content_sha256=content.sha256),),
    )
    resolved_catalog = resolve_pack_catalogs(organization=(catalog,)).catalogs[0]
    trust = evaluate_pack_trust(
        resolve_pack_trust_policy(organization=_trust_policy()),
        resolved_catalog,
        catalog.entries[0],
        signature_verification=stale_signature,
    )
    assert stale_signature.verified is False
    assert trust.allowed is False

    # Enterprise trust cannot be weakened by a lower-level policy that permits another publisher.
    weaker_repo = PackTrustPolicy.from_dict(
        {
            "apiVersion": "sdai.pack-trust-policy/v1",
            "requireSignatures": False,
            "allowedCatalogs": [],
            "deniedCatalogs": [],
            "allowedPublishers": ["other"],
            "deniedPublishers": [],
        }
    )
    resolved = resolve_pack_trust_policy(organization=_trust_policy(), repository=weaker_repo)
    assert resolved.require_signatures is True
    assert "other" not in resolved.allowed_publishers

    # Changed eval-suite inputs invalidate old certification evidence.
    cert_policy = resolve_pack_certification_policy(organization=_cert_policy())
    old_suite = PackEvalSuite((PackEvalCaseSpec("quality-case", "quality", "sha256:" + "a" * 64),))
    eval_evidence = PackEvalEvidence(
        manifest.identity,
        manifest.sha256,
        content.sha256,
        cert_policy.sha256,
        old_suite.sha256,
        (PackEvalCaseResult("quality-case", "quality", "sha256:" + "a" * 64, 10000, True),),
        ProducerMetadata("provider-a", "model-a", "compat-gate"),
    )
    changed_suite = PackEvalSuite((PackEvalCaseSpec("quality-case", "quality", "sha256:" + "b" * 64),))
    stale_cert = evaluate_pack_certification_suite(
        manifest, content.sha256, cert_policy, changed_suite, eval_evidence
    )
    assert stale_cert.status == "stale"
    assert stale_cert.reasons == ("eval-suite-changed",)

    # Dependency resolution still fails deterministically for missing/cyclic inputs.
    missing_manifest = PackManifest.from_dict(
        {
            **manifest.as_dict(),
            "id": "missing-root",
            "dependencies": [{"publisher": "acme", "id": "missing", "version": "=1.0.0"}],
        }
    )
    missing_candidate = PackCandidate(
        missing_manifest,
        "catalog://corp/missing-root",
        "sha256:" + "c" * 64,
    )
    with pytest.raises(PackLockError, match="SDAI-PACK-LOCK-003"):
        resolve_pack_lock([missing_candidate], [])

    a_manifest = PackManifest.from_dict(
        {
            **manifest.as_dict(),
            "id": "a",
            "dependencies": [{"publisher": "acme", "id": "b", "version": "=1.0.0"}],
        }
    )
    b_manifest = PackManifest.from_dict(
        {
            **manifest.as_dict(),
            "id": "b",
            "dependencies": [{"publisher": "acme", "id": "a", "version": "=1.0.0"}],
        }
    )
    a = PackCandidate(a_manifest, "catalog://corp/a", "sha256:" + "d" * 64)
    b = PackCandidate(b_manifest, "catalog://corp/b", "sha256:" + "e" * 64)
    with pytest.raises(PackLockError, match="SDAI-PACK-LOCK-005"):
        resolve_pack_lock([a], [b])
