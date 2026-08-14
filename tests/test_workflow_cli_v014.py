from __future__ import annotations

from pathlib import Path
import json

import pytest
import yaml

from sdai.entrypoint import main as sdai_main
from sdai.execution_ledger import create_execution_run
from sdai.workflow_execution import (
    WorkflowExecutionStatus,
    WorkflowLeafInvocation,
    WorkflowLeafOutcome,
    execute_workflow_graph,
)
from sdai.workflow_graph import load_workflow_graph
from sdai.workflow_machine import inspect_workflow_run


FEATURE = "WF2-CLI-100"
BASELINE = "b" * 40


def _init(root: Path) -> None:
    config = root / ".sdai" / "config.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("version: 1\noperating_mode: individual\n", encoding="utf-8")


def _workflow(
    root: Path,
    steps: list[object],
    *,
    inputs: dict[str, object] | None = None,
    name: str = "engine2",
) -> None:
    path = root / ".sdai" / "workflows" / f"{name}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {
        "version": 9,
        "name": name,
        "validation_mode": "standard",
        "steps": steps,
    }
    if inputs is not None:
        payload["inputs"] = inputs
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def _ledger(root: Path, *, run_id: str):
    feature = root / "specs" / FEATURE
    feature.mkdir(parents=True, exist_ok=True)
    (feature / "00-intake.md").write_text("# CLI journey\n", encoding="utf-8")
    return create_execution_run(root, FEATURE, "engine2", BASELINE, run_id=run_id)


def _json(capsys: pytest.CaptureFixture[str]) -> tuple[dict[str, object], str]:
    output = capsys.readouterr().out
    return json.loads(output), output


