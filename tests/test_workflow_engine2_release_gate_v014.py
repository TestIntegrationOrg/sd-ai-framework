from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
import sys

import pytest
import yaml

from sdai.entrypoint import main as sdai_main
from sdai.execution_ledger import create_execution_run, load_execution_run
from sdai.plugin_steps import (
    PluginExecutorRegistry,
    PluginResult,
    PluginStepError,
    prepare_plugin_step,
)
from sdai.policy import load_effective_configuration
from sdai.workflow_execution import (
    WorkflowExecutionStatus,
    WorkflowLeafInvocation,
    WorkflowLeafOutcome,
    execute_workflow_graph,
)
from sdai.workflow_graph import WorkflowGraphError, load_workflow_graph
from sdai.workflow_machine import inspect_workflow_run, resume_workflow_run
from sdai.workflow_operational_steps import (
    WorkflowOperationalStepError,
    build_workflow_leaf_plan,
    execute_safe_command_leaf,
    normalize_workflow_operational_step,
)
from sdai.workflow_plugin_execution import WorkflowPluginLeafExecutor


FEATURE = "WF2-RELEASE-100"
BASELINE = "d" * 40
SECRET = "literal; echo HACKED && $(touch never) | café Δ"


def _write_yaml(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
        newline="\n",
    )


def _init_project(root: Path) -> None:
    _write_yaml(
        root / ".sdai" / "config.yaml",
        {"version": 1, "operating_mode": "individual"},
    )
    _write_yaml(
        root / ".sdai" / "plugin-policy.yaml",
        {
            "version": 1,
            "allowed_plugins": ["release-evidence"],
            "trusted_publishers": ["release-gate"],
            "permissions": {
                "filesystem": {"read": [], "write": []},
                "network": False,
                "environment": [],
                "commands": [],
                "workspace_write": False,
            },
        },
    )
    _write_yaml(
        root / ".sdai" / "plugin-steps" / "release-evidence.yaml",
        {
            "apiVersion": "sdai/v1",
            "kind": "PluginStep",
            "metadata": {"id": "release-evidence", "version": "1.0.0"},
            "spec": {
                "publisher": "release-gate",
                "executor": "release-evidence",
                "permissions": {
                    "filesystem": {"read": [], "write": []},
                    "network": False,
                    "environment": [],
                    "commands": [],
                    "workspace_write": False,
                },
            },
        },
    )


def _safe_command() -> dict[str, object]:
    return {
        "id": "safe-command",
        "type": "safe-command",
        "executable": Path(sys.executable).name,
        "args_before_input": [
            "-X",
            "utf8",
            "-c",
            "import json,sys; print(json.dumps({'value': sys.argv[1]}, ensure_ascii=False))",
        ],
        "input_mode": "argument",
        "output_mode": "json-stdout",
        "workspace_write": False,
    }


def _workflow_steps() -> list[object]:
    return [
        {
            "id": "pipeline",
            "type": "sequence",
            "steps": [
                {"id": "seed", "type": "deterministic", "action": "specify"},
                {
                    "id": "release-if",
                    "type": "if",
                    "condition": {"eq": [{"ref": "inputs.release"}, True]},
                    "then": [
                        {
                            "id": "agent-review",
                            "type": "agent",
                            "agent": "architect",
                            "capability": "review",
                            "mode": "advisory",
                        }
                    ],
                    "else": [{"id": "release-fallback", "type": "validate"}],
                },
                {
                    "id": "route",
                    "type": "switch",
                    "value": {"ref": "inputs.channel"},
                    "cases": [
                        {
                            "when": "prod",
                            "steps": [{"id": "route-validator", "type": "validate"}],
                        }
                    ],
                    "default": [{"id": "route-quality", "type": "quality-gate", "gate": "tests"}],
                },
                {
                    "id": "parallel-review",
                    "type": "parallel",
                    "max_concurrency": 2,
                    "steps": [
                        {"id": "parallel-left", "type": "deterministic", "action": "inspect"},
                        {"id": "parallel-right", "type": "validate"},
                    ],
                },
                {
                    "id": "fan",
                    "type": "fan-out",
                    "items": {"literal": ["api", "web"]},
                    "as": "target",
                    "max_items": 2,
                    "max_concurrency": 1,
                    "steps": [
                        {
                            "id": "extension-check",
                            "type": "plugin",
                            "plugin": "release-evidence",
                            "inputs": {"token": SECRET, "mode": "review"},
                        }
                    ],
                },
                {
                    "id": "each",
                    "type": "foreach",
                    "items": {"ref": "inputs.targets"},
                    "as": "target",
                    "max_items": 3,
                    "steps": [{"id": "each-validator", "type": "validate"}],
                },
                {
                    "id": "bounded-loop",
                    "type": "bounded-while",
                    "condition": {"lt": [{"ref": "loop.iteration"}, 2]},
                    "max_iterations": 3,
                    "steps": [{"id": "loop-validator", "type": "validate"}],
                },
                {
                    "id": "join",
                    "type": "fan-in",
                    "sources": ["parallel-review", "fan"],
                    "strategy": "all-success",
                },
                _safe_command(),
                {"id": "release-quality", "type": "quality-gate", "gate": "release"},
                {"id": "release-approval", "type": "approval", "gate": "release"},
            ],
        }
    ]


