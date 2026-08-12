from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from sdai.agent_platform import ExecutionMode
from sdai.entrypoint import main as sdai_main
from sdai.extensions.scaffolding import ScaffoldKind, create_extension_scaffold
from sdai.workflows import WorkflowConfigError, load_workflow


def _init(root: Path) -> None:
    config = root / ".sdai" / "config.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("version: 1\n", encoding="utf-8")


def _component(
    root: Path,
    component_id: str,
    *,
    inputs: dict[str, object] | None = None,
    requires: list[str] | None = None,
    steps: list[object] | None = None,
    legacy_path: bool = False,
) -> Path:
    directory = (
        root / ".sdai" / "extensions" / "workflow-components"
        if legacy_path
        else root / ".sdai" / "workflow-components"
    )
    path = directory / f"{component_id}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "apiVersion": "sdai/v1",
        "kind": "WorkflowComponent",
        "metadata": {
            "id": component_id,
            "version": "1.0.0",
            "description": f"component {component_id}",
        },
        "spec": {
            "inputs": inputs or {},
            "requires": requires or [],
            "steps": steps
            or [
                {
                    "id": f"{component_id}-validate",
                    "type": "validate",
                }
            ],
        },
    }
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path


def _workflow(
    root: Path,
    name: str,
    *,
    version: object = 6,
    inputs: dict[str, object] | None = None,
    input_values: dict[str, object] | None = None,
    steps: list[object],
) -> Path:
    path = root / ".sdai" / "workflows" / f"{name}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {
        "version": version,
        "name": name,
        "validation_mode": "standard",
        "steps": steps,
    }
    if inputs is not None:
        payload["inputs"] = inputs
    if input_values is not None:
        payload["input_values"] = input_values
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path


def test_legacy_v5_workflow_behavior_and_profile_are_unchanged(tmp_path: Path) -> None:
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
                "save_as": "ai/review.md",
                "description": "literal legacy token ${{ not-an-input }} is not component syntax",
            },
            {"id": "validate", "type": "validate"},
        ],
    )

    definition = load_workflow(tmp_path, "legacy")

    assert definition.workflow_version == 5
    assert definition.components == ()
    assert definition.input_definitions == ()
    assert definition.step("review").profile == "codex"
    assert definition.step("review").description == (
        "literal legacy token ${{ not-an-input }} is not component syntax"
    )


def test_v6_component_expands_typed_inputs_before_existing_step_parser(tmp_path: Path) -> None:
    _component(
        tmp_path,
        "review-suite",
        inputs={
            "prefix": {"type": "string", "required": True},
            "write": {"type": "boolean", "default": False},
        },
        steps=[
            {
                "id": "${{ inputs.prefix }}-review",
                "type": "agent",
                "agent": "code-reviewer",
                "capability": "review",
                "mode": "advisory",
                "save_as": "ai/${{ inputs.prefix }}-review.md",
            },
            {"id": "${{ inputs.prefix }}-validate", "type": "validate"},
        ],
    )
    _workflow(
        tmp_path,
        "composed",
        inputs={"prefix": {"type": "string", "default": "service"}},
        steps=[
            {
                "uses": "component:review-suite",
                "with": {"prefix": "${{ inputs.prefix }}"},
            }
        ],
    )

    definition = load_workflow(tmp_path, "composed")

    assert definition.workflow_version == 6
    assert [step.id for step in definition.steps] == ["service-review", "service-validate"]
    review = definition.step("service-review")
    assert review.agent_name == "code-reviewer"
    assert review.mode is ExecutionMode.ADVISORY
    assert review.save_as == "ai/service-review.md"
    assert definition.components[0].component_id == "review-suite"
    assert definition.components[0].expanded_step_ids == (
        "service-review",
        "service-validate",
    )


def test_workflow_input_default_file_value_and_api_override_have_deterministic_precedence(
    tmp_path: Path,
) -> None:
    _component(
        tmp_path,
        "validate-one",
        inputs={"prefix": {"type": "string", "required": True}},
        steps=[{"id": "${{ inputs.prefix }}-validate", "type": "validate"}],
    )
    _workflow(
        tmp_path,
        "input-precedence",
        inputs={"prefix": {"type": "string", "default": "default"}},
        input_values={"prefix": "repo"},
        steps=[
            {
                "uses": "component:validate-one",
                "with": {"prefix": "${{ inputs.prefix }}"},
            }
        ],
    )

    assert load_workflow(tmp_path, "input-precedence").steps[0].id == "repo-validate"
    assert (
        load_workflow(
            tmp_path,
            "input-precedence",
            input_values={"prefix": "api"},
        ).steps[0].id
        == "api-validate"
    )


