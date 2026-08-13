from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from sdai.agent_platform import AgentRuntime
from sdai.agent_platform.models import AgentExecutionResult
from sdai.convergence import RemediationKind, RemediationTask
from sdai.execution_ledger import create_execution_run
from sdai.isolated_tasks import (
    IsolatedStage,
    IsolatedStageResult,
    IsolatedStageStatus,
    IsolatedTaskError,
    assert_task_individually_accepted,
    build_final_change_review_contract,
    build_implementation_contract,
    build_isolated_invocation,
    build_review_contract,
    execute_isolated_invocation,
    load_persisted_contract,
    persist_stage_result,
    prepare_implementation_dispatch,
    task_review_chain,
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


FEATURE = "ISOLATED-121"
RUN_ID = "run-isolated-121"


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
    root = tmp_path / "isolated Ω workspace"
    root.mkdir()
    init_project(root)
    install_v05_scaffold(root)
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "SDAI Isolated Test")
    _git(root, "config", "user.email", "sdai@example.test")

    feature = root / "specs" / "changes" / FEATURE
    _write(
        feature / "requirements.md",
        """# Requirements

- FR-001: Signing must preserve café and Δ behavior.
- AC-001: Given a valid script, signing succeeds.
""",
    )
    _write(
        feature / "hidden-context.md",
        "DO-NOT-INHERIT-HIDDEN-FEATURE-CONTEXT\n",
    )
    _write(
        root / "src" / "signing" / "service.py",
        "# Trace: FR-001 AC-001\nSIGNED = False\n",
    )
    _write(
        root / "tests" / "test_signing.py",
        "# Trace: FR-001 AC-001\ndef test_signing():\n    assert True\n",
    )
    _write(root / "specs" / FEATURE / "00-intake.md", "# Legacy execution anchor\n")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "isolated baseline")
    baseline = _git(root, "rev-parse", "HEAD")
    ledger = create_execution_run(root, FEATURE, "enterprise", baseline, run_id=RUN_ID)
    task = RemediationTask(
        task_id="REMEDIATE-1210abcd1210abcd",
        feature_id=FEATURE,
        round_id="ROUND-1210abcd1210abcd",
        verification_report_sha256="sha256:" + "1" * 64,
        verification_input_sha256="sha256:" + "2" * 64,
        finding_sha256="sha256:" + "3" * 64,
        finding_code="SDAI_VERIFY_TRACE_GAP",
        finding_source=VerificationFindingSource.DETERMINISTIC,
        category=VerificationCategory.TRACE_COVERAGE,
        severity=VerificationSeverity.BLOCKING,
        status=VerificationStatus.FAIL,
        subject="requirement:FR-001",
        summary="Implement current signing behavior and tests for FR-001 without changing requirements.",
        remediation_kind=RemediationKind.IMPLEMENTATION,
        allowed_roots=(
            "src",
            "tests",
            f"specs/changes/{FEATURE}/plan.md",
            f"specs/changes/{FEATURE}/tasks.md",
            f"specs/changes/{FEATURE}/tests.md",
        ),
        forbidden_roots=(
            f"specs/changes/{FEATURE}/requirements.md",
            "specs/current",
        ),
        provenance=(
            TraceProvenance(
                f"specs/changes/{FEATURE}/requirements.md",
                3,
                detail="FR-001 remediation source",
            ),
        ),
    )
    return root, baseline, ledger, task


def _passed_result(prepared, output: str) -> IsolatedStageResult:
    return IsolatedStageResult(
        invocation=prepared.record,
        status=IsolatedStageStatus.PASSED,
        git_commit=_git(prepared.invocation.cwd, "rev-parse", "HEAD"),
        output=output,
    )


