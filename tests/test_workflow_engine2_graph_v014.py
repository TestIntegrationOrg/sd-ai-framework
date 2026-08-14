from __future__ import annotations

import json
import math
from pathlib import Path

import pytest
import yaml

from sdai.workflow_graph import (
    MAX_CONCURRENCY,
    MAX_EXPRESSION_DEPTH,
    MAX_FAN_ITEMS,
    MAX_LOOP_ITERATIONS,
    WORKFLOW_GRAPH_API_VERSION,
    WORKFLOW_RESOLUTION_API_VERSION,
    WorkflowGraphError,
    evaluate_workflow_expression,
    load_workflow_graph,
    normalize_workflow_expression,
)
from sdai.workflows import load_workflow


def _workflow(
    root: Path,
    name: str,
    *,
    version: object | None = 9,
    steps: list[object],
    inputs: dict[str, object] | None = None,
    input_values: dict[str, object] | None = None,
) -> Path:
    path = root / ".sdai" / "workflows" / f"{name}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {
        "name": name,
        "validation_mode": "standard",
        "steps": steps,
    }
    if version is not None:
        payload["version"] = version
    if inputs is not None:
        payload["inputs"] = inputs
    if input_values is not None:
        payload["input_values"] = input_values
    path.write_text(
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
        newline="\n",
    )
    return path


def _component(root: Path, component_id: str) -> None:
    path = root / ".sdai" / "workflow-components" / f"{component_id}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "apiVersion": "sdai/v1",
        "kind": "WorkflowComponent",
        "metadata": {
            "id": component_id,
            "version": "1.0.0",
            "description": "compatibility component",
        },
        "spec": {
            "inputs": {"prefix": {"type": "string", "required": True}},
            "requires": [],
            "steps": [
                {"id": "${{ inputs.prefix }}-validate", "type": "validate"}
            ],
        },
    }
    path.write_text(
        yaml.safe_dump(payload, sort_keys=False),
        encoding="utf-8",
        newline="\n",
    )


def _comprehensive_steps() -> list[object]:
    return [
        {"id": "start", "type": "deterministic", "action": "specify"},
        {
            "id": "sequence-a",
            "type": "sequence",
            "steps": [
                {"id": "sequence-check", "type": "validate"},
                {"id": "sequence-gate", "type": "approval", "gate": "architecture"},
            ],
        },
        {
            "id": "release-if",
            "type": "if",
            "condition": {"eq": [{"ref": "inputs.release"}, True]},
            "then": [
                {"id": "release-yes", "type": "deterministic", "action": "plan"}
            ],
            "else": [
                {"id": "release-no", "type": "validate"}
            ],
        },
        {
            "id": "route",
            "type": "switch",
            "value": {"ref": "inputs.channel"},
            "cases": [
                {
                    "when": "prod",
                    "steps": [
                        {"id": "route-prod", "type": "approval", "gate": "release"}
                    ],
                },
                {
                    "when": "dev",
                    "steps": [
                        {"id": "route-dev", "type": "validate"}
                    ],
                },
            ],
            "default": [
                {"id": "route-default", "type": "quality-gate", "gate": "tests"}
            ],
        },
        {
            "id": "parallel-review",
            "type": "parallel",
            "max_concurrency": 2,
            "steps": [
                {"id": "parallel-a", "type": "deterministic", "action": "architect"},
                {"id": "parallel-b", "type": "validate"},
            ],
        },
        {
            "id": "fan",
            "type": "fan-out",
            "items": {"literal": ["api", "web"]},
            "as": "target",
            "max_items": 4,
            "max_concurrency": 2,
            "steps": [
                {"id": "fan-review", "type": "validate"}
            ],
        },
        {
            "id": "each",
            "type": "foreach",
            "items": {"ref": "inputs.targets"},
            "as": "target",
            "max_items": 5,
            "steps": [
                {"id": "each-review", "type": "validate"}
            ],
        },
        {
            "id": "retry-loop",
            "type": "bounded-while",
            "condition": {"lt": [{"ref": "loop.iteration"}, 2]},
            "max_iterations": 3,
            "steps": [
                {"id": "loop-check", "type": "validate"}
            ],
        },
        {
            "id": "join",
            "type": "fan-in",
            "sources": ["parallel-review", "fan"],
            "strategy": "all-success",
        },
        {"id": "finish", "type": "validate"},
    ]


