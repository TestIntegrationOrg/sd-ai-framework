from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

from sdai.agent_platform import AgentRuntime
from sdai.agent_platform.models import AgentExecutionResult
from sdai.convergence import RemediationKind, RemediationTask
from sdai.execution_ledger import create_execution_run
from sdai.isolated_execution import (
    IsolatedWriteViolation,
    execute_isolated_stage,
    validate_isolated_context_current,
)
from sdai.isolated_tasks import (
    IsolatedStageStatus,
    IsolatedTaskError,
    build_implementation_contract,
    build_isolated_invocation,
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


FEATURE = "ISOLATED-EXEC-121"


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


def _setup(tmp_path: Path):
    root = tmp_path / "isolated guard Ω"
    root.mkdir()
    init_project(root)
    install_v05_scaffold(root)
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "SDAI Guard Test")
    _git(root, "config", "user.email", "sdai@example.test")
    _write(
        root / "specs" / "changes" / FEATURE / "requirements.md",
        "# Requirements\n\n- FR-001: Preserve café Δ behavior.\n",
    )
    _write(root / "src" / "service.py", "VALUE = 1\n")
    _write(root / "README-task.md", "must remain unchanged\n")
    _write(root / "specs" / FEATURE / "00-intake.md", "# execution anchor\n")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "baseline")
    baseline = _git(root, "rev-parse", "HEAD")
    ledger = create_execution_run(root, FEATURE, "enterprise", baseline, run_id="run-guard-121")
    task = RemediationTask(
        task_id="REMEDIATE-1210feed1210feed",
        feature_id=FEATURE,
        round_id="ROUND-1210feed1210feed",
        verification_report_sha256="sha256:" + "4" * 64,
        verification_input_sha256="sha256:" + "5" * 64,
        finding_sha256="sha256:" + "6" * 64,
        finding_code="SDAI_VERIFY_TRACE_GAP",
        finding_source=VerificationFindingSource.DETERMINISTIC,
        category=VerificationCategory.TRACE_COVERAGE,
        severity=VerificationSeverity.BLOCKING,
        status=VerificationStatus.FAIL,
        subject="requirement:FR-001",
        summary="Implement FR-001 only under src/tests.",
        remediation_kind=RemediationKind.IMPLEMENTATION,
        allowed_roots=("src", "tests"),
        forbidden_roots=(f"specs/changes/{FEATURE}/requirements.md", "specs/current"),
        provenance=(
            TraceProvenance(f"specs/changes/{FEATURE}/requirements.md", 3),
        ),
    )
    dispatch = prepare_implementation_dispatch(ledger, task)
    contract = build_implementation_contract(root, task, dispatch)
    prepared = build_isolated_invocation(AgentRuntime(root), contract)
    return root, prepared


def test_context_freshness_fails_closed_when_bound_requirement_bytes_change(tmp_path: Path) -> None:
    root, prepared = _setup(tmp_path)
    validate_isolated_context_current(root, prepared.contract)
    _write(
        root / "specs" / "changes" / FEATURE / "requirements.md",
        "# Requirements\n\n- FR-001: Requirement changed after task dispatch.\n",
    )

    with pytest.raises(IsolatedTaskError, match="context is stale"):
        validate_isolated_context_current(root, prepared.contract)


def test_safe_execution_allows_only_task_allowlist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, prepared = _setup(tmp_path)
    runtime = AgentRuntime(root)

    def fake_execute(invocation):
        _write(root / "src" / "service.py", "VALUE = 2  # café Δ\n")
        return AgentExecutionResult(
            feature_id=invocation.feature_id,
            capability=invocation.capability,
            profile=invocation.profile.name,
            provider=invocation.profile.provider,
            output="allowed source update",
            prompt=invocation.prompt,
            skills=(),
            agent_name=invocation.agent_name,
        )

    monkeypatch.setattr(runtime, "execute_invocation", fake_execute)
    result = execute_isolated_stage(
        runtime,
        prepared,
        status=IsolatedStageStatus.PASSED,
    )

    assert result.status is IsolatedStageStatus.PASSED
    assert (root / "src" / "service.py").read_text(encoding="utf-8") == "VALUE = 2  # café Δ\n"
    assert (root / "README-task.md").read_text(encoding="utf-8") == "must remain unchanged\n"


def test_unauthorized_write_rolls_back_entire_worker_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, prepared = _setup(tmp_path)
    runtime = AgentRuntime(root)
    original_source = (root / "src" / "service.py").read_bytes()
    original_readme = (root / "README-task.md").read_bytes()

    def fake_execute(invocation):
        _write(root / "src" / "service.py", "VALUE = 999\n")
        _write(root / "README-task.md", "unauthorized worker edit\n")
        _write(root / "outside-new.txt", "new unauthorized file\n")
        return AgentExecutionResult(
            feature_id=invocation.feature_id,
            capability=invocation.capability,
            profile=invocation.profile.name,
            provider=invocation.profile.provider,
            output="bad worker",
            prompt=invocation.prompt,
            skills=(),
            agent_name=invocation.agent_name,
        )

    monkeypatch.setattr(runtime, "execute_invocation", fake_execute)
    with pytest.raises(IsolatedWriteViolation, match="entire invocation was rolled back"):
        execute_isolated_stage(runtime, prepared)

    assert (root / "src" / "service.py").read_bytes() == original_source
    assert (root / "README-task.md").read_bytes() == original_readme
    assert not (root / "outside-new.txt").exists()


def test_forbidden_requirement_write_is_rolled_back_even_if_task_allowlist_is_broadened(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, prepared = _setup(tmp_path)
    runtime = AgentRuntime(root)
    requirement = root / "specs" / "changes" / FEATURE / "requirements.md"
    original = requirement.read_bytes()

    def fake_execute(invocation):
        _write(requirement, "# rewritten to match implementation\n")
        return AgentExecutionResult(
            feature_id=invocation.feature_id,
            capability=invocation.capability,
            profile=invocation.profile.name,
            provider=invocation.profile.provider,
            output="attempted spec rewrite",
            prompt=invocation.prompt,
            skills=(),
            agent_name=invocation.agent_name,
        )

    monkeypatch.setattr(runtime, "execute_invocation", fake_execute)
    with pytest.raises(IsolatedWriteViolation):
        execute_isolated_stage(runtime, prepared)

    assert requirement.read_bytes() == original
