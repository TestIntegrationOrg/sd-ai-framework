from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import subprocess

import pytest

from sdai.agent_platform import AgentRuntime
from sdai.completion_barrier import CompletionBarrierError, complete_isolated_task, evaluate_task_completion
from sdai.completion_policy import CompletionDimension, CompletionStage, required_dimensions
from sdai.convergence import RemediationKind, RemediationTask
from sdai.execution_ledger import create_execution_run
from sdai.isolated_tasks import (
    IsolatedStage,
    IsolatedStageResult,
    IsolatedStageStatus,
    build_implementation_contract,
    build_isolated_invocation,
    build_review_contract,
    persist_stage_result,
    prepare_implementation_dispatch,
)
from sdai.scaffold import init_project
from sdai.trace_evidence import (
    EvidenceBinding,
    EvidenceBindingKind,
    EvidenceKind,
    EvidenceProducer,
    EvidenceStatus,
    TraceEvidence,
)
from sdai.trace_graph import TraceProvenance
from sdai.v05_scaffold import install_v05_scaffold
from sdai.verification import VerificationCategory, VerificationFindingSource, VerificationSeverity, VerificationStatus


FEATURE = "COMPLETE-122"


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=root, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, encoding="utf-8", check=True, shell=False,
    )
    return completed.stdout.strip()


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def _workspace(tmp_path: Path):
    root = tmp_path / "completion Ω workspace"
    root.mkdir()
    init_project(root)
    install_v05_scaffold(root)
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "SDAI Completion Test")
    _git(root, "config", "user.email", "sdai@example.test")
    _write(root / "specs" / "changes" / FEATURE / "requirements.md", "# Requirements\n\n- FR-001: Preserve café Δ behavior.\n")
    _write(root / "src" / "service.py", "# Trace: FR-001\nREADY = True\n")
    _write(root / "tests" / "test_service.py", "# Trace: FR-001\ndef test_ready():\n    assert True\n")
    _write(root / "specs" / FEATURE / "00-intake.md", "# execution anchor\n")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "baseline")
    baseline = _git(root, "rev-parse", "HEAD")
    ledger = create_execution_run(root, FEATURE, "enterprise", baseline, run_id="run-complete-122")
    task = RemediationTask(
        task_id="REMEDIATE-cafe1220cafe1220",
        feature_id=FEATURE,
        round_id="ROUND-cafe1220cafe1220",
        verification_report_sha256="sha256:" + "1" * 64,
        verification_input_sha256="sha256:" + "2" * 64,
        finding_sha256="sha256:" + "3" * 64,
        finding_code="SDAI_VERIFY_TRACE_GAP",
        finding_source=VerificationFindingSource.DETERMINISTIC,
        category=VerificationCategory.TRACE_COVERAGE,
        severity=VerificationSeverity.BLOCKING,
        status=VerificationStatus.FAIL,
        subject="requirement:FR-001",
        summary="Implement FR-001 without changing requirement truth.",
        remediation_kind=RemediationKind.IMPLEMENTATION,
        allowed_roots=("src", "tests"),
        forbidden_roots=(f"specs/changes/{FEATURE}/requirements.md", "specs/current"),
        provenance=(TraceProvenance(f"specs/changes/{FEATURE}/requirements.md", 3),),
    )
    return root, ledger, task


def _passed(prepared, root: Path, output: str) -> IsolatedStageResult:
    return IsolatedStageResult(prepared.record, IsolatedStageStatus.PASSED, _git(root, "rev-parse", "HEAD"), output)


def _accepted_task(root: Path, ledger, task: RemediationTask):
    dispatch = prepare_implementation_dispatch(ledger, task)
    implementation = build_isolated_invocation(AgentRuntime(root), build_implementation_contract(root, task, dispatch))
    impl_result = _passed(implementation, root, "implementation passed")
    persist_stage_result(root, implementation.contract, impl_result, ledger=ledger)
    spec = build_isolated_invocation(
        AgentRuntime(root),
        build_review_contract(root, task, impl_result, IsolatedStage.SPEC_COMPLIANCE_REVIEW),
    )
    spec_result = _passed(spec, root, "spec review passed")
    persist_stage_result(root, spec.contract, spec_result, ledger=ledger)
    code = build_isolated_invocation(
        AgentRuntime(root),
        build_review_contract(root, task, impl_result, IsolatedStage.CODE_QUALITY_REVIEW, prior_review=spec_result),
    )
    code_result = _passed(code, root, "code review passed")
    persist_stage_result(root, code.contract, code_result, ledger=ledger)
    return impl_result, spec_result, code_result


def _typed(root: Path, task: RemediationTask, kind: EvidenceKind, name: str) -> Path:
    source = root / "src" / "service.py"
    digest = "sha256:" + sha256(source.read_bytes()).hexdigest()
    record = TraceEvidence(
        evidence_id=f"{kind.value}-{name}",
        kind=kind,
        status=EvidenceStatus.PASSED,
        subject=task.subject,
        git_commit=_git(root, "rev-parse", "HEAD"),
        bindings=(EvidenceBinding(EvidenceBindingKind.SOURCE, "src/service.py", digest),),
        provenance=(TraceProvenance("src/service.py", 1),),
        producer=EvidenceProducer("test-runner"),
        result={"name": name},
    )
    path = root / "specs" / "changes" / FEATURE / "evidence" / f"{kind.value}-{name}.json"
    _write(path, record.to_json() + "\n")
    return path


