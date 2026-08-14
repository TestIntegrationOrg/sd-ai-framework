from __future__ import annotations

from pathlib import Path
import sys

import pytest

from sdai.policy import EffectiveConfiguration, OperatingMode
from sdai.workflow_operational_steps import (
    OperationalStepKind,
    WorkflowOperationalStepError,
    build_workflow_leaf_plan,
    execute_safe_command_leaf,
    normalize_workflow_operational_step,
)


def _policy(*, workspace_write: bool = False, environment=None) -> EffectiveConfiguration:
    return EffectiveConfiguration(
        operating_mode=OperatingMode.ENTERPRISE,
        sources=("org-policy", "repo-policy"),
        allowed_profiles=None,
        allowed_providers=None,
        allowed_models={},
        capability_profiles={},
        capability_providers={},
        workspace_write=workspace_write,
        require_prior_approval_for_workspace_write=False,
        allow_force_approval_bypass=False,
        protected_paths=(".sdai/**", ".agents/**", "specs/**"),
        environment_allowlist=environment,
        required_skills_map={},
    )


def _python_name() -> str:
    return Path(sys.executable).name


def test_normalizes_all_existing_leaf_kinds_and_validator_alias() -> None:
    cases = [
        ("build", OperationalStepKind.DETERMINISTIC),
        ({"id": "agent", "type": "agent", "capability": "coding"}, OperationalStepKind.AGENT),
        ({"id": "approve", "type": "approval", "gate": "security"}, OperationalStepKind.APPROVAL),
        ("validate", OperationalStepKind.VALIDATOR),
        ({"id": "validate2", "type": "validator"}, OperationalStepKind.VALIDATOR),
        ({"id": "quality", "type": "quality-gate", "gate": "release"}, OperationalStepKind.QUALITY_GATE),
        ({"id": "plug", "type": "plugin", "plugin": "sample", "inputs": {"b": 2, "a": 1}}, OperationalStepKind.PLUGIN),
    ]
    for raw, expected in cases:
        step = normalize_workflow_operational_step(raw)
        assert step.kind == expected
        assert step.to_json().encode("utf-8").decode("utf-8") == step.to_json()


def test_plugin_plan_hashes_inputs_without_exposing_values() -> None:
    step = normalize_workflow_operational_step(
        {"id": "plug", "type": "plugin", "plugin": "sample", "inputs": {"token": "secret-value"}}
    )
    text = step.to_json()
    assert "secret-value" not in text
    assert step.config["inputKeys"] == ["token"]
    assert str(step.config["inputsSha256"]).startswith("sha256:")


def test_safe_command_is_strict_shell_free_and_allows_literal_metacharacters() -> None:
    step = normalize_workflow_operational_step(
        {
            "id": "echo",
            "type": "safe-command",
            "executable": _python_name(),
            "args_before_input": ["-X", "utf8", "-c", "import sys; print(sys.argv[1])"],
            "input_mode": "argument",
            "output_mode": "stdout",
            "args_after_input": ["literal && still-one-argv ; | $(no-shell)"],
        }
    )
    assert step.kind == OperationalStepKind.SAFE_COMMAND
    assert step.safe_command is not None
    assert step.safe_command.args_after_input[-1] == "literal && still-one-argv ; | $(no-shell)"
    with pytest.raises(WorkflowOperationalStepError, match="unsupported field"):
        normalize_workflow_operational_step(
            {"id": "bad", "type": "safe-command", "executable": "python", "command": "echo unsafe"}
        )


def test_safe_command_plan_is_machine_clean_and_hides_runtime_input() -> None:
    payload = "literal; echo HACKED && $(touch never) | café Δ"
    step = normalize_workflow_operational_step(
        {
            "id": "argv",
            "type": "safe-command",
            "executable": _python_name(),
            "args_before_input": ["-X", "utf8", "-c", "import sys; print(sys.argv[1])"],
            "input_mode": "argument",
            "output_mode": "stdout",
        }
    )
    plan = build_workflow_leaf_plan(step, input_text=payload, policy=_policy())
    assert payload not in plan.to_json()
    assert plan.input_sha256.startswith("sha256:")
    assert plan.policy_sources == ("org-policy", "repo-policy")


def test_safe_command_executes_dynamic_argument_as_one_token(tmp_path: Path) -> None:
    payload = "literal; echo HACKED && $(touch never) | café Δ"
    step = normalize_workflow_operational_step(
        {
            "id": "argv",
            "type": "safe-command",
            "executable": _python_name(),
            "args_before_input": ["-X", "utf8", "-c", "import sys; print(sys.argv[1])"],
            "input_mode": "argument",
            "output_mode": "stdout",
        }
    )
    plan = build_workflow_leaf_plan(step, input_text=payload, policy=_policy())
    result = execute_safe_command_leaf(plan, input_text=payload, project_root=tmp_path, policy=_policy())
    assert result.status == "succeeded"
    assert result.output == payload
    assert not (tmp_path / "never").exists()


def test_stdin_json_and_utf8_are_normalized(tmp_path: Path) -> None:
    step = normalize_workflow_operational_step(
        {
            "id": "json",
            "type": "safe-command",
            "executable": _python_name(),
            "args_before_input": [
                "-X", "utf8", "-c",
                "import json,sys; value=sys.stdin.read(); print(json.dumps({'value': value, 'café': 'Δ'}, ensure_ascii=False))",
            ],
            "input_mode": "stdin",
            "output_mode": "json-stdout",
        }
    )
    plan = build_workflow_leaf_plan(step, input_text="bonjour Δ", policy=_policy())
    result = execute_safe_command_leaf(plan, input_text="bonjour Δ", project_root=tmp_path, policy=_policy())
    assert result.status == "succeeded"
    assert result.output == {"value": "bonjour Δ", "café": "Δ"}