@pytest.mark.parametrize(
    "value",
    [True, 7, ["wrong"]],
)
def test_typed_component_input_rejects_wrong_value_shape(tmp_path: Path, value: object) -> None:
    _component(
        tmp_path,
        "typed",
        inputs={"prefix": {"type": "string", "required": True}},
        steps=[{"id": "typed-validate", "type": "validate"}],
    )
    _workflow(
        tmp_path,
        "typed-workflow",
        steps=[{"uses": "component:typed", "with": {"prefix": value}}],
    )

    with pytest.raises(WorkflowConfigError, match="SDAI-WFCOMP-003.*type 'string'"):
        load_workflow(tmp_path, "typed-workflow")


def test_unknown_and_invalid_enum_inputs_fail_deterministically(tmp_path: Path) -> None:
    _component(
        tmp_path,
        "enum-component",
        inputs={
            "level": {
                "type": "string",
                "required": True,
                "enum": ["standard", "critical"],
            }
        },
        steps=[{"id": "validate", "type": "validate"}],
    )
    _workflow(
        tmp_path,
        "enum-workflow",
        steps=[
            {
                "uses": "component:enum-component",
                "with": {"level": "regulated", "extra": "x"},
            }
        ],
    )

    with pytest.raises(WorkflowConfigError, match="SDAI-WFCOMP-003.*unknown input.*extra"):
        load_workflow(tmp_path, "enum-workflow")

    _workflow(
        tmp_path,
        "enum-workflow",
        steps=[
            {
                "uses": "component:enum-component",
                "with": {"level": "regulated"},
            }
        ],
    )
    with pytest.raises(WorkflowConfigError, match="SDAI-WFCOMP-003.*must be one of"):
        load_workflow(tmp_path, "enum-workflow")


def test_sensitive_inputs_are_redacted_from_component_provenance(tmp_path: Path) -> None:
    _component(
        tmp_path,
        "sensitive",
        inputs={
            "label": {"type": "string", "default": "safe"},
            "token": {"type": "string", "required": True, "sensitive": True},
        },
        steps=[{"id": "${{ inputs.label }}-validate", "type": "validate"}],
    )
    _workflow(
        tmp_path,
        "sensitive-workflow",
        steps=[
            {
                "uses": "component:sensitive",
                "with": {"token": "super-secret-value"},
            }
        ],
    )

    definition = load_workflow(tmp_path, "sensitive-workflow")
    component = definition.components[0]

    assert component.inputs["token"] == "<redacted>"
    assert "super-secret-value" not in json.dumps(component.as_dict())


@pytest.mark.parametrize("field", ["profile", "provider", "shell", "command", "exec", "argv"])
def test_reusable_component_rejects_provider_and_command_execution_fields(
    tmp_path: Path,
    field: str,
) -> None:
    step: dict[str, object] = {"id": "review", "type": "agent", "capability": "review"}
    step[field] = "codex" if field in {"profile", "provider"} else "danger"
    _component(tmp_path, "unsafe", steps=[step])
    _workflow(tmp_path, "unsafe-workflow", steps=[{"uses": "component:unsafe"}])

    with pytest.raises(WorkflowConfigError, match="SDAI-WFCOMP-005.*forbidden"):
        load_workflow(tmp_path, "unsafe-workflow")


def test_component_manifest_id_must_match_requested_file_identity(tmp_path: Path) -> None:
    path = _component(tmp_path, "review")
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    payload["metadata"]["id"] = "different"
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    _workflow(tmp_path, "mismatch", steps=[{"uses": "component:review"}])

    with pytest.raises(WorkflowConfigError, match="SDAI-WFCOMP-001.*filename/id mismatch"):
        load_workflow(tmp_path, "mismatch")


def test_component_expansion_cannot_create_duplicate_workflow_step_ids(tmp_path: Path) -> None:
    _component(
        tmp_path,
        "duplicate",
        steps=[{"id": "review", "type": "validate"}],
    )
    _workflow(
        tmp_path,
        "duplicates",
        steps=[
            {"id": "review", "type": "validate"},
            {"uses": "component:duplicate"},
        ],
    )

    with pytest.raises(WorkflowConfigError, match="duplicate step ids"):
        load_workflow(tmp_path, "duplicates")