def _write_release_workflow(root: Path, org_overlay: Path) -> None:
    _write_yaml(
        root / ".sdai" / "workflows" / "release-gate.yaml",
        {
            "version": 9,
            "name": "release-gate",
            "validation_mode": "standard",
            "inputs": {
                "release": {"type": "boolean", "default": True},
                "channel": {"type": "string", "default": "prod"},
                "targets": {"type": "string-list", "default": ["linux", "windows"]},
            },
            "steps": _workflow_steps(),
        },
    )
    _write_yaml(
        org_overlay,
        {
            "version": 1,
            "id": "organization-release-control",
            "workflow": "release-gate",
            "operations": [
                {
                    "op": "insert-after",
                    "target": "pipeline/seed",
                    "step": {"id": "org-validator", "type": "validate"},
                }
            ],
        },
    )
    _write_yaml(
        root / ".sdai" / "workflow-overlays" / "release-gate.yaml",
        {
            "version": 1,
            "id": "repository-release-evidence",
            "workflow": "release-gate",
            "operations": [
                {
                    "op": "append",
                    "step": {"id": "repository-finalize", "type": "deterministic", "action": "finalize"},
                }
            ],
        },
    )


class _ReleasePluginExecutor:
    def __init__(self, calls: Counter[str]) -> None:
        self.calls = calls

    def execute(self, plan, services):
        self.calls["plugin"] += 1
        assert sorted(plan.inputs) == ["mode", "token"]
        return PluginResult("passed", "release evidence recorded", data={"call": self.calls["plugin"]})


def _json_output(capsys: pytest.CaptureFixture[str]) -> tuple[dict[str, object], str]:
    text = capsys.readouterr().out
    return json.loads(text), text