def test_v9_control_flow_resolves_to_stable_canonical_graph_with_bounds_and_paths(
    tmp_path: Path,
) -> None:
    _workflow(
        tmp_path,
        "control",
        inputs={
            "release": {"type": "boolean", "required": True},
            "channel": {"type": "string", "default": "prod"},
            "targets": {"type": "string-list", "default": ["api", "web"]},
            "token": {"type": "string", "required": True, "sensitive": True},
        },
        steps=_comprehensive_steps(),
    )

    first = load_workflow_graph(
        tmp_path,
        "control",
        input_values={"release": True, "token": "sëcret-Δ"},
    )
    second = load_workflow_graph(
        tmp_path,
        "control",
        input_values={"release": True, "token": "sëcret-Δ"},
    )

    assert first.to_json() == second.to_json()
    assert first.sha256 == second.sha256
    assert first.graph.to_json() == second.graph.to_json()
    assert first.graph.sha256 == second.graph.sha256
    assert first.as_dict()["apiVersion"] == WORKFLOW_RESOLUTION_API_VERSION
    assert first.graph.as_dict()["apiVersion"] == WORKFLOW_GRAPH_API_VERSION
    assert first.workflow_version == 9
    assert first.validation_mode == "standard"
    assert "sëcret-Δ" not in first.to_json()
    assert first.public_inputs["token"]["sensitive"] is True

    paths = {node.path for node in first.graph.nodes}
    assert "$root" in paths
    assert "sequence-a/sequence-check" in paths
    assert "release-if/$then/release-yes" in paths
    assert "release-if/$else/release-no" in paths
    assert "route/$case/0/route-prod" in paths
    assert "route/$default/route-default" in paths
    assert "fan/$body/fan-review" in paths
    assert "each/$body/each-review" in paths
    assert "retry-loop/$body/loop-check" in paths

    parallel = first.graph.node("parallel-review")
    assert parallel.config["maxConcurrency"] == 2
    fan = first.graph.node("fan")
    assert fan.config["maxItems"] == 4
    assert fan.config["maxConcurrency"] == 2
    loop = first.graph.node("retry-loop")
    assert loop.config["maxIterations"] == 3
    join = first.graph.node("join")
    assert join.config["strategy"] == "all-success"

    fan_in_edges = {
        (edge.source, edge.target)
        for edge in first.graph.edges
        if edge.kind == "fan-in"
    }
    assert fan_in_edges == {("parallel-review", "join"), ("fan", "join")}


def test_expression_dsl_is_non_code_deterministic_and_supports_refs_logic_and_exists(
    tmp_path: Path,
) -> None:
    injection = "$(touch should-not-exist); __import__('os').system('false')"
    expression = {
        "and": [
            {"eq": [{"ref": "inputs.command"}, injection]},
            {"gte": [{"ref": "inputs.count"}, 2]},
            {"in": ["api", {"ref": "inputs.targets"}]},
            {"exists": "steps.review.status"},
            {"not": {"eq": [{"ref": "inputs.mode"}, "unsafe"]}},
        ]
    }
    context = {
        "inputs": {
            "command": injection,
            "count": 3,
            "targets": ["api", "web"],
            "mode": "safe",
        },
        "steps": {"review": {"status": "succeeded"}},
        "item": {},
        "loop": {},
    }

    normalized = normalize_workflow_expression(expression)
    assert evaluate_workflow_expression(normalized, context) is True
    assert not (tmp_path / "should-not-exist").exists()
    assert evaluate_workflow_expression({"exists": "steps.missing.status"}, context) is False
    assert evaluate_workflow_expression({"literal": {"café": "Δ"}}, context) == {"café": "Δ"}

    with pytest.raises(WorkflowGraphError, match="SDAI-WF2-004.*unsupported operator"):
        normalize_workflow_expression({"python": "__import__('os')"})
    with pytest.raises(WorkflowGraphError, match="SDAI-WF2-004.*exactly one key"):
        normalize_workflow_expression({"eq": [1, 1], "ne": [1, 2]})
    with pytest.raises(WorkflowGraphError, match="SDAI-WF2-004.*dotted reference"):
        normalize_workflow_expression({"ref": "__class__.__mro__"})
    with pytest.raises(WorkflowGraphError, match="SDAI-WF2-001.*finite"):
        normalize_workflow_expression(math.nan)

    too_deep: object = True
    for _ in range(MAX_EXPRESSION_DEPTH + 2):
        too_deep = {"not": too_deep}
    with pytest.raises(WorkflowGraphError, match="SDAI-WF2-004.*maximum expression depth"):
        normalize_workflow_expression(too_deep)


