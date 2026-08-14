from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest
import yaml

from sdai.execution_ledger import create_execution_run, load_execution_run
from sdai.workflow_execution import (
    WorkflowExecutionStatus,
    WorkflowLeafInvocation,
    WorkflowLeafOutcome,
    execute_workflow_graph,
)
from sdai.workflow_graph import load_workflow_graph


FEATURE = "WF2-EXEC-100"
BASELINE = "a" * 40


def _workflow(
    root: Path,
    steps: list[object],
    *,
    inputs: dict[str, object] | None = None,
) -> None:
    path = root / ".sdai" / "workflows" / "engine2.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {
        "version": 9,
        "name": "engine2",
        "validation_mode": "standard",
        "steps": steps,
    }
    if inputs is not None:
        payload["inputs"] = inputs
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def _ledger(root: Path, *, run_id: str = "wf2-run"):
    feature = root / "specs" / FEATURE
    feature.mkdir(parents=True, exist_ok=True)
    (feature / "00-intake.md").write_text("# Workflow execution\n", encoding="utf-8")
    return create_execution_run(root, FEATURE, "engine2", BASELINE, run_id=run_id)


def _success(values: dict[str, object], calls: Counter[str] | None = None):
    def execute(invocation: WorkflowLeafInvocation) -> WorkflowLeafOutcome:
        if calls is not None:
            calls[invocation.node.id] += 1
        return WorkflowLeafOutcome(
            WorkflowExecutionStatus.SUCCEEDED,
            values.get(invocation.node.id, invocation.node.id),
        )

    return execute


def _record(result, path: str):
    return next(item for item in result.nodes if item.path == path)


def test_sequence_if_switch_and_fan_in_have_deterministic_order(tmp_path: Path) -> None:
    _workflow(
        tmp_path,
        [
            {"id": "a", "type": "deterministic", "action": "a"},
            {
                "id": "decision",
                "type": "if",
                "condition": True,
                "then": [{"id": "b", "type": "deterministic", "action": "b"}],
                "else": [{"id": "c", "type": "deterministic", "action": "c"}],
            },
            {
                "id": "route",
                "type": "switch",
                "value": "blue",
                "cases": [
                    {"when": "blue", "steps": [{"id": "d", "type": "deterministic", "action": "d"}]}
                ],
                "default": [{"id": "e", "type": "deterministic", "action": "e"}],
            },
            {"id": "join", "type": "fan-in", "sources": ["a", "b", "d"], "strategy": "collect"},
        ],
    )
    resolution = load_workflow_graph(tmp_path, "engine2")
    ledger = _ledger(tmp_path)

    result = execute_workflow_graph(
        resolution,
        ledger,
        leaf_executor=_success({"a": "A", "b": "B", "d": "D"}),
    )

    assert result.status == WorkflowExecutionStatus.SUCCEEDED
    assert _record(result, "join").output == ["A", "B", "D"]
    assert not any(item.path.endswith("/c") or item.path.endswith("/e") for item in result.nodes)
    assert ledger.reconstruct().status == "completed"

    repeated = execute_workflow_graph(
        resolution,
        load_execution_run(tmp_path, FEATURE, "wf2-run"),
        leaf_executor=lambda _: pytest.fail("completed leaves must not execute again"),
    )
    assert repeated.as_dict() == result.as_dict()


def test_crash_resume_reuses_dispatch_and_does_not_repeat_completed_side_effect(
    tmp_path: Path,
) -> None:
    _workflow(
        tmp_path,
        [
            {"id": "one", "type": "deterministic", "action": "one"},
            {"id": "two", "type": "deterministic", "action": "two"},
        ],
    )
    resolution = load_workflow_graph(tmp_path, "engine2")
    ledger = _ledger(tmp_path)
    calls: Counter[str] = Counter()
    dispatches: list[str] = []

    def crashing(invocation: WorkflowLeafInvocation) -> WorkflowLeafOutcome:
        calls[invocation.node.id] += 1
        if invocation.node.id == "two":
            dispatches.append(invocation.dispatch_id)
            if calls["two"] == 1:
                raise RuntimeError("simulated process loss")
        return WorkflowLeafOutcome(WorkflowExecutionStatus.SUCCEEDED, invocation.node.id)

    with pytest.raises(RuntimeError, match="simulated process loss"):
        execute_workflow_graph(resolution, ledger, leaf_executor=crashing)

    resumed = execute_workflow_graph(
        resolution,
        load_execution_run(tmp_path, FEATURE, "wf2-run"),
        leaf_executor=crashing,
    )

    assert resumed.status == WorkflowExecutionStatus.SUCCEEDED
    assert calls == Counter({"two": 2, "one": 1})
    assert dispatches[0] == dispatches[1]


