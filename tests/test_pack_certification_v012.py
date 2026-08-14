from __future__ import annotations

from pathlib import Path

import yaml

from sdai.pack_certification import (
    CertificationRequirement,
    PackCertificationPolicy,
    PackEvalCaseResult,
    PackEvalEvidence,
    ProducerMetadata,
    evaluate_pack_certification,
    resolve_pack_certification_policy,
)
from sdai.pack_integrity import build_pack_content_index
from sdai.pack_manifest import PACK_MANIFEST_API_VERSION, load_pack_manifest


SHA_A = "sha256:" + "a" * 64
SHA_B = "sha256:" + "b" * 64
SHA_C = "sha256:" + "c" * 64


def _pack(root: Path, *, capabilities: list[str] | None = None):
    (root / "skills").mkdir(parents=True)
    (root / "skills" / "review.md").write_text("Review café Δ requirements.\n", encoding="utf-8")
    raw = {
        "apiVersion": PACK_MANIFEST_API_VERSION,
        "id": "secure-coding",
        "publisher": "acme",
        "version": "1.2.3",
        "description": "Secure café coding Pack",
        "capabilities": capabilities or ["skills"],
        "contentRoots": ["skills"],
        "dependencies": [],
        "compatibility": {
            "framework": ">=0.5.4,<1.0.0",
            "apis": ["sdai.pack-manifest/v1"],
        },
    }
    (root / "pack.yaml").write_text(
        yaml.safe_dump(raw, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
        newline="\n",
    )
    manifest = load_pack_manifest(root / "pack.yaml")
    return manifest, build_pack_content_index(root, manifest)


def _policy(
    minimum: int = 8000,
    *,
    required: bool = True,
    dimensions: tuple[tuple[str, int], ...] = (("quality", 8000),),
    cases: tuple[str, ...] = ("required-case",),
    capabilities=(),
    risks=(),
):
    return PackCertificationPolicy(
        require_certification=required,
        default=CertificationRequirement(minimum, dimensions, cases),
        capabilities=capabilities,
        risks=risks,
    )


def _evidence(manifest, content, resolved, *, provider="mock", model="model-a", quality=9000, passed=True):
    return PackEvalEvidence(
        pack_identity=manifest.identity,
        manifest_sha256=manifest.sha256,
        content_sha256=content.sha256,
        policy_sha256=resolved.sha256,
        suite_sha256=SHA_A,
        cases=(
            PackEvalCaseResult("optional-case", "security", SHA_B, 9500, True),
            PackEvalCaseResult("required-case", "quality", SHA_C, quality, passed),
        ),
        producer=ProducerMetadata(provider, model, "sdai-eval-v1"),
    )


def test_exact_pack_policy_and_eval_inputs_certify(tmp_path: Path) -> None:
    manifest, content = _pack(tmp_path / "café-pack")
    resolved = resolve_pack_certification_policy(repository=_policy())
    evidence = _evidence(manifest, content, resolved)

    decision = evaluate_pack_certification(manifest, content.sha256, resolved, evidence)

    assert decision.certified
    assert decision.status == "certified"
    assert decision.aggregate_score_basis_points == 9250
    assert dict(decision.dimension_scores) == {"quality": 9000, "security": 9500}
    assert decision.reasons == ()


def test_enterprise_policy_cannot_be_weakened_by_repository_or_user() -> None:
    organization = _policy(9500, dimensions=(("quality", 9200),), cases=("org-case",))
    repository = _policy(7000, dimensions=(("quality", 7000),), cases=("repo-case",))
    user = _policy(1000, required=False, dimensions=(), cases=())

    resolved = resolve_pack_certification_policy(
        organization=organization,
        repository=repository,
        user=user,
    )

    assert resolved.require_certification is True
    assert resolved.default.minimum_score_basis_points == 9500
    assert dict(resolved.default.required_dimensions)["quality"] == 9200
    assert resolved.default.required_cases == ("org-case", "repo-case")
    assert [scope for scope, _ in resolved.provenance] == ["organization", "repository", "user"]


def test_provider_and_model_metadata_cannot_change_certification_truth(tmp_path: Path) -> None:
    manifest, content = _pack(tmp_path / "pack")
    resolved = resolve_pack_certification_policy(repository=_policy())
    one = _evidence(manifest, content, resolved, provider="provider-a", model="alpha")
    two = _evidence(manifest, content, resolved, provider="provider-z", model="omega")

    first = evaluate_pack_certification(manifest, content.sha256, resolved, one)
    second = evaluate_pack_certification(manifest, content.sha256, resolved, two)

    assert one.truth_sha256 == two.truth_sha256
    assert first.certified and second.certified
    assert first.reasons == second.reasons
    assert first.aggregate_score_basis_points == second.aggregate_score_basis_points
    assert first.producer != second.producer


def test_content_manifest_or_policy_change_invalidates_certification(tmp_path: Path) -> None:
    manifest, content = _pack(tmp_path / "pack")
    resolved = resolve_pack_certification_policy(repository=_policy())
    evidence = _evidence(manifest, content, resolved)

    changed_policy = resolve_pack_certification_policy(repository=_policy(8500))
    policy_decision = evaluate_pack_certification(manifest, content.sha256, changed_policy, evidence)
    assert policy_decision.status == "stale"
    assert "policy-changed" in policy_decision.reasons

    content_decision = evaluate_pack_certification(manifest, "sha256:" + "f" * 64, resolved, evidence)
    assert content_decision.status == "stale"
    assert "content-changed" in content_decision.reasons


def test_minimum_required_case_and_dimension_fail_closed(tmp_path: Path) -> None:
    manifest, content = _pack(tmp_path / "pack")
    resolved = resolve_pack_certification_policy(repository=_policy(9000, dimensions=(("quality", 9500),)))
    evidence = _evidence(manifest, content, resolved, quality=8000, passed=False)

    decision = evaluate_pack_certification(manifest, content.sha256, resolved, evidence)

    assert decision.status == "failed"
    assert set(decision.reasons) == {
        "aggregate-score-below-minimum",
        "dimension-score-below-minimum:quality",
        "required-case-failed:required-case",
    }


def test_capability_and_risk_requirements_are_composed(tmp_path: Path) -> None:
    manifest, content = _pack(tmp_path / "pack", capabilities=["skills", "workflows"])
    policy = _policy(
        0,
        dimensions=(),
        cases=(),
        capabilities=(("workflows", CertificationRequirement(8500, (("security", 9000),), ("workflow-case",))),),
        risks=(("high", CertificationRequirement(9500, (), ("high-risk-case",))),),
    )
    resolved = resolve_pack_certification_policy(organization=policy)
    evidence = PackEvalEvidence(
        pack_identity=manifest.identity,
        manifest_sha256=manifest.sha256,
        content_sha256=content.sha256,
        policy_sha256=resolved.sha256,
        suite_sha256=SHA_A,
        cases=(PackEvalCaseResult("required-case", "quality", SHA_B, 10000, True),),
        producer=ProducerMetadata("mock", "neutral", "runner"),
    )

    decision = evaluate_pack_certification(manifest, content.sha256, resolved, evidence, risk="high")

    assert decision.status == "failed"
    assert "required-case-missing:workflow-case" in decision.reasons
    assert "required-case-missing:high-risk-case" in decision.reasons
    assert "required-dimension-missing:security" in decision.reasons


def test_missing_evidence_is_rejected_only_when_policy_requires_it(tmp_path: Path) -> None:
    manifest, content = _pack(tmp_path / "pack")
    required = resolve_pack_certification_policy(repository=_policy(required=True))
    optional = resolve_pack_certification_policy(repository=_policy(required=False))

    required_decision = evaluate_pack_certification(manifest, content.sha256, required, None)
    optional_decision = evaluate_pack_certification(manifest, content.sha256, optional, None)

    assert required_decision.status == "missing"
    assert required_decision.reasons == ("certification-required",)
    assert optional_decision.status == "not-required"
    assert optional_decision.reasons == ()