def test_control_flow_requires_v9_and_explicit_finite_bounds(tmp_path: Path) -> None:
    _workflow(
        tmp_path,
        "old-if",
        version=8,
        steps=[
            {
                "id": "condition",
                "type": "if",
                "condition": True,
                "then": [{"id": "validate", "type": "validate"}],
            }
        ],
    )
    with pytest.raises(WorkflowGraphError, match="SDAI-WF2-002.*version 9"):
        load_workflow_graph(tmp_path, "old-if")

    invalid_steps = [
        (
            "fan-missing",
            {
                "id": "fan",
                "type": "fan-out",
                "items": ["a"],
                "max_concurrency": 1,
                "steps": [{"id": "validate", "type": "validate"}],
            },
            "max_items must be an integer",
        ),
        (
            "fan-too-many",
            {
                "id": "fan",
                "type": "fan-out",
                "items": ["a"],
                "max_items": MAX_FAN_ITEMS + 1,
                "max_concurrency": 1,
                "steps": [{"id": "validate", "type": "validate"}],
            },
            f"between 1 and {MAX_FAN_ITEMS}",
        ),
        (
            "while-missing",
            {
                "id": "loop",
                "type": "bounded-while",
                "condition": True,
                "steps": [{"id": "validate", "type": "validate"}],
            },
            "max_iterations must be an integer",
        ),
        (
            "while-too-large",
            {
                "id": "loop",
                "type": "bounded-while",
                "condition": True,
                "max_iterations": MAX_LOOP_ITERATIONS + 1,
                "steps": [{"id": "validate", "type": "validate"}],
            },
            f"between 1 and {MAX_LOOP_ITERATIONS}",
        ),
        (
            "parallel-concurrency",
            {
                "id": "parallel",
                "type": "parallel",
                "max_concurrency": MAX_CONCURRENCY + 1,
                "steps": [{"id": "validate", "type": "validate"}],
            },
            f"between 1 and {MAX_CONCURRENCY}",
        ),
    ]
    for name, step, message in invalid_steps:
        _workflow(tmp_path, name, steps=[step])
        with pytest.raises(WorkflowGraphError, match=message):
            load_workflow_graph(tmp_path, name)


