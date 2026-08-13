from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import subprocess

from sdai.artifact_state import record_artifact_state
from sdai.trace_evidence import (
    EvidenceBinding,
    EvidenceBindingKind,
    EvidenceKind,
    EvidenceProducer,
    EvidenceStatus,
    TraceEvidence,
)
from sdai.trace_graph import TraceProvenance
from sdai.verification import (
    SemanticReviewDimension,
    SemanticReviewEvidence,
    VerificationFindingSource,
    VerificationOutcome,
)
from sdai.verify_engine import verify_feature
from sdai.version_entrypoint import main


FEATURE = "VERIFY-119"


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


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and ".git" not in path.parts
    }


def _semantic_context(root: Path, name: str) -> Path:
    return root / ".sdai" / "verification" / FEATURE / "context" / f"{name}.txt"


def _review_path(root: Path, review_id: str) -> Path:
    return root / ".sdai" / "verification" / FEATURE / "reviews" / f"{review_id}.json"


def _workspace(root: Path) -> str:
    _git(root, "init")
    _git(root, "config", "user.email", "sdai@example.test")
    _git(root, "config", "user.name", "SDAI Verify Test")
    _write(root / ".sdai" / "config.yaml", "version: 1\n")
    feature = root / "specs" / "changes" / FEATURE
    _write(
        feature / "requirements.md",
        """# Requirements

- FR-001: Sign café scripts. TASK-001 implements it and TEST-001 verifies it.
- NFR-001: Signing must preserve Δ behavior. TASK-001 implements it and TEST-001 verifies it.
""",
    )
    _write(
        feature / "architecture.md",
        "# Architecture\n\nThe signing component implements FR-001 and NFR-001.\n",
    )
    _write(
        feature / "plan.md",
        "# Plan\n\nImplement TASK-001 for FR-001 and NFR-001, then run TEST-001.\n",
    )
    _write(
        feature / "tasks.md",
        "# Tasks\n\n- [x] TASK-001: Implement FR-001 and NFR-001; verified by TEST-001.\n",
    )
    _write(
        feature / "tests.md",
        "# Tests\n\n- TEST-001: Verify FR-001 and NFR-001 through TASK-001.\n",
    )
    _write(
        root / "src" / "signing" / "café.py",
        "# Trace: FR-001 NFR-001 TASK-001 TEST-001\nSIGNED = True\n",
    )
    _write(
        root / "tests" / "test_signing.py",
        "# Trace: FR-001 NFR-001 TASK-001 TEST-001\ndef test_signing():\n    assert True\n",
    )
    for name in ("FR-001", "NFR-001", "failure"):
        _write(_semantic_context(root, name), f"Semantic review context for {name} café Δ.\n")

    for artifact_id in ("requirements", "architecture", "plan", "tasks", "tests"):
        record_artifact_state(root, FEATURE, artifact_id, risk="standard", environ={})

    _git(root, "add", ".")
    _git(root, "commit", "-m", "verification baseline")
    commit = _git(root, "rev-parse", "HEAD")

    _typed_evidence(
        root,
        commit,
        evidence_id="EVIDENCE-FR-001",
        kind=EvidenceKind.TEST,
        status=EvidenceStatus.PASSED,
        subject="requirement:FR-001",
    )
    _typed_evidence(
        root,
        commit,
        evidence_id="EVIDENCE-NFR-001",
        kind=EvidenceKind.TEST,
        status=EvidenceStatus.PASSED,
        subject="requirement:NFR-001",
    )
    _semantic_review(
        root,
        commit,
        review_id="REVIEW-FR-001",
        dimension=SemanticReviewDimension.REQUIREMENT_SATISFACTION,
        subject="requirement:FR-001",
        context_name="FR-001",
    )
    _semantic_review(
        root,
        commit,
        review_id="REVIEW-NFR-001",
        dimension=SemanticReviewDimension.REQUIREMENT_SATISFACTION,
        subject="requirement:NFR-001",
        context_name="NFR-001",
    )
    _semantic_review(
        root,
        commit,
        review_id="REVIEW-FAILURE",
        dimension=SemanticReviewDimension.FAILURE_BEHAVIOR,
        subject=f"feature:{FEATURE}",
        context_name="failure",
    )
    return commit


