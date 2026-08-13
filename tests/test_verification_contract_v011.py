from __future__ import annotations

from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess

import pytest

from sdai.trace_evidence import (
    EvidenceBinding,
    EvidenceBindingKind,
    EvidenceKind,
    EvidenceProducer,
    EvidenceStatus,
    TraceEvidence,
)
from sdai.trace_freshness import ProofFreshness
from sdai.trace_graph import TraceProvenance
from sdai.verification import (
    SEMANTIC_REVIEW_API_VERSION,
    VERIFY_REPORT_API_VERSION,
    SemanticReviewDimension,
    SemanticReviewEvidence,
    SemanticReviewState,
    VerificationCategory,
    VerificationError,
    VerificationFinding,
    VerificationFindingSource,
    VerificationOutcome,
    VerificationReport,
    VerificationSeverity,
    VerificationStatus,
    evaluate_semantic_review_freshness,
    load_semantic_review_evidence,
)


FEATURE = "VERIFY-118"


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        check=True,
        shell=False,
    )
    return result.stdout.strip()


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")
    return path


def _digest(path: Path) -> str:
    return "sha256:" + sha256(path.read_bytes()).hexdigest()


def _repo(root: Path) -> str:
    _git(root, "init")
    _git(root, "config", "user.email", "sdai@example.test")
    _git(root, "config", "user.name", "SDAI Test")
    _write(root / ".sdai" / "config.yaml", "version: 1\n")
    _write(
        root / "specs" / "changes" / FEATURE / "requirements.md",
        "# Requirements\n\n- FR-001: Sign café scripts and preserve Δ behavior.\n",
    )
    _write(
        root / "src" / "café.py",
        "# Trace: FR-001\nSIGNED = True\n",
    )
    _git(root, "add", ".")
    _git(root, "commit", "-m", "baseline")
    return _git(root, "rev-parse", "HEAD")


def _review(
    root: Path,
    commit: str,
    *,
    provider: str = "codex",
    model: str = "model-a",
    status: EvidenceStatus = EvidenceStatus.PASSED,
    kind: EvidenceKind = EvidenceKind.REVIEW,
    review_id: str = "REVIEW-REQ-001",
    subject: str = "requirement:FR-001",
    dimension: SemanticReviewDimension = SemanticReviewDimension.REQUIREMENT_SATISFACTION,
) -> SemanticReviewEvidence:
    review_path = f"specs/changes/{FEATURE}/reviews/{review_id}.json"
    evidence = TraceEvidence(
        evidence_id=review_id,
        kind=kind,
        status=status,
        subject=subject,
        git_commit=commit,
        bindings=(
            EvidenceBinding(
                EvidenceBindingKind.SOURCE,
                "src/café.py",
                _digest(root / "src" / "café.py"),
            ),
        ),
        provenance=(TraceProvenance(review_path, 1),),
        producer=EvidenceProducer("reviewer", provider, model),
        result={
            "verdict": status.value,
            "observations": ["FR-001 behavior matches the reviewed implementation."],
        },
        command=(),
        tool="semantic-review",
    )
    return SemanticReviewEvidence(
        review_id=review_id,
        dimension=dimension,
        subject=subject,
        summary="Requirement behavior is satisfied by the current implementation.",
        evidence=evidence,
    )


def _deterministic_finding(status: VerificationStatus) -> VerificationFinding:
    return VerificationFinding(
        code="SDAI_VERIFY_TRACE_001",
        source=VerificationFindingSource.DETERMINISTIC,
        category=VerificationCategory.TRACE_COVERAGE,
        severity=VerificationSeverity.BLOCKING,
        status=status,
        message="Current trace coverage must satisfy policy.",
        subject="requirement:FR-001",
        provenance=(
            TraceProvenance(
                f"specs/changes/{FEATURE}/requirements.md",
                3,
                detail="FR-001 declaration",
            ),
        ),
        metadata={"risk": "critical"},
    )


def _semantic_finding(review: SemanticReviewEvidence) -> VerificationFinding:
    return VerificationFinding(
        code="SDAI_VERIFY_SEMANTIC_001",
        source=VerificationFindingSource.SEMANTIC,
        category=VerificationCategory.REQUIREMENT_SATISFACTION,
        severity=VerificationSeverity.BLOCKING,
        status=(
            VerificationStatus.PASS
            if review.status is EvidenceStatus.PASSED
            else VerificationStatus.FAIL
        ),
        message=review.summary,
        subject=review.subject,
        evidence_truth_sha256=review.truth_sha256,
        provenance=review.evidence.provenance,
        metadata={"dimension": review.dimension.value},
    )