def test_duplicate_ids_switch_cases_and_missing_fan_in_sources_fail_closed(tmp_path: Path) -> None:
    _workflow(
        tmp_path,
        "duplicate",
        steps=[
            {
                "id": "condition",
                "type": "if",
                "condition": True,
                "then": [{"id": "same", "type": "validate"}],
                "else": [{"id": "same", "type": "validate"}],
            }
        ],
    )
    with pytest.raises(WorkflowGraphError, match="SDAI-WF2-002.*duplicate step id 'same'"):
        load_workflow_graph(tmp_path, "duplicate")

    _workflow(
        tmp_path,
        "duplicate-case",
        steps=[
            {
                "id": "switch",
                "type": "switch",
                "value": {"ref": "inputs.mode"},
                "cases": [
                    {"when": "prod", "steps": [{"id": "one", "type": "validate"}]},
                    {"when": "prod", "steps": [{"id": "two", "type": "validate"}]},
                ],
            }
        ],
        inputs={"mode": {"type": "string", "default": "prod"}},
    )
    with pytest.raises(WorkflowGraphError, match="SDAI-WF2-003.*duplicate case"):
        load_workflow_graph(tmp_path, "duplicate-case")

    _workflow(
        tmp_path,
        "missing-fan-in",
        steps=[
            {"id": "start", "type": "validate"},
            {"id": "join", "type": "fan-in", "sources": ["does-not-exist"]},
        ],
    )
    with pytest.raises(WorkflowGraphError, match="SDAI-WF2-005.*missing"):
        load_workflow_graph(tmp_path, "missing-fan-in")


def test_legacy_v1_v6_component_and_v8_plugin_workflows_remain_graph_compatible(
    tmp_path: Path,
) -> None:
    _workflow(
        tmp_path,
        "legacy",
        version=None,
        steps=["specify", "architect", "plan", "validate"],
    )
    legacy_definition = load_workflow(tmp_path, "legacy")
    legacy_graph = load_workflow_graph(tmp_path, "legacy")
    assert [step.id for step in legacy_definition.steps] == [
        "specify",
        "architect",
        "plan",
        "validate",
    ]
    assert [node.id for node in legacy_graph.graph.nodes if node.path != "$root"] == [
        "architect",
        "plan",
        "specify",
        "validate",
    ]

    _component(tmp_path, "validation")
    _workflow(
        tmp_path,
        "component",
        version=6,
        inputs={"prefix": {"type": "string", "default": "service"}},
        steps=[
            {
                "uses": "component:validation",
                "with": {"prefix": "${{ inputs.prefix }}"},
            }
        ],
    )
    component_definition = load_workflow(tmp_path, "component")
    component_graph = load_workflow_graph(tmp_path, "component")
    assert component_definition.steps[0].id == "service-validate"
    assert component_graph.graph.node("service-validate").kind == "validate"
    assert component_graph.components[0]["component_id"] == "validation"

    _workflow(
        tmp_path,
        "plugin-v8",
        version=8,
        steps=[
            {
                "id": "custom-check",
                "type": "plugin",
                "plugin": "example-check",
                "inputs": {"message": "café Δ"},
            }
        ],
    )
    plugin_definition = load_workflow(tmp_path, "plugin-v8")
    plugin_graph = load_workflow_graph(tmp_path, "plugin-v8")
    assert plugin_definition.steps[0].kind.value == "plugin"
    node = plugin_graph.graph.node("custom-check")
    assert node.kind == "plugin"
    assert node.config["plugin"] == "example-check"
    assert node.config["inputKeys"] == ["message"]
    assert "café Δ" not in plugin_graph.to_json()


def test_graph_json_is_compact_canonical_utf8_and_rejects_invalid_workflow_shapes(
    tmp_path: Path,
) -> None:
    _workflow(
        tmp_path,
        "utf8",
        steps=[
            {
                "id": "gate",
                "type": "if",
                "condition": {"eq": ["café", "café"]},
                "then": [{"id": "done", "type": "validate", "description": "Δ"}],
            }
        ],
    )
    resolution = load_workflow_graph(tmp_path, "utf8")
    payload = resolution.to_json()
    assert "café" in payload
    assert "Δ" in payload
    assert "\n" not in payload
    assert json.dumps(json.loads(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=False) == payload

    _workflow(
        tmp_path,
        "unknown-control-field",
        steps=[
            {
                "id": "seq",
                "type": "sequence",
                "steps": [{"id": "done", "type": "validate"}],
                "shell": "echo unsafe",
            }
        ],
    )
    with pytest.raises(WorkflowGraphError, match="SDAI-WF2-003.*unsupported field.*shell"):
        load_workflow_graph(tmp_path, "unknown-control-field")