def _typed_evidence(
    root: Path,
    commit: str,
    *,
    evidence_id: str,
    kind: EvidenceKind,
    status: EvidenceStatus,
    subject: str,
) -> Path:
    relative = f"specs/changes/{FEATURE}/evidence/{evidence_id}.json"
    record = TraceEvidence(
        evidence_id=evidence_id,
        kind=kind,
        status=status,
        subject=subject,
        git_commit=commit,
        bindings=(
            EvidenceBinding(
                EvidenceBindingKind.SOURCE,
                "src/signing/café.py",
                _digest(root / "src" / "signing" / "café.py"),
            ),
        ),
        provenance=(TraceProvenance(relative, 1),),
        producer=EvidenceProducer("tester", "codex", "model-a"),
        result={"passed": status is EvidenceStatus.PASSED},
        command=("pytest", "-q"),
        tool="pytest",
    )
    return _write(root / relative, record.to_json())


def _semantic_review(
    root: Path,
    commit: str,
    *,
    review_id: str,
    dimension: SemanticReviewDimension,
    subject: str,
    context_name: str,
    status: EvidenceStatus = EvidenceStatus.PASSED,
    provider: str = "codex",
    model: str = "model-a",
) -> Path:
    path = _review_path(root, review_id)
    context = _semantic_context(root, context_name)
    relative = path.relative_to(root).as_posix()
    evidence = TraceEvidence(
        evidence_id=review_id,
        kind=EvidenceKind.REVIEW,
        status=status,
        subject=subject,
        git_commit=commit,
        bindings=(
            EvidenceBinding(
                EvidenceBindingKind.EVIDENCE,
                context.relative_to(root).as_posix(),
                _digest(context),
            ),
        ),
        provenance=(TraceProvenance(relative, 1),),
        producer=EvidenceProducer("reviewer", provider, model),
        result={"verdict": status.value, "dimension": dimension.value},
        command=(),
        tool="semantic-review",
    )
    review = SemanticReviewEvidence(
        review_id=review_id,
        dimension=dimension,
        subject=subject,
        summary=f"{dimension.value} review for {subject} is {status.value}.",
        evidence=evidence,
    )
    return _write(path, review.to_json())


def test_verify_feature_passes_with_fresh_deterministic_and_semantic_truth(tmp_path: Path) -> None:
    _workspace(tmp_path)

    report = verify_feature(tmp_path, FEATURE, risk="standard", environ={})

    assert report.outcome is VerificationOutcome.PASSED
    assert report.passed is True
    assert any(item.source is VerificationFindingSource.SEMANTIC for item in report.findings)
    assert all(
        item.status.value == "pass"
        for item in report.findings
        if item.source is VerificationFindingSource.SEMANTIC
    )
    assert len(report.semantic_reviews) == 3


def test_sdai_verify_json_is_machine_clean_read_only_and_stable(tmp_path: Path, capsys) -> None:
    _workspace(tmp_path)
    before = _snapshot(tmp_path)

    first_code = main(["verify", FEATURE, "--risk", "standard", "--json", "--path", str(tmp_path)])
    first = capsys.readouterr()
    second_code = main(["verify", FEATURE, "--risk", "standard", "--json", "--path", str(tmp_path)])
    second = capsys.readouterr()

    assert first_code == 0 == second_code
    assert first.err == "" == second.err
    assert json.loads(first.out)["apiVersion"] == "sdai.verify-report/v1"
    assert first.out == second.out
    assert _snapshot(tmp_path) == before


def test_missing_required_semantic_review_returns_review_exit_not_provider_execution(
    tmp_path: Path,
    capsys,
) -> None:
    _workspace(tmp_path)
    _review_path(tmp_path, "REVIEW-FAILURE").unlink()

    code = main(["verify", FEATURE, "--json", "--path", str(tmp_path)])
    payload = json.loads(capsys.readouterr().out)

    assert code == 3
    assert payload["outcome"] == "review"
    assert any(
        finding["code"] == "SDAI_VERIFY_SEMANTIC_REQUIRED"
        and finding["category"] == "failure-behavior"
        for finding in payload["findings"]
    )