def test_implementation_contract_is_minimal_durable_and_does_not_inherit_feature_context(
    tmp_path: Path,
) -> None:
    root, _, ledger, task = _workspace(tmp_path)

    dispatch = prepare_implementation_dispatch(ledger, task)
    contract = build_implementation_contract(root, task, dispatch)
    prepared = build_isolated_invocation(AgentRuntime(root), contract)
    again = build_isolated_invocation(AgentRuntime(root), contract)

    assert contract.stage is IsolatedStage.IMPLEMENT
    assert contract.semantic_agent == "developer"
    assert contract.attempt == 1
    assert contract.dispatch_id == dispatch.dispatch_id
    assert len(contract.context) == 1
    assert contract.context[0].source.endswith("requirements.md")
    assert "FR-001" in contract.context[0].text
    assert "DO-NOT-INHERIT-HIDDEN-FEATURE-CONTEXT" not in prepared.invocation.prompt
    assert "DO-NOT-INHERIT-HIDDEN-FEATURE-CONTEXT" not in prepared.invocation.system
    assert contract.sha256 in prepared.invocation.prompt
    assert "no chat history is inherited" in prepared.invocation.prompt
    assert prepared.record.invocation_id == again.record.invocation_id
    persisted = load_persisted_contract(root, FEATURE, task.task_id, 1, IsolatedStage.IMPLEMENT)
    assert persisted == contract


def test_runtime_explicit_context_path_does_not_scan_normal_feature_artifacts(tmp_path: Path) -> None:
    root, _, _, _ = _workspace(tmp_path)
    runtime = AgentRuntime(root)

    isolated = runtime.build_explicit_context_invocation(
        FEATURE,
        capability=runtime.build_invocation(FEATURE, capability=__import__("sdai.agent_platform", fromlist=["Capability"]).Capability.CODING).capability,
        explicit_context="ONLY-THIS-CONTEXT café Δ",
        agent_name="developer",
        mode=__import__("sdai.agent_platform", fromlist=["ExecutionMode"]).ExecutionMode.ADVISORY,
    )

    assert "ONLY-THIS-CONTEXT café Δ" in isolated.prompt
    assert "DO-NOT-INHERIT-HIDDEN-FEATURE-CONTEXT" not in isolated.prompt


def test_execute_isolated_invocation_uses_prebuilt_fresh_invocation_without_rebuilding_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _, ledger, task = _workspace(tmp_path)
    dispatch = prepare_implementation_dispatch(ledger, task)
    contract = build_implementation_contract(root, task, dispatch)
    runtime = AgentRuntime(root)
    prepared = build_isolated_invocation(runtime, contract)
    seen = []

    def fake_execute(invocation):
        seen.append(invocation)
        return AgentExecutionResult(
            feature_id=invocation.feature_id,
            capability=invocation.capability,
            profile=invocation.profile.name,
            provider=invocation.profile.provider,
            output="Implemented only the isolated task.",
            prompt=invocation.prompt,
            skills=(),
            agent_name=invocation.agent_name,
        )

    monkeypatch.setattr(runtime, "execute_invocation", fake_execute)
    result = execute_isolated_invocation(runtime, prepared, status=IsolatedStageStatus.PASSED)

    assert seen == [prepared.invocation]
    assert result.invocation.invocation_id == prepared.record.invocation_id
    assert result.status is IsolatedStageStatus.PASSED
    assert result.output == "Implemented only the isolated task."


def test_stage_result_is_ledger_backed_and_persistence_is_idempotent(tmp_path: Path) -> None:
    root, _, ledger, task = _workspace(tmp_path)
    dispatch = prepare_implementation_dispatch(ledger, task)
    prepared = build_isolated_invocation(AgentRuntime(root), build_implementation_contract(root, task, dispatch))
    result = _passed_result(prepared, "implementation output")

    first_path = persist_stage_result(root, prepared.contract, result, ledger=ledger)
    first_count = len(ledger.load_events())
    second_path = persist_stage_result(root, prepared.contract, result, ledger=ledger)

    assert first_path == second_path
    assert first_path.is_file()
    assert len(ledger.load_events()) == first_count
    events = ledger.load_events()
    assert any(event.kind == "task.started" and event.task_id == task.task_id for event in events)
    implementation = [event for event in events if event.kind == "task.implementation"]
    assert len(implementation) == 1
    assert implementation[0].payload["invocation_id"] == prepared.record.invocation_id
    assert implementation[0].payload["contract_sha256"] == prepared.contract.sha256


