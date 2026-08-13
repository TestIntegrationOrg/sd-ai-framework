from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import subprocess

import pytest

from sdai.agent_platform import AgentRuntime
from sdai.convergence import RemediationKind, RemediationTask
from sdai.execution_ledger import create_execution_run
from sdai.isolated_context_state import prepare_independent_review_contract
from sdai.isolated_execution import validate_isolated_context_current
from sdai.isolated_final_review import prepare_final_change_review_contract
from sdai.isolated_tasks import (
    IsolatedStage,
    IsolatedStageResult,
    IsolatedStageStatus,
    IsolatedTaskError,
    build_implementation_contract,
    build_isolated_invocation,
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


FEATURE = "ISOLATED-HARDEN-121"


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


def _task() -> RemediationTask:
    return RemediationTask(
        task_id="REMEDIATE-acde1210beef1210",
        feature_id=FEATURE,
        round_id="ROUND-acde1210beef1210",
        verification_report_sha256="sha256:" + "a" * 64,
        verification_input_sha256="sha256:" + "b" * 64,
        finding_sha256="sha256:" + "c" * 64,
        finding_code="SDAI_VERIFY_TRACE_GAP",
        finding_source=VerificationFindingSource.DETERMINISTIC,
        category=VerificationCategory.TRACE_COVERAGE,
        severity=VerificationSeverity.BLOCKING,
        status=VerificationStatus.FAIL,
        subject="requirement:FR-001",
        summary="Implement current signing behavior without changing requirement truth.",
        remediation_kind=RemediationKind.IMPLEMENTATION,
        allowed_roots=("src", "tests"),
        forbidden_roots=(f"specs/changes/{FEATURE}/requirements.md", "specs/current"),
        provenance=(TraceProvenance(f"specs/changes/{FEATURE}/requirements.md", 3),),
    )


def _workspace(
    tmp_path: Path,
    *,
    requirement: str = "Sign café scripts with Δ behavior.",
    max_context_chars: int | None = None,
):
    root = tmp_path / "isolated hardening Ω"
    root.mkdir()
    init_project(root)
    install_v05_scaffold(root)
    if max_context_chars is not None:
        config = root / ".sdai" / "config.yaml"
        text = config.read_text(encoding="utf-8")
        config.write_text(
            text.replace(
                "max_context_chars_per_file: 30000",
                f"max_context_chars_per_file: {max_context_chars}",
            ),
            encoding="utf-8",
            newline="\n",
        )
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "SDAI Hardening Test")
    _git(root, "config", "user.email", "sdai@example.test")
    _write(
        root / "specs" / "changes" / FEATURE / "requirements.md",
        f"# Requirements\n\n- FR-001: {requirement}\n",
    )
    _write(root / "src" / "service.py", "SIGNED = False\n")
    _write(root / "specs" / FEATURE / "00-intake.md", "# execution anchor\n")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "baseline")
    baseline = _git(root, "rev-parse", "HEAD")
    ledger = create_execution_run(
        root,
        FEATURE,
        "enterprise",
        baseline,
        run_id="run-isolated-harden-121",
    )
    return root, baseline, ledger, _task()


def _implementation(root: Path, ledger, task: RemediationTask) -> IsolatedStageResult:
    dispatch = prepare_implementation_dispatch(ledger, task)
    contract = build_implementation_contract(root, task, dispatch)
    prepared = build_isolated_invocation(AgentRuntime(root), contract)
    result = IsolatedStageResult(
        invocation=prepared.record,
        status=IsolatedStageStatus.PASSED,
        git_commit=_git(root, "rev-parse", "HEAD"),
        output="implemented signing task",
    )
    persist_stage_result(root, contract, result, ledger=ledger)
    return result


def _accepted_chain(root: Path, task: RemediationTask, implementation: IsolatedStageResult):
    spec_contract = prepare_independent_review_contract(
        root,
        task,
        implementation,
        IsolatedStage.SPEC_COMPLIANCE_REVIEW,
        attempt=1,
    )
    spec_prepared = build_isolated_invocation(AgentRuntime(root), spec_contract)
    spec_result = IsolatedStageResult(
        spec_prepared.record,
        IsolatedStageStatus.PASSED,
        _git(root, "rev-parse", "HEAD"),
        "spec review passed",
    )
    persist_stage_result(root, spec_contract, spec_result)
    code_contract = prepare_independent_review_contract(
        root,
        task,
        implementation,
        IsolatedStage.CODE_QUALITY_REVIEW,
        attempt=1,
        prior_spec_review=spec_result,
    )
    code_prepared = build_isolated_invocation(AgentRuntime(root), code_contract)
    code_result = IsolatedStageResult(
        code_prepared.record,
        IsolatedStageStatus.PASSED,
        _git(root, "rev-parse", "HEAD"),
        "code quality passed",
    )
    persist_stage_result(root, code_contract, code_result)
    return implementation, spec_result, code_result