def test_layered_graph_safe_execution_pause_status_resume_and_idempotency(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = tmp_path / "project-café-Δ"
    org_overlay = tmp_path / "organization-workflow.yaml"
    project.mkdir()
    _init_project(project)
    _write_release_workflow(project, org_overlay)
    monkeypatch.setenv("SDAI_ORG_WORKFLOW_OVERLAY_PATH", str(org_overlay.resolve()))

    common = ["release-gate", "--json", "--path", str(project)]
    assert sdai_main(["workflow", "graph", *common]) == 0
    graph_json, graph_text = _json_output(capsys)
    assert graph_json["apiVersion"] == "sdai.workflow-graph/v2"
    assert SECRET not in graph_text

    assert sdai_main(["workflow", "resolve", *common]) == 0
    resolution_json, resolution_text = _json_output(capsys)
    assert resolution_json["apiVersion"] == "sdai.workflow-resolution/v2"
    assert SECRET not in resolution_text
    assert sdai_main(["workflow", "validate", *common]) == 0
    validation_json, validation_text = _json_output(capsys)
    assert validation_json["status"] == "valid"
    assert SECRET not in validation_text

    resolution = load_workflow_graph(project, "release-gate")
    assert [item["overlay_id"] for item in resolution.overlays] == [
        "organization-release-control",
        "repository-release-evidence",
    ]
    expected_kinds = {
        "sequence", "if", "switch", "parallel", "fan-out", "fan-in", "foreach",
        "bounded-while", "approval", "agent", "deterministic", "validate",
        "quality-gate", "safe-command", "plugin",
    }
    assert expected_kinds <= {node.kind for node in resolution.graph.nodes}

    feature = project / "specs" / FEATURE
    feature.mkdir(parents=True)
    (feature / "00-intake.md").write_text("# Workflow Engine 2 release gate\n", encoding="utf-8")
    ledger = create_execution_run(
        project,
        FEATURE,
        "release-gate",
        BASELINE,
        run_id="v014-release-run",
    )
    calls: Counter[str] = Counter()
    approval_dispatches: list[str] = []
    policy = load_effective_configuration(project)
    safe_step = normalize_workflow_operational_step(_safe_command())
    safe_plan = build_workflow_leaf_plan(safe_step, input_text=SECRET, policy=policy)
    plugin_registry = PluginExecutorRegistry()
    plugin_registry.register("release-evidence", _ReleasePluginExecutor(calls))

    def fallback(invocation: WorkflowLeafInvocation) -> WorkflowLeafOutcome:
        calls[invocation.node.kind] += 1
        if invocation.node.kind == "safe-command":
            result = execute_safe_command_leaf(
                safe_plan,
                input_text=SECRET,
                project_root=project,
                policy=policy,
            )
            return WorkflowLeafOutcome(
                WorkflowExecutionStatus.SUCCEEDED,
                result.output,
                (f"safe-command-plan:{safe_plan.sha256}",),
            )
        if invocation.node.kind == "approval":
            approval_dispatches.append(invocation.dispatch_id)
            if len(approval_dispatches) == 1:
                return WorkflowLeafOutcome(WorkflowExecutionStatus.PAUSED, error="release approval pending")
            return WorkflowLeafOutcome(WorkflowExecutionStatus.SUCCEEDED, {"approved": True})
        return WorkflowLeafOutcome(
            WorkflowExecutionStatus.SUCCEEDED,
            {"kind": invocation.node.kind, "identity": invocation.execution_identity},
        )

    adapter = WorkflowPluginLeafExecutor(
        project,
        resolution,
        registry=plugin_registry,
        fallback=fallback,
    )
    paused = execute_workflow_graph(resolution, ledger, leaf_executor=adapter)
    assert paused.status == WorkflowExecutionStatus.PAUSED
    assert calls["plugin"] == 2
    assert calls["safe-command"] == 1
    assert not (project / "never").exists()

    assert sdai_main([
        "workflow", "status", FEATURE, "--run", "v014-release-run", "--json", "--path", str(project)
    ]) == 2
    status_json, status_text = _json_output(capsys)
    assert status_json["apiVersion"] == "sdai.workflow-run-status/v2"
    assert status_json["status"] == "paused"
    assert status_json["nextWork"]["nodePath"] == "pipeline/release-approval"
    assert SECRET not in status_text

    resumed = resume_workflow_run(
        project,
        FEATURE,
        "v014-release-run",
        leaf_executor=adapter,
    )
    assert resumed.execution.status == WorkflowExecutionStatus.SUCCEEDED
    resume_text = resumed.to_json()
    assert json.loads(resume_text)["apiVersion"] == "sdai.workflow-resume-result/v2"
    assert SECRET not in resume_text
    assert approval_dispatches[0] == approval_dispatches[1]
    assert calls["plugin"] == 2
    assert calls["safe-command"] == 1

    completed = execute_workflow_graph(
        resolution,
        load_execution_run(project, FEATURE, "v014-release-run"),
        leaf_executor=adapter,
    )
    assert completed.status == WorkflowExecutionStatus.SUCCEEDED
    assert calls["plugin"] == 2
    assert calls["safe-command"] == 1
    final_status = inspect_workflow_run(project, FEATURE, "v014-release-run")
    assert final_status.exit_code == 0
    assert json.loads(final_status.to_json())["checkpointStatus"] == "current"


def test_release_gate_fail_closed_examples_and_historical_gates_remain_enabled(
    tmp_path: Path,
) -> None:
    project = tmp_path / "negative"
    project.mkdir()
    _init_project(project)

    _write_yaml(
        project / ".sdai" / "workflows" / "unsafe.yaml",
        {
            "version": 9,
            "name": "unsafe",
            "validation_mode": "standard",
            "steps": [
                {
                    "id": "unbounded",
                    "type": "bounded-while",
                    "condition": True,
                    "steps": [{"id": "body", "type": "validate"}],
                }
            ],
        },
    )
    with pytest.raises(WorkflowGraphError, match="max_iterations"):
        load_workflow_graph(project, "unsafe")

    with pytest.raises(WorkflowOperationalStepError, match="unsupported field"):
        normalize_workflow_operational_step(
            {
                "id": "injected",
                "type": "safe-command",
                "executable": "python",
                "command": "echo unsafe && touch escaped",
            }
        )

    escalation = project / ".sdai" / "plugin-steps" / "release-evidence.yaml"
    raw = yaml.safe_load(escalation.read_text(encoding="utf-8"))
    raw["spec"]["permissions"]["environment"] = ["SECRET_TOKEN"]
    escalation.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    with pytest.raises(PluginStepError, match="environment permission denied"):
        prepare_plugin_step(project, "release-evidence", "negative", {})

    repo_root = Path(__file__).resolve().parents[1]
    historical = (
        "tests/test_v06_release_compatibility.py",
        "tests/test_v07_release_compatibility.py",
        "tests/test_v08_release_compatibility.py",
        "tests/test_v09_release_compatibility.py",
        "tests/test_v010_release_compatibility.py",
        "tests/test_v011_release_evidence.py",
        "tests/test_pack_signed_lifecycle_gate_v012.py",
        "tests/test_integration_sdk_release_gate_v013.py",
    )
    assert all((repo_root / relative).is_file() for relative in historical)
