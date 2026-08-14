from __future__ import annotations

from pathlib import Path

import yaml

from sdai.pack_certification import (
    CertificationRequirement,
    PackCertificationPolicy,
    PackEvalCaseResult,
    PackEvalEvidence,
    ProducerMetadata,
    resolve_pack_certification_policy,
)
from sdai.pack_eval_suite import (
    PackEvalCaseSpec,
    PackEvalSuite,
    evaluate_pack_certification_suite,
)
from sdai.pack_integrity import build_pack_content_index
from sdai.pack_manifest import PACK_MANIFEST_API_VERSION, load_pack_manifest


def _pack(root: Path):
    (root / "skills").mkdir(parents=True)
    (root / "skills" / "review.md").write_text("Review requirements.\n", encoding="utf-8")
    raw = {
        "apiVersion": PACK_MANIFEST_API_VERSION,
        "id": "secure-coding",
        "publisher": "acme",
        "version": "1.2.3",
        "description": "Secure coding",
        "capabilities": ["skills"],
        "contentRoots": ["skills"],
        "dependencies": [],
        "compatibility": {
            "framework": ">=0.5.4,<1.0.0",
            "apis": ["sdai.pack-manifest/v1"],
        },
    }
    (root / "pack.yaml").write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    manifest = load_pack_manifest(root / "pack.yaml")
    return manifest, build_pack_content_index(root, manifest)


def _policy():
    return resolve_pack_certification_policy(
        repository=PackCertificationPolicy(
            True,
            CertificationRequirement(8000, (("quality", 8000),), ("quality-case",)),
        )
    )


def test_eval_suite_change_invalidates_old_evidence(tmp_path: Path) -> None:
    manifest, content = _pack(tmp_path / "pack")
    policy = _policy()
    old_suite = PackEvalSuite(
        (PackEvalCaseSpec("quality-case", "quality", "sha256:" + "a" * 64),)
    )
    evidence = PackEvalEvidence(
        manifest.identity,
        manifest.sha256,
        content.sha256,
        policy.sha256,
        old_suite.sha256,
        (PackEvalCaseResult("quality-case", "quality", "sha256:" + "a" * 64, 9000, True),),
        ProducerMetadata("provider", "model", "runner"),
    )
    new_suite = PackEvalSuite(
        (PackEvalCaseSpec("quality-case", "quality", "sha256:" + "b" * 64),)
    )

    decision = evaluate_pack_certification_suite(
        manifest, content.sha256, policy, new_suite, evidence
    )

    assert decision.status == "stale"
    assert decision.reasons == ("eval-suite-changed",)


def test_evidence_cannot_claim_suite_hash_with_different_case_inputs(tmp_path: Path) -> None:
    manifest, content = _pack(tmp_path / "pack")
    policy = _policy()
    suite = PackEvalSuite(
        (PackEvalCaseSpec("quality-case", "quality", "sha256:" + "a" * 64),)
    )
    evidence = PackEvalEvidence(
        manifest.identity,
        manifest.sha256,
        content.sha256,
        policy.sha256,
        suite.sha256,
        (PackEvalCaseResult("quality-case", "quality", "sha256:" + "b" * 64, 10000, True),),
        ProducerMetadata("provider", "model", "runner"),
    )

    decision = evaluate_pack_certification_suite(
        manifest, content.sha256, policy, suite, evidence
    )

    assert decision.status == "failed"
    assert decision.reasons == ("eval-case-input-mismatch:quality-case",)


def test_evidence_must_cover_exact_current_suite_case_set(tmp_path: Path) -> None:
    manifest, content = _pack(tmp_path / "pack")
    policy = _policy()
    suite = PackEvalSuite(
        (
            PackEvalCaseSpec("quality-case", "quality", "sha256:" + "a" * 64),
            PackEvalCaseSpec("security-case", "security", "sha256:" + "b" * 64),
        )
    )
    evidence = PackEvalEvidence(
        manifest.identity,
        manifest.sha256,
        content.sha256,
        policy.sha256,
        suite.sha256,
        (PackEvalCaseResult("quality-case", "quality", "sha256:" + "a" * 64, 10000, True),),
        ProducerMetadata("provider", "model", "runner"),
    )

    decision = evaluate_pack_certification_suite(
        manifest, content.sha256, policy, suite, evidence
    )

    assert decision.status == "failed"
    assert decision.reasons == ("eval-case-set-mismatch",)