def test_required_components_must_be_used_and_dependency_cycles_fail(tmp_path: Path) -> None:
    _component(tmp_path, "foundation")
    _component(tmp_path, "review", requires=["foundation"])
    _workflow(tmp_path, "missing-required", steps=[{"uses": "component:review"}])

    with pytest.raises(WorkflowConfigError, match="SDAI-WFCOMP-006.*missing required component.*foundation"):
        load_workflow(tmp_path, "missing-required")

    _component(tmp_path, "foundation", requires=["review"])
    _workflow(
        tmp_path,
        "cycle",
        steps=[
            {"uses": "component:foundation"},
            {"uses": "component:review"},
        ],
    )
    with pytest.raises(WorkflowConfigError, match="SDAI-WFCOMP-006.*cycle"):
        load_workflow(tmp_path, "cycle")


def test_component_expansion_still_obeys_parallel_advisory_only_safety(tmp_path: Path) -> None:
    _component(
        tmp_path,
        "bad-parallel",
        steps=[
            {
                "id": "parallel-review",
                "type": "parallel",
                "steps": [
                    {
                        "id": "writer",
                        "type": "agent",
                        "capability": "coding",
                        "agent": "developer",
                        "mode": "workspace-write",
                    }
                ],
            }
        ],
    )
    _workflow(tmp_path, "bad-parallel-workflow", steps=[{"uses": "component:bad-parallel"}])

    with pytest.raises(WorkflowConfigError, match="must use advisory mode"):
        load_workflow(tmp_path, "bad-parallel-workflow")


def test_component_syntax_requires_explicit_workflow_v6(tmp_path: Path) -> None:
    _component(tmp_path, "review")
    _workflow(tmp_path, "v5-component", version=5, steps=[{"uses": "component:review"}])

    with pytest.raises(WorkflowConfigError, match="require workflow version 6"):
        load_workflow(tmp_path, "v5-component")


def test_runtime_accepts_existing_v06_extension_scaffold_component_path(tmp_path: Path) -> None:
    result = create_extension_scaffold(
        tmp_path,
        ScaffoldKind.WORKFLOW_COMPONENT,
        "generated-review",
    )
    assert result.paths[0].relative_to(tmp_path).as_posix() == (
        ".sdai/extensions/workflow-components/generated-review.yaml"
    )
    _workflow(
        tmp_path,
        "generated-workflow",
        steps=[{"uses": "component:generated-review"}],
    )

    definition = load_workflow(tmp_path, "generated-workflow")

    assert definition.components[0].source == (
        ".sdai/extensions/workflow-components/generated-review.yaml"
    )
    assert definition.steps[0].id == "generated-review-validate"


def test_component_discovery_rejects_ambiguous_canonical_and_legacy_definitions(tmp_path: Path) -> None:
    _component(tmp_path, "ambiguous")
    _component(tmp_path, "ambiguous", legacy_path=True)
    _workflow(tmp_path, "ambiguous-workflow", steps=[{"uses": "component:ambiguous"}])

    with pytest.raises(WorkflowConfigError, match="SDAI-WFCOMP-001.*more than one location"):
        load_workflow(tmp_path, "ambiguous-workflow")


def test_workflow_explain_json_reports_provenance_and_redacts_sensitive_inputs(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "Enterprise Workspace Ω"
    root.mkdir()
    _init(root)
    _component(
        root,
        "review-suite",
        inputs={
            "prefix": {"type": "string", "default": "service"},
            "token": {"type": "string", "required": True, "sensitive": True},
        },
        steps=[
            {
                "id": "${{ inputs.prefix }}-review",
                "type": "agent",
                "agent": "code-reviewer",
                "capability": "review",
                "mode": "advisory",
                "save_as": "ai/café-${{ inputs.prefix }}.md",
            }
        ],
    )
    _workflow(
        root,
        "explainable",
        inputs={"prefix": {"type": "string", "default": "service"}},
        steps=[
            {
                "uses": "component:review-suite",
                "with": {
                    "prefix": "${{ inputs.prefix }}",
                    "token": "secret-value",
                },
            }
        ],
    )

    assert (
        sdai_main(
            [
                "workflow",
                "explain",
                "explainable",
                "--json",
                "--path",
                str(root),
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    payload = json.loads(output)

    assert payload["workflow_version"] == 6
    assert payload["components"][0]["component_id"] == "review-suite"
    assert payload["components"][0]["inputs"]["token"] == "<redacted>"
    assert "secret-value" not in output
    assert "\\" not in payload["components"][0]["source"]