def test_environment_requirement_cannot_widen_effective_policy(tmp_path: Path) -> None:
    step = normalize_workflow_operational_step(
        {
            "id": "env",
            "type": "safe-command",
            "executable": _python_name(),
            "args_before_input": ["-X", "utf8", "-c", "import os; print(os.getenv('SDAI_TEST_ALLOWED','missing'))"],
            "input_mode": "none",
            "output_mode": "stdout",
            "environment": ["SDAI_TEST_ALLOWED"],
        }
    )
    with pytest.raises(WorkflowOperationalStepError, match="environment denied"):
        build_workflow_leaf_plan(step, policy=_policy(environment=frozenset()))
    policy = _policy(environment=frozenset({"SDAI_TEST_ALLOWED"}))
    plan = build_workflow_leaf_plan(step, policy=policy)
    result = execute_safe_command_leaf(
        plan,
        project_root=tmp_path,
        policy=policy,
        environment={"SDAI_TEST_ALLOWED": "visible", "UNDECLARED_SECRET": "must-not-forward"},
    )
    assert result.status == "succeeded"
    assert result.output == "visible"
    assert "visible" not in plan.to_json()


def test_workspace_write_is_checked_at_plan_and_execution_time(tmp_path: Path) -> None:
    step = normalize_workflow_operational_step(
        {
            "id": "write",
            "type": "safe-command",
            "executable": _python_name(),
            "args_before_input": ["-X", "utf8", "-c", "print('ok')"],
            "workspace_write": True,
        }
    )
    with pytest.raises(WorkflowOperationalStepError, match="workspace-write"):
        build_workflow_leaf_plan(step, policy=_policy(workspace_write=False))
    permissive = _policy(workspace_write=True)
    plan = build_workflow_leaf_plan(step, policy=permissive)
    result = execute_safe_command_leaf(plan, project_root=tmp_path, policy=_policy(workspace_write=False))
    assert result.status == "policy-violation"


def test_protected_path_modification_is_restored(tmp_path: Path) -> None:
    protected = tmp_path / ".sdai" / "state.txt"
    protected.parent.mkdir()
    protected.write_text("original", encoding="utf-8")
    step = normalize_workflow_operational_step(
        {
            "id": "tamper",
            "type": "safe-command",
            "executable": _python_name(),
            "args_before_input": [
                "-X", "utf8", "-c",
                "from pathlib import Path; Path('.sdai/state.txt').write_text('changed', encoding='utf-8')",
            ],
            "workspace_write": True,
            "output_mode": "none",
        }
    )
    policy = _policy(workspace_write=True)
    plan = build_workflow_leaf_plan(step, policy=policy)
    result = execute_safe_command_leaf(plan, project_root=tmp_path, policy=policy)
    assert result.status == "policy-violation"
    assert protected.read_text(encoding="utf-8") == "original"


def test_timeout_nonzero_and_malformed_output_are_stable(tmp_path: Path) -> None:
    timeout_step = normalize_workflow_operational_step(
        {
            "id": "timeout", "type": "safe-command", "executable": _python_name(),
            "args_before_input": ["-X", "utf8", "-c", "import time; time.sleep(2)"],
            "timeout_seconds": 1,
        }
    )
    timeout_plan = build_workflow_leaf_plan(timeout_step, policy=_policy())
    assert execute_safe_command_leaf(timeout_plan, project_root=tmp_path, policy=_policy()).status == "timed-out"

    exit_step = normalize_workflow_operational_step(
        {
            "id": "exit", "type": "safe-command", "executable": _python_name(),
            "args_before_input": ["-X", "utf8", "-c", "import sys; print('private stderr', file=sys.stderr); raise SystemExit(7)"],
        }
    )
    exit_plan = build_workflow_leaf_plan(exit_step, policy=_policy())
    exit_result = execute_safe_command_leaf(exit_plan, project_root=tmp_path, policy=_policy())
    assert exit_result.status == "exit-error"
    assert exit_result.exit_code == 7
    assert "private stderr" not in exit_result.to_json()

    bad_json = normalize_workflow_operational_step(
        {
            "id": "bad-json", "type": "safe-command", "executable": _python_name(),
            "args_before_input": ["-X", "utf8", "-c", "print('{bad json')"],
            "output_mode": "json-stdout",
        }
    )
    bad_plan = build_workflow_leaf_plan(bad_json, policy=_policy())
    assert execute_safe_command_leaf(bad_plan, project_root=tmp_path, policy=_policy()).status == "malformed-output"


def test_invalid_utf8_and_unsafe_paths_fail_closed(tmp_path: Path) -> None:
    invalid_utf8 = normalize_workflow_operational_step(
        {
            "id": "bytes", "type": "safe-command", "executable": _python_name(),
            "args_before_input": ["-c", "import sys; sys.stdout.buffer.write(bytes([255]))"],
        }
    )
    plan = build_workflow_leaf_plan(invalid_utf8, policy=_policy())
    assert execute_safe_command_leaf(plan, project_root=tmp_path, policy=_policy()).status == "malformed-output"

    with pytest.raises(WorkflowOperationalStepError, match="project-relative"):
        normalize_workflow_operational_step(
            {"id": "bad", "type": "safe-command", "executable": "python", "input_mode": "file", "input_path": "../escape.txt"}
        )
    with pytest.raises(WorkflowOperationalStepError, match="requires cwd"):
        normalize_workflow_operational_step(
            {"id": "cwd", "type": "safe-command", "executable": "python", "cwd": "tools"}
        )