def test_started_interruption_reuses_dispatch_and_exact_persisted_contract(tmp_path: Path) -> None:
    root, _, ledger, task = _workspace(tmp_path)
    first_dispatch = prepare_implementation_dispatch(ledger, task)
    first_contract = build_implementation_contract(root, task, first_dispatch)
    ledger.append_event(
        "task.started",
        task_id=task.task_id,
        git_commit=first_contract.git_commit,
        payload={"dispatch_id": first_dispatch.dispatch_id, "attempt": 1},
    )

    # Simulate partial implementation work. This is outside the bound requirements
    # context, so resume must reuse the durable task context rather than rescan the
    # repository and accidentally inherit the partial worker state as new instructions.
    _write(root / "src" / "signing" / "service.py", "# partial interrupted work\nSIGNED = True\n")
    second_dispatch = prepare_implementation_dispatch(ledger, task)
    second_contract = build_implementation_contract(root, task, second_dispatch)

    assert second_dispatch.reused is True
    assert second_dispatch.dispatch_id == first_dispatch.dispatch_id
    assert second_contract == first_contract
    assert second_contract.sha256 == first_contract.sha256


def test_spec_and_code_reviews_are_independent_fresh_invocations_in_strict_order(tmp_path: Path) -> None:
    root, _, ledger, task = _workspace(tmp_path)
    dispatch = prepare_implementation_dispatch(ledger, task)
    implementation_contract = build_implementation_contract(root, task, dispatch)
    implementation = build_isolated_invocation(AgentRuntime(root), implementation_contract)
    implementation_result = _passed_result(implementation, "worker output")
    persist_stage_result(root, implementation_contract, implementation_result, ledger=ledger)

    with pytest.raises(IsolatedTaskError, match="requires prior spec-compliance"):
        build_review_contract(
            root,
            task,
            implementation_result,
            IsolatedStage.CODE_QUALITY_REVIEW,
        )

    spec_contract = build_review_contract(
        root,
        task,
        implementation_result,
        IsolatedStage.SPEC_COMPLIANCE_REVIEW,
    )
    spec_prepared = build_isolated_invocation(AgentRuntime(root), spec_contract)
    spec_result = _passed_result(spec_prepared, "spec compliance passed")
    persist_stage_result(root, spec_contract, spec_result, ledger=ledger)

    code_contract = build_review_contract(
        root,
        task,
        implementation_result,
        IsolatedStage.CODE_QUALITY_REVIEW,
        prior_review=spec_result,
    )
    code_prepared = build_isolated_invocation(AgentRuntime(root), code_contract)
    code_result = _passed_result(code_prepared, "code quality passed")
    persist_stage_result(root, code_contract, code_result, ledger=ledger)

    assert implementation.record.semantic_agent == "developer"
    assert spec_prepared.record.semantic_agent == "code-reviewer"
    assert code_prepared.record.semantic_agent == "code-reviewer"
    assert len(
        {
            implementation.record.invocation_id,
            spec_prepared.record.invocation_id,
            code_prepared.record.invocation_id,
        }
    ) == 3
    assert spec_contract.worker_invocation_id == implementation.record.invocation_id
    assert code_contract.worker_invocation_id == implementation.record.invocation_id
    assert code_contract.predecessor_invocation_ids == (spec_prepared.record.invocation_id,)
    assert implementation.record.invocation_id not in code_contract.predecessor_invocation_ids
    chain = task_review_chain(root, FEATURE, task.task_id, 1)
    assert_task_individually_accepted(chain)
    reviews = [event for event in ledger.load_events() if event.kind == "task.review"]
    assert [event.payload["stage"] for event in reviews] == [
        "spec-compliance-review",
        "code-quality-review",
    ]


