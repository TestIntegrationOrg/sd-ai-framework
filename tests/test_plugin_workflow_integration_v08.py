from __future__ import annotations

import json
import math
from pathlib import Path

import pytest
import yaml

from sdai.entrypoint import main as sdai_main
from sdai.orchestrator import Orchestrator
from sdai.plugin_steps import PluginExecutorRegistry, PluginResult
from sdai.scaffold import init_project
from sdai.workflows import WorkflowConfigError, grant_approval, load_workflow


def _init(root: Path) -> None:
    config = root / ".sdai" / "config.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("version: 1\n", encoding="utf-8")


def _plugin(
    root: Path,
    *,
    workspace_write: bool = False,
    write_paths: tuple[str, ...] = (),
) -> None:
    path = root / ".sdai" / "plugin-steps" / "sample.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(
            {
                "apiVersion": "sdai/v1",
                "kind": "PluginStep",
                "metadata": {"id": "sample", "version": "1.0.0"},
                "spec": {
                    "publisher": "acme",
                    "executor": "sample-executor",
                    "permissions": {
                        "filesystem": {"read": [], "write": list(write_paths)},
                        "network": False,
                        "environment": [],
                        "commands": [],
                        "workspace_write": workspace_write,
                    },
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _plugin_policy(
    root: Path,
    *,
    denied: bool = False,
    workspace_write: bool = False,
    write_paths: tuple[str, ...] = (),
) -> None:
    path = root / ".sdai" / "plugin-policy.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "allowed_plugins": ["sample"],
                "denied_plugins": ["sample"] if denied else [],
                "trusted_publishers": ["acme"],
                "permissions": {
                    "filesystem": {"read": [], "write": list(write_paths)},
                    "network": False,
                    "environment": [],
                    "commands": [],
                    "workspace_write": workspace_write,
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _workflow(
    root: Path,
    name: str,
    *,
    version: int = 8,
    steps: list[object] | None = None,
) -> Path:
    path = root / ".sdai" / "workflows" / f"{name}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(
            {
                "version": version,
                "name": name,
                "validation_mode": "light",
                "steps": steps
                or [
                    {
                        "id": "scan",
                        "type": "plugin",
                        "plugin": "sample",
                        "inputs": {"target": "src"},
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return path


def _component(root: Path) -> None:
    path = root / ".sdai" / "workflow-components" / "plugin-suite.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(
            {
                "apiVersion": "sdai/v1",
                "kind": "WorkflowComponent",
                "metadata": {"id": "plugin-suite", "version": "1.0.0"},
                "spec": {
                    "inputs": {},
                    "requires": [],
                    "steps": [
                        {
                            "id": "component-scan",
                            "type": "plugin",
                            "plugin": "sample",
                            "inputs": {"source": "component"},
                        }
                    ],
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


class _PassingExecutor:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def execute(self, plan, services):
        self.calls.append(plan.step_id)
        return PluginResult(
            "passed",
            "ok",
            data={"step": plan.step_id, "input_keys": sorted(plan.inputs)},
        )


class _RetryExecutor:
    def __init__(self) -> None:
        self.calls: dict[str, int] = {}

    def execute(self, plan, services):
        count = self.calls.get(plan.step_id, 0) + 1
        self.calls[plan.step_id] = count
        if plan.step_id == "unstable" and count == 1:
            return PluginResult("failed", "try again")
        return PluginResult("passed", "ok", data={"attempt": count})


class _ContinueExecutor:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def execute(self, plan, services):
        self.calls.append(plan.step_id)
        if plan.step_id == "nonblocking":
            return PluginResult("failed", "expected failure")
        return PluginResult("passed", "continued")


def _registry(executor) -> PluginExecutorRegistry:
    registry = PluginExecutorRegistry()
    registry.register("sample-executor", executor)
    return registry


def test_plugin_workflow_step_requires_explicit_version_8(tmp_path: Path) -> None:
    _workflow(tmp_path, "old", version=7)
    with pytest.raises(WorkflowConfigError, match="Plugin workflow steps require explicit workflow version 8"):
        load_workflow(tmp_path, "old")

    _workflow(tmp_path, "current", version=8)
    step = load_workflow(tmp_path, "current").step("scan")
    assert step.kind.value == "plugin"
    assert step.plugin_id == "sample"
    assert step.plugin_input_values == {"target": "src"}


@pytest.mark.parametrize("field", ["profile", "provider", "mode", "shell", "command", "argv", "exec", "save_as"])
def test_plugin_workflow_step_rejects_execution_escape_fields(
    tmp_path: Path,
    field: str,
) -> None:
    step: dict[str, object] = {
        "id": "scan",
        "type": "plugin",
        "plugin": "sample",
        "inputs": {},
        field: "danger",
    }
    _workflow(tmp_path, "unsafe", steps=[step])
    with pytest.raises(WorkflowConfigError, match="unsupported field"):
        load_workflow(tmp_path, "unsafe")


def test_component_expanded_plugin_is_version_gated_and_parsed(tmp_path: Path) -> None:
    _component(tmp_path)
    _workflow(
        tmp_path,
        "component-old",
        version=7,
        steps=[{"uses": "component:plugin-suite"}],
    )
    with pytest.raises(WorkflowConfigError, match="version 8"):
        load_workflow(tmp_path, "component-old")

    _workflow(
        tmp_path,
        "component-current",
        version=8,
        steps=[{"uses": "component:plugin-suite"}],
    )
    definition = load_workflow(tmp_path, "component-current")
    assert definition.step("component-scan").plugin_id == "sample"
    assert definition.components[0].component_id == "plugin-suite"


def test_workflow_validate_prepares_plugin_without_executor_or_side_effects(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _init(tmp_path)
    _plugin(tmp_path)
    _plugin_policy(tmp_path)
    _workflow(tmp_path, "validated")

    assert (
        sdai_main(
            [
                "workflow",
                "validate",
                "validated",
                "--json",
                "--path",
                str(tmp_path),
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    plan = payload["plugin_plans"]["scan"]
    assert plan["plugin"]["id"] == "sample"
    assert plan["plugin"]["executor"] == "sample-executor"
    assert plan["effective_permissions"]["workspace_write"] is False
    assert plan["input_keys"] == ["target"]
    assert "target" not in json.dumps(plan)
    assert not (tmp_path / "specs").exists()


def test_workflow_validate_rejects_nonfinite_plugin_input(tmp_path: Path) -> None:
    _init(tmp_path)
    _plugin(tmp_path)
    _plugin_policy(tmp_path)
    _workflow(
        tmp_path,
        "nan-input",
        steps=[
            {
                "id": "scan",
                "type": "plugin",
                "plugin": "sample",
                "inputs": {"value": math.nan},
            }
        ],
    )
    assert (
        sdai_main(
            ["workflow", "validate", "nan-input", "--path", str(tmp_path)]
        )
        == 1
    )


def test_component_cannot_bypass_organization_plugin_deny(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init(tmp_path)
    _plugin(tmp_path)
    _plugin_policy(tmp_path)
    _component(tmp_path)
    _workflow(
        tmp_path,
        "denied-component",
        steps=[{"uses": "component:plugin-suite"}],
    )
    org = tmp_path.parent / f"{tmp_path.name}-org-plugin.yaml"
    org.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "allowed_plugins": ["sample"],
                "denied_plugins": ["sample"],
                "trusted_publishers": ["acme"],
                "permissions": {
                    "filesystem": {"read": [], "write": []},
                    "network": False,
                    "environment": [],
                    "commands": [],
                    "workspace_write": False,
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("SDAI_ORG_PLUGIN_POLICY_PATH", str(org.resolve()))

    assert (
        sdai_main(
            ["workflow", "validate", "denied-component", "--path", str(tmp_path)]
        )
        == 1
    )


def test_overlay_and_lifecycle_hook_cannot_inject_plugin_step(tmp_path: Path) -> None:
    _workflow(
        tmp_path,
        "overlay-safe",
        steps=[{"id": "validate", "type": "validate"}],
    )
    overlay = tmp_path / ".sdai" / "workflow-overlays" / "bad.yaml"
    overlay.parent.mkdir(parents=True, exist_ok=True)
    overlay.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "id": "repo-plugin",
                "workflow": "overlay-safe",
                "operations": [
                    {
                        "op": "append",
                        "step": {
                            "id": "injected",
                            "type": "plugin",
                            "plugin": "sample",
                            "inputs": {},
                        },
                    }
                ],
                "hooks": {},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    with pytest.raises(WorkflowConfigError, match="SDAI-WFOVER-003.*unsupported step type 'plugin'"):
        load_workflow(tmp_path, "overlay-safe")


def test_orchestrator_dry_run_does_not_require_registered_executor_or_write_evidence(
    tmp_path: Path,
) -> None:
    _plugin(tmp_path)
    _plugin_policy(tmp_path)
    _workflow(tmp_path, "dry-run")
    orchestrator = Orchestrator(tmp_path, plugin_executor_registry=PluginExecutorRegistry())

    execution = orchestrator.run_manual_step("PLUG-DRY", "dry-run", "scan", dry_run=True)

    assert execution.status == "dry-run"
    assert execution.result.plugin.id == "sample"
    assert not (tmp_path / "specs" / "PLUG-DRY" / "plugin" / "scan.json").exists()


def test_orchestrator_executes_registered_plugin_and_persists_structured_evidence(
    tmp_path: Path,
) -> None:
    _plugin(tmp_path)
    _plugin_policy(tmp_path)
    _workflow(tmp_path, "execute")
    executor = _PassingExecutor()
    orchestrator = Orchestrator(tmp_path, plugin_executor_registry=_registry(executor))

    execution = orchestrator.run_manual_step("PLUG-1", "execute", "scan")

    assert execution.status == "completed"
    assert execution.attempts == 1
    assert executor.calls == ["scan"]
    evidence_path = tmp_path / "specs" / "PLUG-1" / "plugin" / "scan.json"
    payload = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert payload["plan"]["plugin"]["id"] == "sample"
    assert payload["plan"]["input_keys"] == ["target"]
    assert payload["plan"]["input_sha256"].startswith("sha256:")
    assert payload["result"]["status"] == "passed"
    assert payload["result"]["data"]["step"] == "scan"
    assert "target" not in json.dumps(payload["plan"]["plugin"])


def test_plugin_failed_result_retries_then_succeeds(tmp_path: Path) -> None:
    _plugin(tmp_path)
    _plugin_policy(tmp_path)
    _workflow(
        tmp_path,
        "retry",
        steps=[
            {
                "id": "unstable",
                "type": "plugin",
                "plugin": "sample",
                "inputs": {},
                "retry": {"max_attempts": 2, "delay_seconds": 0},
            }
        ],
    )
    executor = _RetryExecutor()
    orchestrator = Orchestrator(tmp_path, plugin_executor_registry=_registry(executor))

    execution = orchestrator.run_manual_step("PLUG-2", "retry", "unstable")

    assert execution.status == "completed"
    assert execution.attempts == 2
    assert executor.calls["unstable"] == 2


def test_plugin_on_failure_continue_allows_next_workflow_step(tmp_path: Path) -> None:
    _plugin(tmp_path)
    _plugin_policy(tmp_path)
    _workflow(
        tmp_path,
        "continue-flow",
        steps=[
            {
                "id": "nonblocking",
                "type": "plugin",
                "plugin": "sample",
                "inputs": {},
                "on_failure": "continue",
            },
            {
                "id": "after",
                "type": "plugin",
                "plugin": "sample",
                "inputs": {},
            },
        ],
    )
    executor = _ContinueExecutor()
    orchestrator = Orchestrator(tmp_path, plugin_executor_registry=_registry(executor))

    executions = orchestrator.run_workflow("PLUG-3", "continue-flow")

    assert [item.status for item in executions] == ["failed", "completed"]
    assert executor.calls == ["nonblocking", "after"]


def test_plugin_workspace_write_obeys_existing_prior_approval_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    init_project(tmp_path)
    config_path = tmp_path / ".sdai" / "config.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["operating_mode"] = "enterprise"
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")

    org_policy = tmp_path.parent / f"{tmp_path.name}-org-policy.yaml"
    org_policy.write_text(
        "version: 1\nexecution:\n  require_prior_approval_for_workspace_write: true\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SDAI_ORG_POLICY_PATH", str(org_policy.resolve()))

    _plugin(tmp_path, workspace_write=True, write_paths=("generated",))
    _plugin_policy(tmp_path, workspace_write=True, write_paths=("generated",))
    _workflow(
        tmp_path,
        "write-plugin",
        steps=[
            {"id": "architecture-approval", "type": "approval", "gate": "architecture"},
            {
                "id": "write",
                "type": "plugin",
                "plugin": "sample",
                "inputs": {},
            },
        ],
    )
    executor = _PassingExecutor()
    orchestrator = Orchestrator(tmp_path, plugin_executor_registry=_registry(executor))

    with pytest.raises(RuntimeError, match="unsatisfied prior approval"):
        orchestrator.run_manual_step("PLUG-4", "write-plugin", "write")
    assert executor.calls == []

    grant_approval(
        orchestrator.context("PLUG-4"),
        "architecture",
        approved_by="architect@example.com",
    )
    execution = orchestrator.run_manual_step("PLUG-4", "write-plugin", "write")
    assert execution.status == "completed"
    assert executor.calls == ["write"]