def test_partial_parallel_completion_resumes_only_uncommitted_branch(tmp_path: Path) -> None:
    _workflow(
        tmp_path,
        [
            {
                "id": "parallel",
                "type": "parallel",
                "max_concurrency": 2,
                "steps": [
                    {"id": "left", "type": "deterministic", "action": "left"},
                    {"id": "right", "type": "deterministic", "action": "right"},
                ],
            }
        ],
    )
    resolution = load_workflow_graph(tmp_path, "engine2")
    ledger = _ledger(tmp_path)
    calls: Counter[str] = Counter()

    def executor(invocation: WorkflowLeafInvocation) -> WorkflowLeafOutcome:
        calls[invocation.node.id] += 1
        if invocation.node.id == "right" and calls["right"] == 1:
            raise RuntimeError("parallel crash")
        return WorkflowLeafOutcome(WorkflowExecutionStatus.SUCCEEDED, invocation.node.id.upper())

    with pytest.raises(RuntimeError):
        execute_workflow_graph(resolution, ledger, leaf_executor=executor)
    result = execute_workflow_graph(resolution, ledger, leaf_executor=executor)

    assert result.status == WorkflowExecutionStatus.SUCCEEDED
    assert calls == Counter({"right": 2, "left": 1})
    assert _record(result, "parallel").output == ["LEFT", "RIGHT"]


def test_fan_out_item_identity_and_aggregation_follow_input_order(tmp_path: Path) -> None:
    _workflow(
        tmp_path,
        [
            {
                "id": "fan",
                "type": "fan-out",
                "items": {"literal": ["β", "alpha", "β"]},
                "as": "target",
                "max_items": 4,
                "max_concurrency": 2,
                "steps": [{"id": "inspect", "type": "validate"}],
            }
        ],
    )
    resolution = load_workflow_graph(tmp_path, "engine2")
    ledger = _ledger(tmp_path)
    identities: list[str] = []

    def executor(invocation: WorkflowLeafInvocation) -> WorkflowLeafOutcome:
        identities.append(invocation.execution_identity)
        return WorkflowLeafOutcome(
            WorkflowExecutionStatus.SUCCEEDED,
            invocation.context["item"]["target"],  # type: ignore[index]
        )

    result = execute_workflow_graph(resolution, ledger, leaf_executor=executor)

    assert _record(result, "fan").output == [["β"], ["alpha"], ["β"]]
    assert len(set(identities)) == 3
    assert ":item-0000-" in identities[0]
    assert ":item-0001-" in identities[1]


def test_retry_attempts_are_durable_and_success_is_evidence_bound(tmp_path: Path) -> None:
    _workflow(
        tmp_path,
        [
            {
                "id": "flaky",
                "type": "deterministic",
                "action": "flaky",
                "retry": {"max_attempts": 3, "delay_seconds": 0},
            }
        ],
    )
    resolution = load_workflow_graph(tmp_path, "engine2")
    ledger = _ledger(tmp_path)
    attempts: list[int] = []

    def executor(invocation: WorkflowLeafInvocation) -> WorkflowLeafOutcome:
        attempts.append(invocation.attempt)
        if invocation.attempt < 3:
            return WorkflowLeafOutcome(WorkflowExecutionStatus.FAILED, error="transient")
        return WorkflowLeafOutcome(WorkflowExecutionStatus.SUCCEEDED, "recovered")

    result = execute_workflow_graph(resolution, ledger, leaf_executor=executor)

    assert result.status == WorkflowExecutionStatus.SUCCEEDED
    assert attempts == [1, 2, 3]
    task = next(item for item in ledger.reconstruct().tasks if item.task_id.startswith("wf-"))
    assert task.status == "completed"
    assert task.bindings and task.bindings[0].kind == "evidence"


def test_bounded_loop_terminates_and_nonterminating_loop_fails_at_bound(tmp_path: Path) -> None:
    _workflow(
        tmp_path,
        [
            {
                "id": "loop",
                "type": "bounded-while",
                "condition": {"lt": [{"ref": "loop.iteration"}, 2]},
                "max_iterations": 3,
                "steps": [{"id": "body", "type": "validate"}],
            }
        ],
    )
    resolution = load_workflow_graph(tmp_path, "engine2")
    result = execute_workflow_graph(resolution, _ledger(tmp_path), leaf_executor=_success({"body": "ok"}))
    assert result.status == WorkflowExecutionStatus.SUCCEEDED
    assert _record(result, "loop").output == [["ok"], ["ok"]]

    other = tmp_path / "nonterminating"
    _workflow(
        other,
        [
            {
                "id": "loop",
                "type": "bounded-while",
                "condition": True,
                "max_iterations": 2,
                "steps": [{"id": "body", "type": "validate"}],
            }
        ],
    )
    failed = execute_workflow_graph(
        load_workflow_graph(other, "engine2"),
        _ledger(other),
        leaf_executor=_success({"body": "ok"}),
    )
    assert failed.status == WorkflowExecutionStatus.FAILED
    assert "maxIterations" in str(failed.error)


