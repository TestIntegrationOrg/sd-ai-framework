from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from sdai.agent_platform.definitions import load_agent_definition
from sdai.agent_platform.models import Capability
from sdai.agent_platform.skills import load_skill
from sdai.debug_records import (
    DEBUG_RECORD_API_VERSION,
    DebugRecordError,
    complete_debugger_task,
    persist_debug_record,
    register_debugger_task,
    validate_debug_record,
)
from sdai.evals import MockEvalExecutor, run_behavioral_eval
from sdai.execution_ledger import ExecutionLedgerError, create_execution_run


FEATURE = "DEBUG-100"
RUN_ID = "run-debug"
COMMIT = "a" * 40


def _ledger(tmp_path: Path):
    feature = tmp_path / "specs" / FEATURE
    feature.mkdir(parents=True)
    (feature / "00-intake.md").write_text("# Debug feature café Δ\n", encoding="utf-8")
    return create_execution_run(
        tmp_path,
        FEATURE,
        "debugger-workflow",
        COMMIT,
        run_id=RUN_ID,
    )


def _record(*, provider: str = "codex", status: str = "fixed") -> dict[str, object]:
    root_cause: dict[str, object] | None = {
        "statement": "A stale cache entry is reused after tenant context changes.",
        "evidence_ids": ["OBS_CACHE", "EXP_CACHE"],
        "confidence": "confirmed",
    }
    fix: dict[str, object] | None = {
        "summary": "Key the cache by tenant and invalidate on context change.",
        "files": ["src/service/cache.py"],
    }
    regression: list[dict[str, str]] = [
        {
            "id": "REG_TENANT",
            "command": "pytest -q tests/test_cache.py::test_tenant_switch",
            "result": "1 passed",
            "status": "passed",
        }
    ]
    if status == "investigating":
        root_cause = None
        fix = None
        regression = []
    return {
        "apiVersion": DEBUG_RECORD_API_VERSION,
        "feature_id": FEATURE,
        "run_id": RUN_ID,
        "task_id": "TASK-001",
        "semantic_role": "debugger",
        "status": status,
        "reproduction": {
            "steps": ["Run the tenant-switch request twice with the same cache process."],
            "observed": "Second tenant receives the first tenant's cached result.",
            "expected": "Each tenant receives only its own result.",
        },
        "observations": [
            {
                "id": "OBS_CACHE",
                "fact": "The cache key contains resource id but not tenant id.",
                "source": "debug log at cache lookup boundary",
            }
        ],
        "hypotheses": [
            {
                "id": "HYP_CACHE",
                "statement": "Cache key omission causes cross-tenant reuse.",
                "status": "supported" if status == "fixed" else "open",
                "observation_ids": ["OBS_CACHE"],
            }
        ],
        "experiments": [
            {
                "id": "EXP_CACHE",
                "hypothesis_id": "HYP_CACHE",
                "action": "Include tenant id in an instrumented cache key and rerun reproduction.",
                "result": "Cross-tenant reuse disappears and tenant-specific entries are observed.",
                "conclusion": "supports" if status == "fixed" else "inconclusive",
            }
        ],
        "root_cause": root_cause,
        "fix": fix,
        "regression_evidence": regression,
        "producer": {
            "agent": "debugger",
            "provider": provider,
            "model": "test-model",
        },
    }


def test_debug_record_contract_is_provider_neutral() -> None:
    codex = validate_debug_record(_record(provider="codex"), require_completion=True)
    claude = validate_debug_record(_record(provider="claude"), require_completion=True)

    assert codex["apiVersion"] == claude["apiVersion"] == DEBUG_RECORD_API_VERSION
    assert codex["semantic_role"] == claude["semantic_role"] == "debugger"
    assert codex["producer"]["provider"] == "codex"  # type: ignore[index]
    assert claude["producer"]["provider"] == "claude"  # type: ignore[index]


def test_completion_requires_confirmed_root_cause_fix_and_passing_regression() -> None:
    investigating = _record(status="investigating")
    validate_debug_record(investigating, require_completion=False)

    with pytest.raises(DebugRecordError, match="not completion-ready"):
        validate_debug_record(investigating, require_completion=True)

    failing = _record()
    failing["regression_evidence"][0]["status"] = "failed"  # type: ignore[index]
    with pytest.raises(DebugRecordError, match="regression evidence must pass"):
        validate_debug_record(failing, require_completion=True)