def test_trivial_task_completion_requires_current_independent_reviews_and_transitions_ledger(tmp_path: Path) -> None:
    root, ledger, task = _workspace(tmp_path)
    prepare_implementation_dispatch(ledger, task)
    report = evaluate_task_completion(root, ledger, task, attempt=1, risk="trivial")
    assert report.passed is False
    assert {item.dimension for item in report.findings if not item.satisfied} == {
        CompletionDimension.SPEC_REVIEW,
        CompletionDimension.CODE_QUALITY_REVIEW,
    }

    _accepted_task(root, ledger, task)
    event = complete_isolated_task(root, ledger, task, attempt=1, risk="trivial")

    assert event.kind == "task.completed"
    assert ledger.reconstruct().task_map()[task.task_id].status == "completed"
    evidence_events = [item for item in ledger.load_events() if item.kind == "task.evidence"]
    assert {item.payload["dimension"] for item in evidence_events} == {"spec-review", "code-quality-review"}


def test_review_becomes_stale_after_new_commit_and_cannot_complete(tmp_path: Path) -> None:
    root, ledger, task = _workspace(tmp_path)
    _accepted_task(root, ledger, task)
    _write(root / "src" / "service.py", "# Trace: FR-001\nREADY = True\nCHANGED = True\n")
    _git(root, "add", "src/service.py")
    _git(root, "commit", "-m", "change after review")

    report = evaluate_task_completion(root, ledger, task, attempt=1, risk="trivial")
    assert report.passed is False
    assert any(item.status == "stale" for item in report.findings)
    with pytest.raises(CompletionBarrierError, match="task completion blocked"):
        complete_isolated_task(root, ledger, task, attempt=1, risk="trivial")


def test_previous_attempt_cannot_satisfy_reopened_task(tmp_path: Path) -> None:
    root, ledger, task = _workspace(tmp_path)
    _accepted_task(root, ledger, task)
    result_path = next((root / ".sdai" / "isolated" / FEATURE / task.task_id / "attempt-1" / "code-quality-review").glob("*.result.json"))
    binding = ledger.binding_for_file(result_path, kind="evidence")
    ledger.append_event("task.completed", task_id=task.task_id, git_commit=_git(root, "rev-parse", "HEAD"), bindings=(binding,))
    ledger.append_event("task.reopened", task_id=task.task_id)

    report = evaluate_task_completion(root, ledger, task, attempt=1, risk="trivial")
    assert report.passed is False
    assert {item.status for item in report.findings} == {"wrong-attempt"}


def test_standard_task_requires_exact_head_test_and_quality_evidence(tmp_path: Path) -> None:
    root, ledger, task = _workspace(tmp_path)
    _accepted_task(root, ledger, task)
    test_evidence = _typed(root, task, EvidenceKind.TEST, "current")
    quality_evidence = _typed(root, task, EvidenceKind.QUALITY, "current")
    paths = {"test": test_evidence, "quality": quality_evidence}

    report = evaluate_task_completion(root, ledger, task, attempt=1, risk="standard", typed_evidence_paths=paths)
    assert report.passed is True

    _write(root / "src" / "service.py", "# Trace: FR-001\nREADY = False\n")
    stale = evaluate_task_completion(root, ledger, task, attempt=1, risk="standard", typed_evidence_paths=paths)
    assert stale.passed is False
    assert any(item.dimension is CompletionDimension.TEST and item.status == "stale" for item in stale.findings)


def test_wrong_subject_typed_evidence_is_rejected(tmp_path: Path) -> None:
    root, ledger, task = _workspace(tmp_path)
    _accepted_task(root, ledger, task)
    path = _typed(root, task, EvidenceKind.TEST, "subject")
    raw = path.read_text(encoding="utf-8").replace("requirement:FR-001", "requirement:FR-999")
    path.write_text(raw, encoding="utf-8")

    report = evaluate_task_completion(
        root,
        ledger,
        task,
        attempt=1,
        risk="standard",
        typed_evidence_paths={"test": path},
    )
    assert report.passed is False
    assert any(item.dimension is CompletionDimension.TEST and item.status in {"blocked", "wrong-subject"} for item in report.findings)


def test_org_policy_can_strengthen_but_repo_and_user_cannot_weaken(tmp_path: Path) -> None:
    root = tmp_path / "policy"
    (root / ".sdai").mkdir(parents=True)
    org = root / "org.yaml"
    org.write_text(
        "apiVersion: sdai.completion-policy/v1\nrisks:\n  trivial:\n    task: [security]\n",
        encoding="utf-8",
    )
    (root / ".sdai" / "completion-policy.yaml").write_text(
        "apiVersion: sdai.completion-policy/v1\nrisks:\n  trivial:\n    task: []\n",
        encoding="utf-8",
    )
    user = root / "user.yaml"
    user.write_text(
        "apiVersion: sdai.completion-policy/v1\nrisks:\n  trivial:\n    task: []\n",
        encoding="utf-8",
    )

    required = required_dimensions(
        root,
        "trivial",
        CompletionStage.TASK,
        environ={
            "SDAI_ORG_COMPLETION_POLICY_PATH": str(org),
            "SDAI_USER_COMPLETION_POLICY_PATH": str(user),
        },
    )
    assert CompletionDimension.SECURITY in required
    assert CompletionDimension.SPEC_REVIEW in required
    assert CompletionDimension.CODE_QUALITY_REVIEW in required