def test_approval_pause_resume_rechecks_decision_without_new_state_store(tmp_path: Path) -> None:
    _workflow(tmp_path, [{"id": "approve", "type": "approval", "gate": "release"}])
    resolution = load_workflow_graph(tmp_path, "engine2")
    ledger = _ledger(tmp_path)
    decisions = iter((False, True))
    dispatches: list[str] = []

    def executor(invocation: WorkflowLeafInvocation) -> WorkflowLeafOutcome:
        dispatches.append(invocation.dispatch_id)
        if not next(decisions):
            return WorkflowLeafOutcome(WorkflowExecutionStatus.PAUSED, error="approval pending")
        return WorkflowLeafOutcome(WorkflowExecutionStatus.SUCCEEDED, {"approved": True})

    paused = execute_workflow_graph(resolution, ledger, leaf_executor=executor)
    assert paused.status == WorkflowExecutionStatus.PAUSED
    assert ledger.reconstruct().status == "paused"

    resumed = execute_workflow_graph(resolution, ledger, leaf_executor=executor)
    assert resumed.status == WorkflowExecutionStatus.SUCCEEDED
    assert dispatches[0] == dispatches[1]
    assert ledger.reconstruct().status == "completed"
    checkpoint = ledger.load_checkpoint()
    assert checkpoint is not None
    assert checkpoint["extra"]["workflowEngine2"]["status"] == "succeeded"  # type: ignore[index]


def test_changed_input_invalidates_paused_task_and_cancellation_is_terminal(tmp_path: Path) -> None:
    _workflow(
        tmp_path,
        [{"id": "approve", "type": "approval", "gate": "release"}],
        inputs={"channel": {"type": "string", "required": True}},
    )
    first = load_workflow_graph(tmp_path, "engine2", input_values={"channel": "dev"})
    ledger = _ledger(tmp_path)
    paused = execute_workflow_graph(
        first,
        ledger,
        leaf_executor=lambda _: WorkflowLeafOutcome(WorkflowExecutionStatus.PAUSED, error="pending"),
    )
    assert paused.status == WorkflowExecutionStatus.PAUSED

    changed = load_workflow_graph(tmp_path, "engine2", input_values={"channel": "prod"})
    completed = execute_workflow_graph(
        changed,
        ledger,
        leaf_executor=lambda _: WorkflowLeafOutcome(WorkflowExecutionStatus.SUCCEEDED, "approved-prod"),
    )
    assert completed.status == WorkflowExecutionStatus.SUCCEEDED
    assert all(task.status == "completed" for task in ledger.reconstruct().tasks)
    assert len(ledger.reconstruct().tasks) == 2

    cancelled_root = tmp_path / "cancelled"
    _workflow(cancelled_root, [{"id": "work", "type": "deterministic", "action": "work"}])
    cancelled_ledger = _ledger(cancelled_root)
    cancelled = execute_workflow_graph(
        load_workflow_graph(cancelled_root, "engine2"),
        cancelled_ledger,
        cancelled=lambda: True,
    )
    assert cancelled.status == WorkflowExecutionStatus.CANCELLED
    assert cancelled_ledger.reconstruct().status == "cancelled"


def test_runtime_rejects_workspace_write_inside_parallel_and_safe_command_is_graph_leaf(
    tmp_path: Path,
) -> None:
    _workflow(
        tmp_path,
        [
            {
                "id": "parallel",
                "type": "parallel",
                "max_concurrency": 2,
                "steps": [
                    {
                        "id": "writer",
                        "type": "agent",
                        "agent": "developer",
                        "capability": "coding",
                        "mode": "workspace-write",
                    },
                    {"id": "reader", "type": "validate"},
                ],
            }
        ],
    )
    failed = execute_workflow_graph(
        load_workflow_graph(tmp_path, "engine2"),
        _ledger(tmp_path),
        leaf_executor=_success({}),
    )
    assert failed.status == WorkflowExecutionStatus.FAILED
    assert "workspace-write branches" in str(failed.error)

    safe_root = tmp_path / "safe-command"
    _workflow(
        safe_root,
        [
            {
                "id": "inspect",
                "type": "safe-command",
                "executable": "python",
                "args_before_input": ["-c", "print('ok')"],
                "workspace_write": False,
            }
        ],
    )
    safe = load_workflow_graph(safe_root, "engine2")
    assert safe.graph.node("inspect").kind == "safe-command"
    assert safe.graph.node("inspect").config["requiresWorkspaceWrite"] is False