def test_failed_spec_review_blocks_code_quality_review(tmp_path: Path) -> None:
    root, _, ledger, task = _workspace(tmp_path)
    dispatch = prepare_implementation_dispatch(ledger, task)
    implementation = build_isolated_invocation(AgentRuntime(root), build_implementation_contract(root, task, dispatch))
    implementation_result = _passed_result(implementation, "worker output")
    persist_stage_result(root, implementation.contract, implementation_result, ledger=ledger)
    spec = build_isolated_invocation(
        AgentRuntime(root),
        build_review_contract(root, task, implementation_result, IsolatedStage.SPEC_COMPLIANCE_REVIEW),
    )
    failed_spec = IsolatedStageResult(
        spec.record,
        IsolatedStageStatus.FAILED,
        _git(root, "rev-parse", "HEAD"),
        "Requirement mismatch found.",
    )
    persist_stage_result(root, spec.contract, failed_spec, ledger=ledger)

    with pytest.raises(IsolatedTaskError, match="requires passing spec-compliance"):
        build_review_contract(
            root,
            task,
            implementation_result,
            IsolatedStage.CODE_QUALITY_REVIEW,
            prior_review=failed_spec,
        )


def test_final_whole_change_review_requires_every_task_individually_accepted(tmp_path: Path) -> None:
    root, baseline, ledger, task = _workspace(tmp_path)
    dispatch = prepare_implementation_dispatch(ledger, task)
    implementation = build_isolated_invocation(AgentRuntime(root), build_implementation_contract(root, task, dispatch))
    impl_result = _passed_result(implementation, "worker output")
    persist_stage_result(root, implementation.contract, impl_result, ledger=ledger)

    with pytest.raises(IsolatedTaskError, match="not individually accepted"):
        build_final_change_review_contract(
            root,
            FEATURE,
            {task.task_id: (impl_result,)},
            baseline_commit=baseline,
        )

    spec = build_isolated_invocation(
        AgentRuntime(root),
        build_review_contract(root, task, impl_result, IsolatedStage.SPEC_COMPLIANCE_REVIEW),
    )
    spec_result = _passed_result(spec, "spec passed")
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
    code_result = _passed_result(code, "code passed")
    persist_stage_result(root, code.contract, code_result, ledger=ledger)

    final_contract = build_final_change_review_contract(
        root,
        FEATURE,
        {task.task_id: (impl_result, spec_result, code_result)},
        baseline_commit=baseline,
    )
    final_prepared = build_isolated_invocation(AgentRuntime(root), final_contract)

    assert final_contract.stage is IsolatedStage.FINAL_CHANGE_REVIEW
    assert final_contract.semantic_agent == "code-reviewer"
    assert final_contract.worker_invocation_id == implementation.record.invocation_id
    assert set(final_contract.predecessor_invocation_ids) == {
        spec.record.invocation_id,
        code.record.invocation_id,
    }
    assert final_prepared.record.invocation_id not in {
        implementation.record.invocation_id,
        spec.record.invocation_id,
        code.record.invocation_id,
    }
    assert final_contract.context[0].source.endswith("final-change.diff")


def test_contract_and_results_are_machine_clean_and_utf8_portable(tmp_path: Path) -> None:
    root, _, ledger, task = _workspace(tmp_path)
    dispatch = prepare_implementation_dispatch(ledger, task)
    contract = build_implementation_contract(root, task, dispatch)
    prepared = build_isolated_invocation(AgentRuntime(root), contract)
    result = _passed_result(prepared, "reviewed café Δ output")
    path = persist_stage_result(root, contract, result, ledger=ledger)

    contract_payload = json.loads(contract.to_json())
    result_payload = json.loads(path.read_text(encoding="utf-8"))
    assert contract_payload["apiVersion"] == "sdai.isolated-task/v1"
    assert result_payload["apiVersion"] == "sdai.isolated-result/v1"
    assert result_payload["invocation"]["apiVersion"] == "sdai.isolated-invocation/v1"
    assert "café" in result_payload["output"]
    assert "\\" not in contract.to_json()
