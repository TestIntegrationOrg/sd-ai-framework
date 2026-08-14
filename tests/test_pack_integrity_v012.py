from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import hmac
import json
from pathlib import Path

import pytest
import yaml

from sdai.pack_integrity import (
    IntegrityStatus,
    PackIntegrityError,
    PackSignatureEvidence,
    SignatureStatus,
    build_pack_content_index,
    build_pack_signature_payload,
    load_pack_signature_evidence,
    verify_pack_signature,
)
from sdai.pack_manifest import PACK_MANIFEST_API_VERSION, PackManifest, load_pack_manifest


class HmacTestVerifier:
    def __init__(self, keys: dict[str, bytes]) -> None:
        self.keys = keys

    def verify(self, *, key_id: str, payload: bytes, signature: bytes) -> bool:
        key = self.keys.get(key_id)
        if key is None:
            return False
        expected = hmac.new(key, payload, sha256).digest()
        return hmac.compare_digest(expected, signature)


class ExplodingVerifier:
    def verify(self, *, key_id: str, payload: bytes, signature: bytes) -> bool:
        raise RuntimeError("backend unavailable")


def _raw_manifest() -> dict[str, object]:
    return {
        "apiVersion": PACK_MANIFEST_API_VERSION,
        "id": "secure-coding",
        "publisher": "acme",
        "version": "1.2.3",
        "description": "Secure café engineering Δ pack",
        "capabilities": ["skills", "workflows"],
        "contentRoots": ["skills", "workflows"],
        "dependencies": [],
        "compatibility": {
            "framework": ">=0.5.4,<1.0.0",
            "apis": ["sdai.pack-manifest/v1"],
        },
    }


def _workspace(root: Path) -> tuple[Path, PackManifest]:
    (root / "skills" / "café").mkdir(parents=True)
    (root / "workflows").mkdir(parents=True)
    (root / "skills" / "café" / "review.md").write_text(
        "Review Δ requirements.\n",
        encoding="utf-8",
        newline="\n",
    )
    (root / "workflows" / "secure.yaml").write_text(
        "steps:\n  - verify\n",
        encoding="utf-8",
        newline="\n",
    )
    manifest_path = root / "pack.yaml"
    manifest_path.write_text(
        yaml.safe_dump(_raw_manifest(), sort_keys=False, allow_unicode=True),
        encoding="utf-8",
        newline="\n",
    )
    return manifest_path, load_pack_manifest(manifest_path)


def _evidence(root: Path, manifest: PackManifest, *, key_id: str = "key-1", key: bytes = b"secret") -> PackSignatureEvidence:
    content = build_pack_content_index(root, manifest)
    payload = build_pack_signature_payload(manifest, content)
    signature = hmac.new(key, payload.to_bytes(), sha256).digest()
    return PackSignatureEvidence.create(
        payload,
        algorithm="test-hmac-sha256",
        key_id=key_id,
        signature=signature,
    )


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink()
    }


def test_content_index_is_canonical_utf8_and_input_file_order_independent(tmp_path: Path) -> None:
    _, manifest = _workspace(tmp_path)

    first = build_pack_content_index(tmp_path, manifest)
    second = build_pack_content_index(tmp_path, manifest)

    assert first.to_json() == second.to_json()
    assert first.sha256 == second.sha256
    assert [entry.path for entry in first.entries] == [
        "skills/café/review.md",
        "workflows/secure.yaml",
    ]
    assert all(entry.sha256.startswith("sha256:") for entry in first.entries)
    assert "café" in first.to_json()