def test_root_cause_references_must_resolve_to_observation_or_experiment() -> None:
    invalid = _record()
    invalid["root_cause"]["evidence_ids"] = ["UNKNOWN_EVIDENCE"]  # type: ignore[index]

    with pytest.raises(DebugRecordError, match="unknown evidence"):
        validate_debug_record(invalid, require_completion=True)


def test_debugger_completion_is_blocked_until_completion_ready_evidence_exists(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    register_debugger_task(ledger, "TASK-001", title="Diagnose cross-tenant cache bug")
    ledger.append_event("task.started", task_id="TASK-001")

    artifact = tmp_path / "specs" / FEATURE / "fix.txt"
    artifact.write_text("fix placeholder\n", encoding="utf-8")
    artifact_binding = ledger.binding_for_file(artifact, kind="artifact")

    with pytest.raises(ExecutionLedgerError, match="SDAI-LEDGER-009"):
        ledger.append_event(
            "task.completed",
            task_id="TASK-001",
            git_commit=COMMIT,
            bindings=(artifact_binding,),
        )

    draft = persist_debug_record(ledger, "TASK-001", _record(status="investigating"))
    assert draft.completion_ready is False
    with pytest.raises(ExecutionLedgerError, match="SDAI-LEDGER-009"):
        ledger.append_event(
            "task.completed",
            task_id="TASK-001",
            git_commit=COMMIT,
            bindings=(artifact_binding, draft.binding),
        )


def test_complete_debugger_task_persists_evidence_then_allows_terminal_claim(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    register_debugger_task(ledger, "TASK-001")
    ledger.append_event("task.started", task_id="TASK-001")

    artifact = tmp_path / "specs" / FEATURE / "src" / "service" / "cache.py"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("cache key includes tenant\n", encoding="utf-8")
    artifact_binding = ledger.binding_for_file(artifact, kind="artifact")

    evidence = complete_debugger_task(
        ledger,
        "TASK-001",
        COMMIT,
        _record(),
        artifact_bindings=(artifact_binding,),
    )

    state = ledger.reconstruct().task_map()["TASK-001"]
    assert state.status == "completed"
    assert evidence.completion_ready is True
    assert evidence.binding in state.bindings
    document = json.loads(
        ledger.task_record_paths("TASK-001")["evidence"].read_text(encoding="utf-8")
    )
    assert document["payload"]["evidence_contract"] == DEBUG_RECORD_API_VERSION
    assert document["payload"]["debug_record"]["root_cause"]["confidence"] == "confirmed"
    kinds = [event.kind for event in ledger.load_events()]
    assert kinds.index("task.evidence") < kinds.index("task.completed")


def test_evidence_from_previous_attempt_does_not_satisfy_reopened_debugger_task(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    register_debugger_task(ledger, "TASK-001")
    ledger.append_event("task.started", task_id="TASK-001")
    first = persist_debug_record(ledger, "TASK-001", _record(), require_completion=True)
    ledger.append_event(
        "task.failed",
        task_id="TASK-001",
        bindings=(first.binding,),
        payload={"reason": "fix experiment interrupted"},
    )
    ledger.append_event("task.reopened", task_id="TASK-001")
    ledger.append_event("task.started", task_id="TASK-001")

    artifact = tmp_path / "specs" / FEATURE / "attempt-two.txt"
    artifact.write_text("attempt two\n", encoding="utf-8")
    binding = ledger.binding_for_file(artifact, kind="artifact")
    with pytest.raises(ExecutionLedgerError, match="SDAI-LEDGER-009"):
        ledger.append_event(
            "task.completed",
            task_id="TASK-001",
            git_commit=COMMIT,
            bindings=(binding, first.binding),
        )


def test_debugger_agent_and_skill_are_provider_neutral_and_behaviorally_evaluated() -> None:
    root = Path(__file__).resolve().parents[1]
    agent = load_agent_definition(root, "debugger")
    skill = load_skill(root, "systematic-debugging")

    assert agent.name == "debugger"
    assert agent.providers == {}
    assert agent.capabilities == (Capability.CODING, Capability.TESTING, Capability.REVIEW)
    assert "systematic-debugging" in agent.skills
    assert skill.name == "systematic-debugging"

    skill_report = run_behavioral_eval(
        root,
        "skill",
        "systematic-debugging",
        executor=MockEvalExecutor(),
        require_improvement=True,
    )
    agent_report = run_behavioral_eval(
        root,
        "agent",
        "debugger",
        executor=MockEvalExecutor(),
        require_improvement=True,
    )
    assert skill_report.passed is True
    assert skill_report.delta > 0
    assert agent_report.passed is True
    assert agent_report.delta > 0
