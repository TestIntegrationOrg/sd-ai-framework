from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from sdai.entrypoint import main as sdai_main
from sdai.workflows import WorkflowConfigError, load_workflow


def _workflow(
    root: Path,
    name: str,
    *,
    version: int = 7,
    validation_mode: str = "standard",
    extends: str | None = None,
    lifecycle: dict[str, str] | None = None,
    inputs: dict[str, object] | None = None,
    steps: list[object] | None = None,
) -> Path:
    path = root / ".sdai" / "workflows" / f"{name}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {
        "version": version,
        "name": name,
        "validation_mode": validation_mode,
        "steps": steps or [],
    }
    if extends:
        payload["extends"] = extends
    if lifecycle is not None:
        payload["lifecycle"] = lifecycle
    if inputs is not None:
        payload["inputs"] = inputs
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path


def _overlay(
    path: Path,
    overlay_id: str,
    workflow: str,
    *,
    operations: list[dict[str, object]] | None = None,
    hooks: dict[str, list[object]] | None = None,
    required_steps: list[str] | None = None,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {
        "version": 1,
        "id": overlay_id,
        "workflow": workflow,
        "operations": operations or [],
        "hooks": hooks or {},
    }
    if required_steps is not None:
        payload["required_steps"] = required_steps
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path


def _base_steps() -> list[object]:
    return [
        {
            "id": "requirements",
            "type": "agent",
            "agent": "requirements-analyst",
            "capability": "requirements",
            "mode": "advisory",
        },
        {
            "id": "architecture",
            "type": "agent",
            "agent": "architect",
            "capability": "architecture",
            "mode": "advisory",
        },
        {
            "id": "implementation",
            "type": "agent",
            "agent": "developer",
            "capability": "coding",
            "mode": "workspace-write",
        },
        {"id": "validate", "type": "validate"},
        {
            "id": "delivery-approval",
            "type": "approval",
            "gate": "delivery",
        },
    ]


def _lifecycle() -> dict[str, str]:
    return {
        "requirements": "requirements",
        "architecture": "architecture",
        "implementation": "implementation",
        "verify": "validate",
        "delivery": "delivery-approval",
    }


def _init(root: Path) -> None:
    config = root / ".sdai" / "config.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("version: 1\n", encoding="utf-8")


def test_existing_v5_workflow_without_overlay_remains_unchanged(tmp_path: Path) -> None:
    _workflow(
        tmp_path,
        "legacy",
        version=5,
        steps=[
            {
                "id": "review",
                "type": "agent",
                "agent": "code-reviewer",
                "capability": "review",
                "profile": "codex",
                "mode": "advisory",
            },
            {"id": "validate", "type": "validate"},
        ],
    )

    definition = load_workflow(tmp_path, "legacy", environ={})

    assert definition.workflow_version == 5
    assert definition.inheritance == ("legacy",)
    assert definition.overlays == ()
    assert definition.lifecycle_hooks == ()
    assert definition.mandatory_steps == ()
    assert definition.step("review").profile == "codex"


def test_inheritance_adds_steps_and_inputs_without_copying_parent(tmp_path: Path) -> None:
    _workflow(
        tmp_path,
        "base",
        validation_mode="standard",
        inputs={"service": {"type": "string", "default": "base"}},
        steps=[{"id": "validate", "type": "validate"}],
    )
    _workflow(
        tmp_path,
        "derived",
        extends="base",
        validation_mode="critical",
        inputs={"region": {"type": "string", "default": "us"}},
        steps=[
            {
                "id": "review",
                "type": "agent",
                "agent": "code-reviewer",
                "capability": "review",
                "mode": "advisory",
            }
        ],
    )

    definition = load_workflow(tmp_path, "derived", environ={})

    assert definition.inheritance == ("base", "derived")
    assert definition.workflow_version == 7
    assert definition.validation_mode.value == "critical"
    assert [step.id for step in definition.steps] == ["validate", "review"]
    assert {item.name for item in definition.input_definitions} == {"service", "region"}
    assert definition.name == "derived"


def test_inheritance_cycle_and_validation_mode_weakening_fail_closed(tmp_path: Path) -> None:
    _workflow(tmp_path, "a", extends="b", steps=[{"id": "a-step", "type": "validate"}])
    _workflow(tmp_path, "b", extends="a", steps=[{"id": "b-step", "type": "validate"}])
    with pytest.raises(WorkflowConfigError, match="SDAI-WFOVER-002.*inheritance cycle"):
        load_workflow(tmp_path, "a", environ={})

    root = tmp_path / "weaken"
    _workflow(root, "critical-base", validation_mode="critical", steps=[{"id": "validate", "type": "validate"}])
    _workflow(
        root,
        "weaker-child",
        extends="critical-base",
        validation_mode="standard",
        steps=[],
    )
    with pytest.raises(WorkflowConfigError, match="SDAI-WFOVER-002.*cannot lower inherited validation_mode"):
        load_workflow(root, "weaker-child", environ={})


def test_org_overlay_targeting_parent_applies_to_derived_and_is_mandatory(tmp_path: Path) -> None:
    _workflow(
        tmp_path,
        "enterprise-base",
        validation_mode="critical",
        lifecycle=_lifecycle(),
        steps=_base_steps(),
    )
    _workflow(
        tmp_path,
        "payments",
        extends="enterprise-base",
        validation_mode="critical",
        steps=[
            {
                "id": "payments-review",
                "type": "agent",
                "agent": "code-reviewer",
                "capability": "review",
                "mode": "advisory",
            }
        ],
    )
    org = _overlay(
        tmp_path / "org-overlay.yaml",
        "org-security",
        "enterprise-base",
        operations=[
            {
                "op": "add-before",
                "target": "delivery-approval",
                "step": {
                    "id": "org-security-approval",
                    "type": "approval",
                    "gate": "org-security",
                },
            }
        ],
        required_steps=["org-security-approval"],
    )

    definition = load_workflow(
        tmp_path,
        "payments",
        environ={"SDAI_ORG_WORKFLOW_OVERLAY_PATH": str(org.resolve())},
    )

    assert definition.inheritance == ("enterprise-base", "payments")
    assert definition.step("org-security-approval").gate == "org-security"
    assert "org-security-approval" in definition.mandatory_steps
    assert definition.overlays[0].target == "enterprise-base"


def test_repo_cannot_disable_or_replace_org_mandated_step(tmp_path: Path) -> None:
    _workflow(tmp_path, "enterprise", validation_mode="critical", lifecycle=_lifecycle(), steps=_base_steps())
    org = _overlay(
        tmp_path / "org.yaml",
        "org-gate",
        "enterprise",
        operations=[
            {
                "op": "add-before",
                "target": "delivery-approval",
                "step": {"id": "org-approval", "type": "approval", "gate": "org"},
            }
        ],
    )
    _overlay(
        tmp_path / ".sdai" / "workflow-overlays" / "repo.yaml",
        "repo-weaken",
        "enterprise",
        operations=[{"op": "disable", "target": "org-approval"}],
    )

    with pytest.raises(WorkflowConfigError, match="SDAI-WFOVER-004.*organization-mandated step 'org-approval'"):
        load_workflow(
            tmp_path,
            "enterprise",
            environ={"SDAI_ORG_WORKFLOW_OVERLAY_PATH": str(org.resolve())},
        )


def test_repo_user_cannot_disable_protected_core_gate_validation_or_security_step(tmp_path: Path) -> None:
    steps = _base_steps()
    steps.insert(
        3,
        {
            "id": "security-review",
            "type": "agent",
            "agent": "security-reviewer",
            "capability": "security",
            "mode": "advisory",
        },
    )
    _workflow(tmp_path, "protected", validation_mode="critical", lifecycle=_lifecycle(), steps=steps)

    for target in ("validate", "delivery-approval", "security-review"):
        root = tmp_path / target
        _workflow(root, "protected", validation_mode="critical", lifecycle=_lifecycle(), steps=steps)
        _overlay(
            root / ".sdai" / "workflow-overlays" / "disable.yaml",
            "repo-disable",
            "protected",
            operations=[{"op": "disable", "target": target}],
        )
        with pytest.raises(WorkflowConfigError, match="SDAI-WFOVER-004.*cannot disable protected step"):
            load_workflow(root, "protected", environ={})


def test_lower_overlay_cannot_change_agent_capability_identity_or_widen_mode(tmp_path: Path) -> None:
    _workflow(tmp_path, "agents", steps=[_base_steps()[0]])
    cases = [
        {
            "id": "requirements",
            "type": "agent",
            "agent": "requirements-analyst",
            "capability": "architecture",
            "mode": "advisory",
        },
        {
            "id": "requirements",
            "type": "agent",
            "agent": "architect",
            "capability": "requirements",
            "mode": "advisory",
        },
        {
            "id": "requirements",
            "type": "agent",
            "agent": "requirements-analyst",
            "capability": "requirements",
            "mode": "workspace-write",
        },
    ]
    expected = ["capability", "semantic agent", "widen advisory"]
    for index, (replacement, fragment) in enumerate(zip(cases, expected)):
        root = tmp_path / f"case-{index}"
        _workflow(root, "agents", steps=[_base_steps()[0]])
        _overlay(
            root / ".sdai" / "workflow-overlays" / "replace.yaml",
            "repo-replace",
            "agents",
            operations=[{"op": "replace", "target": "requirements", "step": replacement}],
        )
        with pytest.raises(WorkflowConfigError, match=rf"SDAI-WFOVER-004.*{fragment}"):
            load_workflow(root, "agents", environ={})


def test_safe_lower_replacement_can_reduce_workspace_write_to_advisory(tmp_path: Path) -> None:
    _workflow(tmp_path, "safe", steps=[_base_steps()[2]])
    _overlay(
        tmp_path / ".sdai" / "workflow-overlays" / "safe.yaml",
        "repo-safe",
        "safe",
        operations=[
            {
                "op": "replace",
                "target": "implementation",
                "step": {
                    "id": "implementation",
                    "type": "agent",
                    "agent": "developer",
                    "capability": "coding",
                    "mode": "advisory",
                    "description": "Read-only implementation review",
                },
            }
        ],
    )

    definition = load_workflow(tmp_path, "safe", environ={})
    assert definition.step("implementation").mode.value == "advisory"


def test_org_lifecycle_hook_is_inserted_at_anchor_and_lower_layer_cannot_remove_anchor(tmp_path: Path) -> None:
    _workflow(tmp_path, "hooked", validation_mode="critical", lifecycle=_lifecycle(), steps=_base_steps())
    org = _overlay(
        tmp_path / "org.yaml",
        "org-hooks",
        "hooked",
        hooks={
            "before:architecture": [
                {
                    "id": "org-architecture-policy",
                    "type": "agent",
                    "agent": "security-reviewer",
                    "capability": "security",
                    "mode": "advisory",
                }
            ]
        },
    )

    definition = load_workflow(
        tmp_path,
        "hooked",
        environ={"SDAI_ORG_WORKFLOW_OVERLAY_PATH": str(org.resolve())},
    )
    ids = [step.id for step in definition.steps]
    assert ids.index("org-architecture-policy") + 1 == ids.index("architecture")
    assert "org-architecture-policy" in definition.mandatory_steps
    assert "architecture" in definition.mandatory_steps
    assert definition.lifecycle_hooks[0].point == "before:architecture"

    _overlay(
        tmp_path / ".sdai" / "workflow-overlays" / "weaken.yaml",
        "repo-weaken-anchor",
        "hooked",
        operations=[{"op": "disable", "target": "architecture"}],
    )
    with pytest.raises(WorkflowConfigError, match="SDAI-WFOVER-004.*organization-mandated step 'architecture'"):
        load_workflow(
            tmp_path,
            "hooked",
            environ={"SDAI_ORG_WORKFLOW_OVERLAY_PATH": str(org.resolve())},
        )


def test_lifecycle_hooks_are_advisory_gate_validation_only(tmp_path: Path) -> None:
    _workflow(tmp_path, "hooked", lifecycle=_lifecycle(), steps=_base_steps())
    bad_steps = [
        {
            "id": "writer",
            "type": "agent",
            "agent": "developer",
            "capability": "coding",
            "mode": "workspace-write",
        },
        {"id": "deterministic", "type": "deterministic", "action": "implement"},
    ]
    for index, bad_step in enumerate(bad_steps):
        root = tmp_path / f"bad-hook-{index}"
        _workflow(root, "hooked", lifecycle=_lifecycle(), steps=_base_steps())
        _overlay(
            root / ".sdai" / "workflow-overlays" / "bad.yaml",
            "repo-bad-hook",
            "hooked",
            hooks={"before:implementation": [bad_step]},
        )
        with pytest.raises(WorkflowConfigError, match="SDAI-WFOVER-007"):
            load_workflow(root, "hooked", environ={})


def test_overlay_steps_reject_provider_shell_and_component_execution_fields(tmp_path: Path) -> None:
    _workflow(tmp_path, "safe", steps=[{"id": "validate", "type": "validate"}])
    for field in ("profile", "provider", "shell", "command", "exec", "argv", "uses"):
        root = tmp_path / field
        _workflow(root, "safe", steps=[{"id": "validate", "type": "validate"}])
        step: dict[str, object] = {
            "id": "added",
            "type": "agent",
            "agent": "code-reviewer",
            "capability": "review",
            "mode": "advisory",
            field: "danger",
        }
        _overlay(
            root / ".sdai" / "workflow-overlays" / "bad.yaml",
            "repo-bad",
            "safe",
            operations=[{"op": "append", "step": step}],
        )
        with pytest.raises(WorkflowConfigError, match="SDAI-WFOVER-003"):
            load_workflow(root, "safe", environ={})


def test_hook_requires_declared_lifecycle_anchor(tmp_path: Path) -> None:
    _workflow(tmp_path, "no-lifecycle", steps=[{"id": "validate", "type": "validate"}])
    _overlay(
        tmp_path / ".sdai" / "workflow-overlays" / "hook.yaml",
        "repo-hook",
        "no-lifecycle",
        hooks={"before:architecture": [{"id": "review", "type": "validate"}]},
    )

    with pytest.raises(WorkflowConfigError, match="SDAI-WFOVER-006.*does not declare lifecycle anchor 'architecture'"):
        load_workflow(tmp_path, "no-lifecycle", environ={})


def test_same_layer_double_mutation_of_target_fails_instead_of_order_dependent_last_write(tmp_path: Path) -> None:
    _workflow(tmp_path, "mutations", steps=[_base_steps()[0]])
    _overlay(
        tmp_path / ".sdai" / "workflow-overlays" / "a.yaml",
        "repo-a",
        "mutations",
        operations=[
            {
                "op": "replace",
                "target": "requirements",
                "step": {
                    "id": "requirements",
                    "type": "agent",
                    "agent": "requirements-analyst",
                    "capability": "requirements",
                    "mode": "advisory",
                    "description": "first",
                },
            }
        ],
    )
    _overlay(
        tmp_path / ".sdai" / "workflow-overlays" / "b.yaml",
        "repo-b",
        "mutations",
        operations=[{"op": "disable", "target": "requirements"}],
    )

    with pytest.raises(WorkflowConfigError, match="SDAI-WFOVER-003.*mutates step 'requirements' more than once"):
        load_workflow(tmp_path, "mutations", environ={})


def test_org_repo_user_overlay_order_and_explain_provenance_are_deterministic(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "Enterprise Workspace Ω"
    root.mkdir()
    _init(root)
    _workflow(root, "ordered", lifecycle=_lifecycle(), steps=_base_steps())
    org = _overlay(
        root / "org café.yaml",
        "org-order",
        "ordered",
        operations=[
            {
                "op": "add-after",
                "target": "requirements",
                "step": {"id": "org-review", "type": "validate"},
            }
        ],
    )
    _overlay(
        root / ".sdai" / "workflow-overlays" / "repo.yaml",
        "repo-order",
        "ordered",
        operations=[
            {
                "op": "add-after",
                "target": "org-review",
                "step": {
                    "id": "repo-review",
                    "type": "agent",
                    "agent": "code-reviewer",
                    "capability": "review",
                    "mode": "advisory",
                },
            }
        ],
    )
    user = _overlay(
        root / "user.yaml",
        "user-order",
        "ordered",
        operations=[
            {
                "op": "append",
                "step": {"id": "user-validation", "type": "validate"},
            }
        ],
    )

    environ = {
        "SDAI_ORG_WORKFLOW_OVERLAY_PATH": str(org.resolve()),
        "SDAI_USER_WORKFLOW_OVERLAY_PATH": str(user.resolve()),
    }
    definition = load_workflow(root, "ordered", environ=environ)

    assert [item.layer.value for item in definition.overlays] == ["org", "repo", "user"]
    assert [step.id for step in definition.steps][:4] == [
        "requirements",
        "org-review",
        "repo-review",
        "architecture",
    ]
    assert definition.steps[-1].id == "user-validation"

    old_org = __import__("os").environ.get("SDAI_ORG_WORKFLOW_OVERLAY_PATH")
    old_user = __import__("os").environ.get("SDAI_USER_WORKFLOW_OVERLAY_PATH")
    try:
        __import__("os").environ["SDAI_ORG_WORKFLOW_OVERLAY_PATH"] = str(org.resolve())
        __import__("os").environ["SDAI_USER_WORKFLOW_OVERLAY_PATH"] = str(user.resolve())
        assert (
            sdai_main(
                [
                    "workflow",
                    "explain",
                    "ordered",
                    "--json",
                    "--path",
                    str(root),
                ]
            )
            == 0
        )
    finally:
        if old_org is None:
            __import__("os").environ.pop("SDAI_ORG_WORKFLOW_OVERLAY_PATH", None)
        else:
            __import__("os").environ["SDAI_ORG_WORKFLOW_OVERLAY_PATH"] = old_org
        if old_user is None:
            __import__("os").environ.pop("SDAI_USER_WORKFLOW_OVERLAY_PATH", None)
        else:
            __import__("os").environ["SDAI_USER_WORKFLOW_OVERLAY_PATH"] = old_user

    output = capsys.readouterr().out
    payload = json.loads(output)
    assert [item["layer"] for item in payload["overlays"]] == ["org", "repo", "user"]
    assert "\\" not in payload["overlays"][1]["source"]
