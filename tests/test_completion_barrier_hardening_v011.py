from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import subprocess

import pytest

from sdai.agent_platform import AgentRuntime
from sdai.completion_barrier import complete_isolated_task
from sdai.completion_change_checks import final_review_finding
from sdai.completion_policy import (
    CompletionPolicyError,
    CompletionStage,
    required_dimensions,
    resolve_completion_risk,
)
from sdai.convergence import (
    ConvergenceState,
    ConvergenceStatus,
    RemediationKind,
    RemediationTask,
    convergence_state_path,
)
from sdai.execution_ledger import create_execution_run
from sdai.isolated_final_review import prepare_final_change_review_contract
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
from sdai.trace_graph import TraceProvenance
from sdai.v05_scaffold import install_v05_scaffold
from sdai.verification import (
    VerificationCategory,
    VerificationFindingSource,
    VerificationSeverity,
    VerificationStatus,
)


FEATURE = "COMPLETE-HARDEN-122"


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
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
    return completed.stdout.strip()


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")
    return path


def _workspace(tmp_path: Path):
    root = tmp_path / "completion hardening Ω"
    root.mkdir()
    init_project(root)
    install_v05_scaffold(root)
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "SDAI Completion Hardening")
    _git(root, "config", "user.email", "sdai@example.test")
    _write(
        root / "specs" / "changes" / FEATURE / "requirements.md",
        "# Requirements\n\n- FR-001: Preserve café Δ behavior.\n",
    )
    _write(root / "src" / "service.py", "# Trace: FR-001\nREADY = True\n")
    _write(
        root / "tests" / "test_service.py",
        "# Trace: FR-001\ndef test_ready():\n    assert True\n",
    )
    _write(root / "specs" / FEATURE / "00-intake.md", "# execution anchor\n")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "baseline")
    baseline = _git(root, "rev-parse", "HEAD")
    ledger = create_execution_run(
        root,
        FEATURE,
        "enterprise",
        baseline,
        run_id="run-complete-harden-122",
    )
    task = RemediationTask(
        task_id="REMEDIATE-acde1220beef1220",
        feature_id=FEATURE,
        round_id="ROUND-acde1220beef1220",
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
    return root, baseline, ledger, task


def _passed(prepared, root: Path, output: str) -> IsolatedStageResult:
    return IsolatedStageResult(
        prepared.record,
        IsolatedStageStatus.PASSED,
        _git(root, "rev-parse", "HEAD"),
        output,
    )


def _accepted_task(root: Path, ledger, task: RemediationTask):
    dispatch = prepare_implementation_dispatch(ledger, task)
    implementation = build_isolated_invocation(
        AgentRuntime(root),
        build_implementation_contract(root, task, dispatch),
    )
    impl_result = _passed(implementation, root, "implementation passed")
    persist_stage_result(root, implementation.contract, impl_result, ledger=ledger)
    spec = build_isolated_invocation(
        AgentRuntime(root),
        build_review_contract(root, task, impl_result, IsolatedStage.SPEC_COMPLIANCE_REVIEW),
    )
    spec_result = _passed(spec, root, "spec passed")
    persist_stage_result(root, spec.contract, spec_result, ledger=ledger)
    code = build_isolated_invocation(
        AgentRuntime(root),
        build_review_contract(
            root,
            task,
            impl_result,
            IsolatedStage.CODE_QUALITY_REVIEW,
            prior_review=spec_result,
        ),
    )
    code_result = _passed(code, root, "code passed")
    persist_stage_result(root, code.contract, code_result, ledger=ledger)
    return impl_result, spec_result, code_result


def _final_review(root: Path, baseline: str, chains, *, attempt: int, status=IsolatedStageStatus.PASSED):
    contract = prepare_final_change_review_contract(
        root,
        FEATURE,
        chains,
        baseline_commit=baseline,
        attempt=attempt,
    )
    prepared = build_isolated_invocation(AgentRuntime(root), contract)
    result = IsolatedStageResult(
        prepared.record,
        status,
        _git(root, "rev-parse", "HEAD"),
        f"final review attempt {attempt}",
    )
    persist_stage_result(root, contract, result)
    return result


def _persist_convergence_risk(root: Path, risk: str) -> None:
    state = ConvergenceState(
        feature_id=FEATURE,
        risk=risk,
        max_rounds=3,
        status=ConvergenceStatus.VERIFIED,
        escalation_reason=None,
        current_verification_report_sha256="sha256:" + "4" * 64,
        current_verification_input_sha256="sha256:" + "5" * 64,
        rounds=(),
        tasks=(),
    )
    path = convergence_state_path(root, FEATURE)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(state.to_json() + "\n", encoding="utf-8", newline="\n")


def test_durable_convergence_risk_cannot_be_downgraded_by_completion_caller(tmp_path: Path) -> None:
    root, _, _, _ = _workspace(tmp_path)
    _persist_convergence_risk(root, "critical")

    assert resolve_completion_risk(root, FEATURE) == "critical"
    with pytest.raises(CompletionPolicyError, match="does not match durable convergence risk"):
        resolve_completion_risk(root, FEATURE, "trivial")


def test_external_policy_symlink_is_rejected_before_resolution(tmp_path: Path) -> None:
    root, _, _, _ = _workspace(tmp_path)
    target = root / "real-policy.yaml"
    target.write_text(
        "apiVersion: sdai.completion-policy/v1\nrisks:\n  trivial:\n    task: []\n",
        encoding="utf-8",
    )
    link = root / "policy-link.yaml"
    try:
        link.symlink_to(target.name)
    except OSError:
        pytest.skip("symlink creation is unavailable on this platform")

    with pytest.raises(CompletionPolicyError, match="non-symlink"):
        required_dimensions(
            root,
            "trivial",
            CompletionStage.TASK,
            environ={"SDAI_ORG_COMPLETION_POLICY_PATH": str(link)},
        )


def test_successful_task_completion_retry_returns_same_terminal_event(tmp_path: Path) -> None:
    root, _, ledger, task = _workspace(tmp_path)
    _accepted_task(root, ledger, task)

    first = complete_isolated_task(root, ledger, task, attempt=1, risk="trivial")
    event_count = len(ledger.load_events())
    second = complete_isolated_task(root, ledger, task, attempt=1, risk="trivial")

    assert second.event_id == first.event_id
    assert second.sha256 == first.sha256
    assert len(ledger.load_events()) == event_count


def test_latest_failed_final_review_supersedes_older_passed_attempt(tmp_path: Path) -> None:
    root, baseline, ledger, task = _workspace(tmp_path)
    chain = _accepted_task(root, ledger, task)
    complete_isolated_task(root, ledger, task, attempt=1, risk="trivial")
    _final_review(root, baseline, {task.task_id: chain}, attempt=1)
    _final_review(
        root,
        baseline,
        {task.task_id: chain},
        attempt=2,
        status=IsolatedStageStatus.FAILED,
    )
    head = _git(root, "rev-parse", "HEAD")

    old = final_review_finding(root, ledger, head=head, final_attempt=1)
    current = final_review_finding(root, ledger, head=head)

    assert old.status == "wrong-attempt"
    assert current.status == "failed"


def test_final_review_is_invalidated_when_new_current_task_is_added(tmp_path: Path) -> None:
    root, baseline, ledger, task = _workspace(tmp_path)
    chain = _accepted_task(root, ledger, task)
    complete_isolated_task(root, ledger, task, attempt=1, risk="trivial")
    _final_review(root, baseline, {task.task_id: chain}, attempt=1)

    second = replace(
        task,
        task_id="REMEDIATE-acde1220beef1221",
        finding_sha256="sha256:" + "6" * 64,
        summary="Second independent remediation task.",
    )
    second_chain = _accepted_task(root, ledger, second)
    complete_isolated_task(root, ledger, second, attempt=1, risk="trivial")
    assert len(second_chain) == 3

    finding = final_review_finding(
        root,
        ledger,
        head=_git(root, "rev-parse", "HEAD"),
    )

    assert finding.status == "wrong-subject"
    assert "current ledger task/attempt set" in finding.reason