def test_signature_payload_binds_pack_publisher_manifest_and_content() -> None:
    manifest = PackManifest.from_dict(_raw_manifest())
    root = Path(".")
    # Build a content index directly from canonical entries via a real workspace in
    # the integration tests; this assertion focuses on payload field binding.
    from sdai.pack_integrity import PackContentEntry, PackContentIndex

    content = PackContentIndex(
        (
            PackContentEntry(
                path="skills/a.md",
                sha256="sha256:" + sha256(b"a").hexdigest(),
                size=1,
            ),
        )
    )
    payload = build_pack_signature_payload(manifest, content)

    assert payload.pack_identity == "acme/secure-coding@1.2.3"
    assert payload.publisher == "acme"
    assert payload.manifest_sha256 == manifest.sha256
    assert payload.content_sha256 == content.sha256
    assert payload.to_bytes() == payload.to_json().encode("utf-8")
    assert json.loads(payload.to_json())["apiVersion"] == "sdai.pack-signature-payload/v1"
    assert root == Path(".")


def test_valid_signature_is_cryptographically_verified_but_trust_is_not_evaluated(tmp_path: Path) -> None:
    _, manifest = _workspace(tmp_path)
    evidence = _evidence(tmp_path, manifest)

    report = verify_pack_signature(
        tmp_path,
        manifest,
        evidence,
        {"test-hmac-sha256": HmacTestVerifier({"key-1": b"secret"})},
    )

    assert report.verified is True
    assert report.integrity_status is IntegrityStatus.CURRENT
    assert report.publisher_bound is True
    assert report.signature_status is SignatureStatus.VALID
    assert report.trust_status == "not-evaluated"
    assert report.reasons == ()
    assert json.loads(report.to_json())["verified"] is True


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ("change", "content-stale"),
        ("delete", "content-stale"),
        ("add", "content-stale"),
    ],
)
def test_any_declared_content_change_invalidates_current_signature(
    tmp_path: Path,
    mutation: str,
    reason: str,
) -> None:
    _, manifest = _workspace(tmp_path)
    evidence = _evidence(tmp_path, manifest)
    target = tmp_path / "skills" / "café" / "review.md"
    if mutation == "change":
        target.write_text("changed truth\n", encoding="utf-8", newline="\n")
    elif mutation == "delete":
        target.unlink()
    else:
        (tmp_path / "skills" / "extra.md").write_text("extra declared content\n", encoding="utf-8")

    report = verify_pack_signature(
        tmp_path,
        manifest,
        evidence,
        {"test-hmac-sha256": HmacTestVerifier({"key-1": b"secret"})},
    )

    assert report.verified is False
    assert report.integrity_status is IntegrityStatus.STALE
    assert report.signature_status is SignatureStatus.VALID
    assert reason in report.reasons


def test_manifest_truth_change_invalidates_signature_even_when_content_is_unchanged(tmp_path: Path) -> None:
    _, manifest = _workspace(tmp_path)
    evidence = _evidence(tmp_path, manifest)
    changed = deepcopy(_raw_manifest())
    changed["description"] = "Changed manifest truth"
    changed_manifest = PackManifest.from_dict(changed)

    report = verify_pack_signature(
        tmp_path,
        changed_manifest,
        evidence,
        {"test-hmac-sha256": HmacTestVerifier({"key-1": b"secret"})},
    )

    assert report.verified is False
    assert report.integrity_status is IntegrityStatus.STALE
    assert "manifest-stale" in report.reasons


def test_wrong_publisher_cannot_become_verified_even_with_valid_signature_for_evidence_payload(tmp_path: Path) -> None:
    _, manifest = _workspace(tmp_path)
    original = _evidence(tmp_path, manifest)
    payload = original.payload()
    from sdai.pack_integrity import PackSignaturePayload

    wrong_payload = PackSignaturePayload(
        pack_identity=payload.pack_identity,
        publisher="other-publisher",
        manifest_sha256=payload.manifest_sha256,
        content_sha256=payload.content_sha256,
    )
    wrong_signature = hmac.new(b"secret", wrong_payload.to_bytes(), sha256).digest()
    wrong = PackSignatureEvidence.create(
        wrong_payload,
        algorithm="test-hmac-sha256",
        key_id="key-1",
        signature=wrong_signature,
    )

    report = verify_pack_signature(
        tmp_path,
        manifest,
        wrong,
        {"test-hmac-sha256": HmacTestVerifier({"key-1": b"secret"})},
    )

    assert report.signature_status is SignatureStatus.VALID
    assert report.publisher_bound is False
    assert report.verified is False
    assert "publisher-mismatch" in report.reasons