def test_review_snapshot_includes_untracked_worker_files_and_detects_later_change(tmp_path: Path) -> None:
    root, _, ledger, task = _workspace(tmp_path)
    implementation = _implementation(root, ledger, task)
    _write(root / "src" / "service.py", "SIGNED = True\n")
    _write(root / "tests" / "test_new_signing.py", "def test_new():\n    assert True\n")

    contract = prepare_independent_review_contract(
        root,
        task,
        implementation,
        IsolatedStage.SPEC_COMPLIANCE_REVIEW,
        attempt=1,
    )
    snapshot = next(item for item in contract.context if item.source.endswith("workspace.diff"))

    assert "SDAI-UNTRACKED-TEXT tests/test_new_signing.py" in snapshot.text
    assert "def test_new" in snapshot.text
    validate_isolated_context_current(root, contract)

    _write(root / "tests" / "test_new_signing.py", "def test_new():\n    assert False\n")
    with pytest.raises(IsolatedTaskError, match="review workspace is stale"):
        validate_isolated_context_current(root, contract)


def test_code_review_rejects_foreign_or_tampered_spec_review_result(tmp_path: Path) -> None:
    root, _, ledger, task = _workspace(tmp_path)
    implementation = _implementation(root, ledger, task)
    _write(root / "src" / "service.py", "SIGNED = True\n")
    spec_contract = prepare_independent_review_contract(
        root,
        task,
        implementation,
        IsolatedStage.SPEC_COMPLIANCE_REVIEW,
        attempt=1,
    )
    spec_prepared = build_isolated_invocation(AgentRuntime(root), spec_contract)
    spec_result = IsolatedStageResult(
        spec_prepared.record,
        IsolatedStageStatus.PASSED,
        _git(root, "rev-parse", "HEAD"),
        "spec review passed",
    )
    persist_stage_result(root, spec_contract, spec_result)

    foreign_record = replace(
        spec_result.invocation,
        contract_sha256="sha256:" + "d" * 64,
    )
    foreign_result = IsolatedStageResult(
        foreign_record,
        IsolatedStageStatus.PASSED,
        spec_result.git_commit,
        spec_result.output,
    )
    with pytest.raises(IsolatedTaskError, match="does not belong to the current task/attempt/worker"):
        prepare_independent_review_contract(
            root,
            task,
            implementation,
            IsolatedStage.CODE_QUALITY_REVIEW,
            attempt=1,
            prior_spec_review=foreign_result,
        )


def test_final_review_snapshot_fails_closed_when_workspace_changes_after_preparation(tmp_path: Path) -> None:
    root, baseline, ledger, task = _workspace(tmp_path)
    implementation = _implementation(root, ledger, task)
    _write(root / "src" / "service.py", "SIGNED = True\n")
    chain = _accepted_chain(root, task, implementation)
    final_contract = prepare_final_change_review_contract(
        root,
        FEATURE,
        {task.task_id: chain},
        baseline_commit=baseline,
    )

    validate_isolated_context_current(root, final_contract)
    _write(root / "tests" / "late_untracked_test.py", "assert False\n")
    with pytest.raises(IsolatedTaskError, match="review workspace is stale"):
        validate_isolated_context_current(root, final_contract)


def test_contract_persistence_rejects_prompt_larger_than_runtime_limit(tmp_path: Path) -> None:
    root, _, ledger, task = _workspace(
        tmp_path,
        requirement="X" * 1600,
        max_context_chars=1000,
    )
    dispatch = prepare_implementation_dispatch(ledger, task)

    with pytest.raises(IsolatedTaskError, match="prompt context exceeds configured runtime limit"):
        build_implementation_contract(root, task, dispatch)


def test_execute_invocation_rechecks_prompt_safety_for_prebuilt_invocation(tmp_path: Path) -> None:
    root, _, _, _ = _workspace(tmp_path)
    runtime = AgentRuntime(root)
    invocation = runtime.build_explicit_context_invocation(
        FEATURE,
        __import__("sdai.agent_platform", fromlist=["Capability"]).Capability.REVIEW,
        "safe review context",
        agent_name="code-reviewer",
    )
    unsafe = replace(
        invocation,
        prompt=invocation.prompt + "\nAKIA1234567890ABCDEF",
    )

    with pytest.raises(RuntimeError, match="AWS_ACCESS_KEY"):
        runtime.execute_invocation(unsafe)