def test_graph_resolve_validate_are_canonical_repeatable_and_redact_sensitive_inputs(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _init(tmp_path)
    _workflow(
        tmp_path,
        [
            {
                "id": "route",
                "type": "if",
                "description": "route Ω",
                "condition": {"eq": [{"ref": "inputs.channel"}, "prod"]},
                "then": [{"id": "review", "type": "validate", "description": "café"}],
                "else": [{"id": "fallback", "type": "validate"}],
            }
        ],
        inputs={
            "channel": {"type": "string", "default": "prod"},
            "token": {"type": "string", "required": True, "sensitive": True},
        },
    )
    common = ["--input", "token=super-secret", "--json", "--path", str(tmp_path)]

    assert sdai_main(["workflow", "graph", "engine2", *common]) == 0
    graph, graph_text = _json(capsys)
    assert graph["apiVersion"] == "sdai.workflow-graph/v2"
    assert str(graph["graphSha256"]).startswith("sha256:")
    assert "route Ω" in graph_text and "café" in graph_text
    assert "super-secret" not in graph_text

    assert sdai_main(["workflow", "resolve", "engine2", *common]) == 0
    resolution, first = _json(capsys)
    assert resolution["apiVersion"] == "sdai.workflow-resolution/v2"
    assert resolution["resolvedInputs"]["token"]["sensitive"] is True  # type: ignore[index]
    assert "super-secret" not in first
    assert sdai_main(["workflow", "resolve", "engine2", *common]) == 0
    _, repeated = _json(capsys)
    assert repeated == first

    assert sdai_main(["workflow", "validate", "engine2", *common]) == 0
    validation, validation_text = _json(capsys)
    assert validation["apiVersion"] == "sdai.workflow-validation/v2"
    assert validation["status"] == "valid"
    assert str(validation["validationSha256"]).startswith("sha256:")
    assert "super-secret" not in validation_text


def test_resolve_emits_safe_command_plan_and_effective_permissions(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _init(tmp_path)
    _workflow(
        tmp_path,
        [
            {
                "id": "inspect",
                "type": "safe-command",
                "executable": "python",
                "args_before_input": ["-c", "print('ok')"],
                "environment": ["CI"],
                "workspace_write": False,
            }
        ],
    )

    assert sdai_main(["workflow", "resolve", "engine2", "--json", "--path", str(tmp_path)]) == 0
    payload, _ = _json(capsys)
    plan = payload["stepPlans"][0]  # type: ignore[index]
    assert plan["kind"] == "safe-command"
    assert plan["permissions"] == {
        "environmentNames": ["CI"],
        "network": False,
        "policySources": [],
        "workspaceWrite": False,
    }
    assert str(plan["planSha256"]).startswith("sha256:")


def test_legacy_explain_keeps_shape_but_redacts_top_level_sensitive_input(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _init(tmp_path)
    path = tmp_path / ".sdai" / "workflows" / "legacy.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(
            {
                "version": 6,
                "name": "legacy",
                "validation_mode": "standard",
                "inputs": {
                    "token": {"type": "string", "required": True, "sensitive": True}
                },
                "steps": [{"id": "check", "type": "validate"}],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    assert (
        sdai_main(
            [
                "workflow",
                "explain",
                "legacy",
                "--input",
                "token=legacy-secret",
                "--json",
                "--path",
                str(tmp_path),
            ]
        )
        == 0
    )
    payload, output = _json(capsys)
    assert payload["version"] == 1
    assert payload["resolved_inputs"]["token"]["sensitive"] is True  # type: ignore[index]
    assert "legacy-secret" not in output


def test_status_and_resume_expose_next_work_without_leaf_outputs(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _init(tmp_path)
    _workflow(tmp_path, [{"id": "approve", "type": "approval", "gate": "release"}])
    resolution = load_workflow_graph(tmp_path, "engine2")
    ledger = _ledger(tmp_path, run_id="paused-run")
    paused = execute_workflow_graph(
        resolution,
        ledger,
        leaf_executor=lambda _: WorkflowLeafOutcome(
            WorkflowExecutionStatus.PAUSED,
            error="approval pending",
        ),
    )
    assert paused.status == WorkflowExecutionStatus.PAUSED

    command = [
        "workflow",
        "status",
        FEATURE,
        "--run",
        "paused-run",
        "--json",
        "--path",
        str(tmp_path),
    ]
    assert sdai_main(command) == 2
    status, first = _json(capsys)
    assert status["apiVersion"] == "sdai.workflow-run-status/v2"
    assert status["status"] == "paused"
    assert status["nextWork"]["action"] == "re-evaluate"  # type: ignore[index]
    assert status["nextWork"]["nodePath"] == "approve"  # type: ignore[index]
    assert sdai_main(command) == 2
    _, repeated = _json(capsys)
    assert repeated == first

    resume = [
        "workflow",
        "resume",
        FEATURE,
        "--run",
        "paused-run",
        "--json",
        "--path",
        str(tmp_path),
    ]
    assert sdai_main(resume) == 2
    result, output = _json(capsys)
    assert result["apiVersion"] == "sdai.workflow-resume-result/v2"
    assert result["status"] == "paused"
    assert "approval pending" not in output


def test_completed_status_redacts_output_and_deterministic_resume_completes(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _init(tmp_path)
    _workflow(
        tmp_path,
        [
            {
                "id": "targets",
                "type": "fan-out",
                "items": {"literal": ["api"]},
                "as": "target",
                "max_items": 2,
                "max_concurrency": 1,
                "steps": [{"id": "work", "type": "validate"}],
            }
        ],
    )
    resolution = load_workflow_graph(tmp_path, "engine2")
    completed_ledger = _ledger(tmp_path, run_id="completed-run")
    execute_workflow_graph(
        resolution,
        completed_ledger,
        leaf_executor=lambda _: WorkflowLeafOutcome(
            WorkflowExecutionStatus.SUCCEEDED,
            {"token": "leaf-secret"},
        ),
    )
    assert (
        sdai_main(
            [
                "workflow",
                "status",
                FEATURE,
                "--run",
                "completed-run",
                "--json",
                "--path",
                str(tmp_path),
            ]
        )
        == 0
    )
    status, output = _json(capsys)
    assert status["status"] == "succeeded"
    leaf = next(item for item in status["nodes"] if item["taskId"] is not None)  # type: ignore[union-attr]
    assert leaf["outputSha256"].startswith("sha256:")
    assert leaf["scope"]["items"][0]["index"] == 0
    assert "leaf-secret" not in output
    assert "output" not in leaf

    active = _ledger(tmp_path, run_id="active-run")
    assert active.reconstruct().status == "active"
    active_status = inspect_workflow_run(tmp_path, FEATURE, "active-run")
    assert active_status.body["nextWork"] == {
        "action": "plan",
        "taskId": None,
        "executionIdentity": "$root",
        "nodePath": "$root",
        "scope": {"branches": [], "items": [], "iterations": []},
    }
    assert (
        sdai_main(
            [
                "workflow",
                "resume",
                FEATURE,
                "--run",
                "active-run",
                "--json",
                "--path",
                str(tmp_path),
            ]
        )
        == 0
    )
    resumed, _ = _json(capsys)
    assert resumed["status"] == "succeeded"
    assert resumed["run"]["nextWork"] is None  # type: ignore[index]


def test_stale_checkpoint_invalid_input_and_missing_run_have_stable_exit_codes(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _init(tmp_path)
    _workflow(
        tmp_path,
        [
            {"id": "one", "type": "deterministic", "action": "one"},
            {"id": "two", "type": "deterministic", "action": "two"},
        ],
    )
    ledger = _ledger(tmp_path, run_id="crashed-run")
    calls = 0

    def crash_second(invocation: WorkflowLeafInvocation) -> WorkflowLeafOutcome:
        nonlocal calls
        calls += 1
        if invocation.node.id == "two":
            raise RuntimeError("process loss")
        return WorkflowLeafOutcome(WorkflowExecutionStatus.SUCCEEDED, "done")

    with pytest.raises(RuntimeError, match="process loss"):
        execute_workflow_graph(
            load_workflow_graph(tmp_path, "engine2"),
            ledger,
            leaf_executor=crash_second,
        )
    stale = inspect_workflow_run(tmp_path, FEATURE, "crashed-run")
    assert stale.exit_code == 4
    assert stale.body["checkpointStatus"] == "stale"

    assert (
        sdai_main(
            [
                "workflow",
                "status",
                FEATURE,
                "--run",
                "missing-run",
                "--json",
                "--path",
                str(tmp_path),
            ]
        )
        == 3
    )
    missing, _ = _json(capsys)
    assert missing["category"] == "not-found"

    assert (
        sdai_main(
            [
                "workflow",
                "resolve",
                "engine2",
                "--input",
                "broken",
                "--json",
                "--path",
                str(tmp_path),
            ]
        )
        == 4
    )
    invalid, _ = _json(capsys)
    assert invalid["category"] == "invalid-unsafe"