def test_wrong_key_invalid_signature_unsupported_algorithm_and_backend_error_fail_closed(tmp_path: Path) -> None:
    _, manifest = _workspace(tmp_path)
    evidence = _evidence(tmp_path, manifest)

    wrong_key = verify_pack_signature(
        tmp_path,
        manifest,
        evidence,
        {"test-hmac-sha256": HmacTestVerifier({"key-1": b"wrong"})},
    )
    unsupported = verify_pack_signature(tmp_path, manifest, evidence, {})
    backend_error = verify_pack_signature(
        tmp_path,
        manifest,
        evidence,
        {"test-hmac-sha256": ExplodingVerifier()},
    )

    assert wrong_key.signature_status is SignatureStatus.INVALID
    assert wrong_key.verified is False
    assert "invalid-signature" in wrong_key.reasons
    assert unsupported.signature_status is SignatureStatus.UNSUPPORTED
    assert unsupported.verified is False
    assert "unsupported-algorithm" in unsupported.reasons
    assert backend_error.signature_status is SignatureStatus.ERROR
    assert backend_error.verified is False
    assert "verifier-error" in backend_error.reasons


def test_evidence_round_trip_reconstructs_exact_signed_payload_bytes(tmp_path: Path) -> None:
    _, manifest = _workspace(tmp_path)
    evidence = _evidence(tmp_path, manifest)

    round_trip = PackSignatureEvidence.from_json(evidence.to_json())

    assert round_trip.to_json() == evidence.to_json()
    assert round_trip.sha256 == evidence.sha256
    assert round_trip.payload().to_bytes() == evidence.payload().to_bytes()
    assert round_trip.payload_sha256 == evidence.payload().sha256


def test_malformed_duplicate_and_tampered_evidence_fails_closed(tmp_path: Path) -> None:
    _, manifest = _workspace(tmp_path)
    evidence = _evidence(tmp_path, manifest)
    raw = evidence.as_dict()
    raw["payloadSha256"] = "sha256:" + "0" * 64
    with pytest.raises(PackIntegrityError, match="payloadSha256 does not match"):
        PackSignatureEvidence.from_dict(raw)

    duplicate = evidence.to_json().replace(
        '"algorithm":"test-hmac-sha256"',
        '"algorithm":"test-hmac-sha256","algorithm":"test-hmac-sha256"',
    )
    with pytest.raises(PackIntegrityError, match="duplicate key 'algorithm'"):
        PackSignatureEvidence.from_json(duplicate)

    raw = evidence.as_dict()
    raw["signature"] = "not base64!!!"
    with pytest.raises(PackIntegrityError, match="canonical Base64"):
        PackSignatureEvidence.from_dict(raw)


def test_integrity_and_verification_are_read_only(tmp_path: Path) -> None:
    _, manifest = _workspace(tmp_path)
    evidence = _evidence(tmp_path, manifest)
    before = _snapshot(tmp_path)

    build_pack_content_index(tmp_path, manifest)
    verify_pack_signature(
        tmp_path,
        manifest,
        evidence,
        {"test-hmac-sha256": HmacTestVerifier({"key-1": b"secret"})},
    )

    assert _snapshot(tmp_path) == before


def test_signature_evidence_loader_rejects_symlink(tmp_path: Path) -> None:
    _, manifest = _workspace(tmp_path)
    evidence = _evidence(tmp_path, manifest)
    target = tmp_path / "signature.json"
    target.write_text(evidence.to_json(), encoding="utf-8", newline="\n")
    link = tmp_path / "signature-link.json"
    try:
        link.symlink_to(target.name)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable on this platform: {exc}")

    with pytest.raises(PackIntegrityError, match="must not be a symlink"):
        load_pack_signature_evidence(link)
