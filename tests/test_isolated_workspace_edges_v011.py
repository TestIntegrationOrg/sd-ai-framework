from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

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
    build_review_contract,
    persist_stage_result,
    prepare_implementation_dispatch,
)
from sdai.isolated_workspace import IsolatedWorkspaceError, render_workspace_snapshot
from sdai.scaffold import init_project
from sdai.trace_graph import TraceProvenance
from sdai.v05_scaffold import install_v05_scaffold
from sdai.verification import (
    VerificationCategory,
    VerificationFindingSource,
    VerificationSeverity,
    VerificationStatus,
)


FEATURE = "ISOLATED-EDGE-121"


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


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")


def _workspace(tmp_path: Path):
    root = tmp_path / "workspace edge Ω"
    root.mkdir()
    init_project(root)
    install_v05_scaffold(root)
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "SDAI Edge Test")
    _git(root, "config", "user.email", "sdai@example.test")
    _write(
        root / "specs" / "changes" / FEATURE / "requirements.md",
        "# Requirements\n\n- FR-001: Sign café scripts.\n",
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
        run_id="run-isolated-edge-121",
    )
    task = RemediationTask(
        task_id="REMEDIATE-edge1210edge1210",
        feature_id=FEATURE,
        round_id="ROUND-edge1210edge1210",
        verification_report_sha256="sha256:" + "1" * 64,
        verification_input_sha256="sha256:" + "2" * 64,
        finding_sha256="sha256:" + "3" * 64,
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
    contract = build_implementation_contract(root, task, dispatch)
    prepared = build_isolated_invocation(AgentRuntime(root), contract)
    implementation = IsolatedStageResult(
        prepared.record,
        IsolatedStageStatus.PASSED,
        baseline,
        "implementation passed",
    )
    persist_stage_result(root, contract, implementation, ledger=ledger)
    return root, baseline, task, implementation


def test_nul_containing_valid_utf8_untracked_file_is_binary_metadata(tmp_path: Path) -> None:
    root, _, task, implementation = _workspace(tmp_path)
    target = root / "tests" / "nul-valid-utf8.bin"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"\x00ABC")

    contract = prepare_independent_review_contract(
        root,
        task,
        implementation,
        IsolatedStage.SPEC_COMPLIANCE_REVIEW,
        attempt=1,
    )
    snapshot = next(item for item in contract.context if item.source.endswith("workspace.diff"))

    assert "SDAI-UNTRACKED-BINARY tests/nul-valid-utf8.bin" in snapshot.text
    assert "\x00" not in snapshot.text


def test_large_untracked_text_is_bounded_during_streaming(tmp_path: Path) -> None:
    root, baseline, _, _ = _workspace(tmp_path)
    target = root / "tests" / "large-worker-output.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("x" * 200_000, encoding="utf-8")

    with pytest.raises(IsolatedWorkspaceError, match="snapshot budget"):
        render_workspace_snapshot(root, baseline, max_chars=1_000)


def test_legacy_review_contract_keeps_file_freshness_compatibility(tmp_path: Path) -> None:
    root, _, task, implementation = _workspace(tmp_path)
    legacy = build_review_contract(
        root,
        task,
        implementation,
        IsolatedStage.SPEC_COMPLIANCE_REVIEW,
    )

    assert not any(item.source.endswith("workspace.diff") for item in legacy.context)
    validate_isolated_context_current(root, legacy)