def test_semantic_review_truth_is_provider_model_independent_and_round_trips(tmp_path: Path) -> None:
    commit = _repo(tmp_path)
    first = _review(tmp_path, commit, provider="codex", model="model-a")
    second = _review(tmp_path, commit, provider="claude", model="model-b")

    assert first.evidence.truth_sha256 == second.evidence.truth_sha256
    assert first.evidence.sha256 != second.evidence.sha256
    assert first.truth_sha256 == second.truth_sha256
    assert first.sha256 != second.sha256
    assert SemanticReviewEvidence.from_json(first.to_json()) == first
    assert json.loads(first.to_json())["apiVersion"] == SEMANTIC_REVIEW_API_VERSION


def test_semantic_review_freshness_is_bound_to_current_git_and_source_bytes(tmp_path: Path) -> None:
    commit = _repo(tmp_path)
    review = _review(tmp_path, commit)

    current = evaluate_semantic_review_freshness(tmp_path, review)
    assert current.freshness is ProofFreshness.VALID
    assert current.satisfies_current_verification is True
    assert current.truth_sha256 == review.truth_sha256

    _write(tmp_path / "src" / "café.py", "# Trace: FR-001\nSIGNED = False  # Δ changed\n")
    stale = evaluate_semantic_review_freshness(tmp_path, review)
    assert stale.freshness is ProofFreshness.STALE
    assert stale.satisfies_current_verification is False
    assert any("SHA-256 changed" in reason for reason in stale.reasons)


def test_failed_or_blocked_semantic_review_never_satisfies_current_verification(tmp_path: Path) -> None:
    commit = _repo(tmp_path)
    failed = _review(tmp_path, commit, status=EvidenceStatus.FAILED)
    blocked = _review(tmp_path, commit, status=EvidenceStatus.BLOCKED, review_id="REVIEW-REQ-002")

    failed_state = evaluate_semantic_review_freshness(tmp_path, failed)
    blocked_state = evaluate_semantic_review_freshness(tmp_path, blocked)

    assert failed_state.freshness is ProofFreshness.BLOCKED
    assert blocked_state.freshness is ProofFreshness.BLOCKED
    assert failed_state.satisfies_current_verification is False
    assert blocked_state.satisfies_current_verification is False


def test_semantic_review_rejects_non_review_kind_recorded_status_and_identity_mismatch(tmp_path: Path) -> None:
    commit = _repo(tmp_path)

    with pytest.raises(VerificationError, match="SDAI-VERIFY-003"):
        _review(tmp_path, commit, kind=EvidenceKind.QUALITY)

    with pytest.raises(VerificationError, match="SDAI-VERIFY-003"):
        _review(tmp_path, commit, status=EvidenceStatus.RECORDED)

    evidence = _review(tmp_path, commit).evidence
    with pytest.raises(VerificationError, match="review_id must match evidence_id"):
        SemanticReviewEvidence(
            review_id="REVIEW-DIFFERENT",
            dimension=SemanticReviewDimension.REQUIREMENT_SATISFACTION,
            subject=evidence.subject,
            summary="Mismatch must fail closed.",
            evidence=evidence,
        )


def test_semantic_review_parser_rejects_unknown_or_tampered_fields(tmp_path: Path) -> None:
    commit = _repo(tmp_path)
    review = _review(tmp_path, commit)
    payload = review.as_dict()
    payload["unexpected"] = True

    with pytest.raises(VerificationError, match="fields do not match"):
        SemanticReviewEvidence.from_mapping(payload)

    tampered = review.as_dict()
    tampered["summary"] = "Provider claims a different conclusion."
    with pytest.raises(VerificationError, match="truth SHA-256"):
        SemanticReviewEvidence.from_mapping(tampered)


def test_semantic_finding_requires_truth_bound_evidence() -> None:
    with pytest.raises(VerificationError, match="semantic verification findings require"):
        VerificationFinding(
            code="SDAI_VERIFY_SEMANTIC_002",
            source=VerificationFindingSource.SEMANTIC,
            category=VerificationCategory.ARCHITECTURE_INTENT,
            severity=VerificationSeverity.REVIEW,
            status=VerificationStatus.REVIEW_REQUIRED,
            message="Architecture intent requires independent semantic review.",
            subject="feature:VERIFY-118",
            provenance=(TraceProvenance(f"specs/changes/{FEATURE}/requirements.md", 1),),
        )