def test_stale_semantic_review_is_review_required_without_changing_deterministic_sources(
    tmp_path: Path,
) -> None:
    _workspace(tmp_path)
    _write(_semantic_context(tmp_path, "failure"), "Semantic context changed after review Δ.\n")

    report = verify_feature(tmp_path, FEATURE, risk="standard", environ={})

    assert report.outcome is VerificationOutcome.REVIEW
    stale = [item for item in report.findings if item.code == "SDAI_VERIFY_SEMANTIC_STALE"]
    assert len(stale) == 1
    assert stale[0].category.value == "failure-behavior"
    assert stale[0].status.value == "stale"


def test_current_failed_semantic_review_blocks_verification(tmp_path: Path) -> None:
    commit = _workspace(tmp_path)
    _semantic_review(
        tmp_path,
        commit,
        review_id="REVIEW-FR-001",
        dimension=SemanticReviewDimension.REQUIREMENT_SATISFACTION,
        subject="requirement:FR-001",
        context_name="FR-001",
        status=EvidenceStatus.FAILED,
    )

    report = verify_feature(tmp_path, FEATURE, risk="standard", environ={})

    assert report.outcome is VerificationOutcome.BLOCKED
    assert any(
        item.code == "SDAI_VERIFY_SEMANTIC_REJECTED"
        and item.subject == "requirement:FR-001"
        and item.status.value == "fail"
        for item in report.findings
    )


def test_deterministic_analysis_blocker_cannot_be_overridden_by_semantic_pass(tmp_path: Path) -> None:
    _workspace(tmp_path)
    _write(
        tmp_path / "specs" / "changes" / FEATURE / "security" / "threats.yaml",
        "threat_id: THREAT-001\nstatus: open\nreferences: [FR-001]\n",
    )

    report = verify_feature(tmp_path, FEATURE, risk="standard", environ={})

    assert report.outcome is VerificationOutcome.BLOCKED
    assert any(
        item.code == "SDAI_VERIFY_ANALYSIS_UNMITIGATED_THREAT"
        and item.source is VerificationFindingSource.DETERMINISTIC
        for item in report.findings
    )
    assert any(
        item.code == "SDAI_VERIFY_SEMANTIC_PASS"
        and item.status.value == "pass"
        for item in report.findings
    )


def test_current_failed_execution_evidence_blocks_without_invalidating_requirement_proof(
    tmp_path: Path,
) -> None:
    commit = _workspace(tmp_path)
    _typed_evidence(
        tmp_path,
        commit,
        evidence_id="EVIDENCE-EXEC-FAILED",
        kind=EvidenceKind.EXECUTION,
        status=EvidenceStatus.FAILED,
        subject="task:TASK-001",
    )

    report = verify_feature(tmp_path, FEATURE, risk="standard", environ={})

    assert report.outcome is VerificationOutcome.BLOCKED
    assert any(item.code == "SDAI_VERIFY_EXECUTION_FAILED" for item in report.findings)


def test_corrupt_semantic_review_fails_closed_with_operational_exit(tmp_path: Path, capsys) -> None:
    _workspace(tmp_path)
    _write(_review_path(tmp_path, "CORRUPT"), "{not-json\n")

    code = main(["verify", FEATURE, "--json", "--path", str(tmp_path)])
    captured = capsys.readouterr()

    assert code == 1
    assert captured.out == ""
    assert "SDAI-VERIFY-003" in captured.err


def test_provider_model_only_change_does_not_change_verification_truth(tmp_path: Path) -> None:
    commit = _workspace(tmp_path)
    first = verify_feature(tmp_path, FEATURE, risk="standard", environ={})

    _semantic_review(
        tmp_path,
        commit,
        review_id="REVIEW-FR-001",
        dimension=SemanticReviewDimension.REQUIREMENT_SATISFACTION,
        subject="requirement:FR-001",
        context_name="FR-001",
        provider="another-provider",
        model="another-model",
    )
    second = verify_feature(tmp_path, FEATURE, risk="standard", environ={})

    assert first.input_sha256 == second.input_sha256
    assert first.sha256 == second.sha256
    assert first.to_json() == second.to_json()


def test_human_output_separates_deterministic_and_semantic_findings(tmp_path: Path, capsys) -> None:
    _workspace(tmp_path)

    code = main(["verify", FEATURE, "--path", str(tmp_path)])
    captured = capsys.readouterr()

    assert code == 0
    assert "Verify feature=VERIFY-119 outcome=passed" in captured.out
    assert "Semantic findings:" in captured.out
    assert "Semantic review evidence:" in captured.out
    assert "requirement-satisfaction" in captured.out
