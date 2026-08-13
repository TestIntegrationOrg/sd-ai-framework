from __future__ import annotations

from pathlib import Path
import subprocess

from sdai.agent_platform import AgentRuntime
from sdai.convergence import RemediationKind, RemediationTask
from sdai.execution_ledger import create_execution_run
from sdai.isolated_context_state import prepare_independent_review_contract
from sdai.isolated_execution import validate_isolated_context_current
from sdai.isolated_tasks import (
    IsolatedStage,
    IsolatedStageResult,
    IsolatedStageStatus,
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


FEATURE = "ISOLATED-REVIEW-121"


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return completed.stdout.strip()


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")
    return path


def _setup(tmp_path: Path):
    root = tmp_path / "review context Ω"
    root.mkdir()
    init_project(root)
    install_v05_scaffold(root)
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "SDAI Review Context Test")
    _git(root, "config", "user.email", "sdai@example.test")
    _write(
        root / "specs" / "changes" / FEATURE / "requirements.md",
        "# Requirements\n\n- FR-001: Sign café scripts with Δ behavior.\n",
    )
    _write(root / "src" / "service.py", "SIGNED = False\n")
    _write(root / "specs" / FEATURE / "00-intake.md", "# execution anchor\n")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "baseline")
    baseline = _git(root, "rev-parse", "HEAD")
    ledger = create_execution_run(root, FEATURE, "enterprise", baseline, run_id="run-review-context")
    task = RemediationTask(
        task_id="REMEDIATE-acde1210acde1210",
        feature_id=FEATURE,
        round_id="ROUND-acde1210acde1210",
        verification_report_sha256="sha256:" + "7" * 64,
        verification_input_sha256="sha256:" + "8" * 64,
        finding_sha256="sha256:" + "9" * 64,
        finding_code="SDAI_VERIFY_TRACE_GAP",
        finding_source=VerificationFindingSource.DETERMINISTIC,
        category=VerificationCategory.TRACE_COVERAGE,
        severity=VerificationSeverity.BLOCKING,
        status=VerificationStatus.FAIL,
        subject="requirement:FR-001",
        summary="Implement signing without changing requirement truth.",
        remediation_kind=RemediationKind.IMPLEMENTATION,
        allowed_roots=("src", "tests"),
        forbidden_roots=(f"specs/changes/{FEATURE}/requirements.md", "specs/current"),
        provenance=(TraceProvenance(f"specs/changes/{FEATURE}/requirements.md", 3),),
    )
    dispatch = prepare_implementation_dispatch(ledger, task)
    implementation_contract = build_implementation_contract(root, task, dispatch)
    implementation = build_isolated_invocation(AgentRuntime(root), implementation_contract)
    result = IsolatedStageResult(
        invocation=implementation.record,
        status=IsolatedStageStatus.PASSED,
        git_commit=baseline,
        output="Changed src/service.py to set SIGNED=True and kept requirement truth untouched.",
    )
    persist_stage_result(root, implementation_contract, result, ledger=ledger)
    return root, task, result


def test_spec_review_receives_worker_output_and_current_workspace_diff(tmp_path: Path) -> None:
    root, task, implementation = _setup(tmp_path)
    _write(root / "src" / "service.py", "SIGNED = True  # café Δ\n")

    contract = prepare_independent_review_contract(
        root,
        task,
        implementation,
        IsolatedStage.SPEC_COMPLIANCE_REVIEW,
        attempt=1,
    )
    prepared = build_isolated_invocation(AgentRuntime(root), contract)

    sources = {item.source for item in contract.context}
    assert any(source.endswith("worker-output.txt") for source in sources)
    assert any(source.endswith("workspace.diff") for source in sources)
    assert "Changed src/service.py" in prepared.invocation.prompt
    assert "SIGNED = True" in prepared.invocation.prompt
    assert "FR-001" in prepared.invocation.prompt
    validate_isolated_context_current(root, contract)
    for item in contract.context:
        if item.source.startswith(f".sdai/isolated/{FEATURE}/"):
            assert (root / item.source).is_file()


def test_code_quality_review_receives_prior_spec_review_without_chat_history(tmp_path: Path) -> None:
    root, task, implementation = _setup(tmp_path)
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
        invocation=spec_prepared.record,
        status=IsolatedStageStatus.PASSED,
        git_commit=_git(root, "rev-parse", "HEAD"),
        output="Spec review passed: implementation matches FR-001.",
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

    assert spec_prepared.record.invocation_id in code_contract.predecessor_invocation_ids
    assert "Spec review passed" in code_prepared.invocation.prompt
    assert "this is the complete task context" in code_prepared.invocation.prompt
    assert code_prepared.record.invocation_id != spec_prepared.record.invocation_id
    assert code_prepared.record.invocation_id != implementation.invocation.invocation_id