def test_deterministic_blocker_cannot_be_overridden_by_semantic_pass(tmp_path: Path) -> None:
    commit = _repo(tmp_path)
    review = _review(tmp_path, commit)
    review_state = evaluate_semantic_review_freshness(tmp_path, review)
    report = VerificationReport(
        feature_id=FEATURE,
        git_commit=commit,
        input_sha256="sha256:" + ("1" * 64),
        findings=(
            _semantic_finding(review),
            _deterministic_finding(VerificationStatus.FAIL),
        ),
        semantic_reviews=(review_state,),
    )

    assert report.outcome is VerificationOutcome.BLOCKED
    assert report.passed is False
    assert any(
        item.source is VerificationFindingSource.DETERMINISTIC
        and item.status is VerificationStatus.FAIL
        for item in report.findings
    )
    assert any(
        item.source is VerificationFindingSource.SEMANTIC
        and item.status is VerificationStatus.PASS
        for item in report.findings
    )


def test_verification_report_outcome_is_monotonic_and_canonical(tmp_path: Path) -> None:
    commit = _repo(tmp_path)
    passing = _deterministic_finding(VerificationStatus.PASS)
    review_required = VerificationFinding(
        code="SDAI_VERIFY_REVIEW_001",
        source=VerificationFindingSource.DETERMINISTIC,
        category=VerificationCategory.FAILURE_BEHAVIOR,
        severity=VerificationSeverity.REVIEW,
        status=VerificationStatus.REVIEW_REQUIRED,
        message="Failure behavior requires semantic review evidence.",
        provenance=(TraceProvenance(f"specs/changes/{FEATURE}/requirements.md", 3),),
    )

    needs_review = VerificationReport(
        FEATURE,
        commit,
        "sha256:" + ("2" * 64),
        (review_required, passing),
    )
    same = VerificationReport(
        FEATURE,
        commit,
        "sha256:" + ("2" * 64),
        (passing, review_required),
    )

    assert needs_review.outcome is VerificationOutcome.REVIEW
    assert needs_review.to_json() == same.to_json()
    assert VerificationReport.from_json(needs_review.to_json()) == needs_review
    assert json.loads(needs_review.to_json())["apiVersion"] == VERIFY_REPORT_API_VERSION

    passed = VerificationReport(
        FEATURE,
        commit,
        "sha256:" + ("3" * 64),
        (passing,),
    )
    assert passed.outcome is VerificationOutcome.PASSED
    assert passed.passed is True


def test_provider_change_does_not_change_verification_report_truth(tmp_path: Path) -> None:
    commit = _repo(tmp_path)
    first_review = _review(tmp_path, commit, provider="codex", model="one")
    second_review = _review(tmp_path, commit, provider="claude", model="two")

    first = VerificationReport(
        FEATURE,
        commit,
        "sha256:" + ("4" * 64),
        (_deterministic_finding(VerificationStatus.PASS), _semantic_finding(first_review)),
        (evaluate_semantic_review_freshness(tmp_path, first_review),),
    )
    second = VerificationReport(
        FEATURE,
        commit,
        "sha256:" + ("4" * 64),
        (_semantic_finding(second_review), _deterministic_finding(VerificationStatus.PASS)),
        (evaluate_semantic_review_freshness(tmp_path, second_review),),
    )

    assert first.sha256 == second.sha256
    assert first.to_json() == second.to_json()


def test_semantic_review_state_round_trip_preserves_utf8_and_current_truth(tmp_path: Path) -> None:
    commit = _repo(tmp_path)
    review = _review(
        tmp_path,
        commit,
        review_id="REVIEW-CAFE-001",
        subject="requirement:FR-001 café Δ",
        dimension=SemanticReviewDimension.UNDOCUMENTED_BEHAVIOR,
    )
    state = evaluate_semantic_review_freshness(tmp_path, review)
    restored = SemanticReviewState.from_mapping(state.as_dict())

    assert restored == state
    assert "café" in restored.subject
    assert restored.freshness is ProofFreshness.VALID


def test_semantic_review_loader_is_safe_and_rejects_symlink(tmp_path: Path) -> None:
    commit = _repo(tmp_path)
    review = _review(tmp_path, commit)
    path = tmp_path / "specs" / "changes" / FEATURE / "reviews" / "review.json"
    _write(path, review.to_json())

    assert load_semantic_review_evidence(tmp_path, path) == review

    link = path.with_name("review-link.json")
    try:
        os.symlink(path, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is unavailable on this platform")
    with pytest.raises(VerificationError, match="symlink"):
        load_semantic_review_evidence(tmp_path, link)
