from __future__ import annotations

import argparse
import json
from pathlib import Path

import yaml

from sdai.plugin_steps import execute_plugin_step
from sdai.workflows import StepKind, load_workflow


def add_workflow_parser(commands: argparse._SubParsersAction) -> None:
    workflow = commands.add_parser(
        "workflow",
        help="Validate and explain composed declarative workflows",
    )
    actions = workflow.add_subparsers(dest="workflow_action", required=True)

    validate = actions.add_parser("validate")
    validate.add_argument("name")
    validate.add_argument("--input", action="append", default=[])
    validate.add_argument("--json", action="store_true")
    validate.add_argument("--path")

    explain = actions.add_parser("explain")
    explain.add_argument("name")
    explain.add_argument("--input", action="append", default=[])
    explain.add_argument("--json", action="store_true")
    explain.add_argument("--path")


def _parse_input_values(values: list[str]) -> dict[str, object]:
    result: dict[str, object] = {}
    for raw in values:
        if "=" not in raw:
            raise ValueError("workflow --input must use NAME=YAML_VALUE")
        name, text = raw.split("=", 1)
        name = name.strip()
        if not name:
            raise ValueError("workflow --input name cannot be empty")
        if name in result:
            raise ValueError(f"workflow input '{name}' was supplied more than once")
        try:
            value = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise ValueError(f"workflow input '{name}' is invalid YAML: {exc}") from exc
        result[name] = value
    return result


def _plugin_plan_payload(plan) -> dict[str, object]:
    """Expose permission/provenance evidence without echoing plugin input values."""

    return {
        "plugin": {
            "id": plan.plugin.id,
            "version": plan.plugin.version,
            "publisher": plan.plugin.publisher,
            "executor": plan.plugin.executor,
            "source": plan.plugin.source,
        },
        "effective_permissions": plan.permissions.as_dict(),
        "policy_sources": list(plan.policy_sources),
        "input_keys": sorted(plan.inputs),
    }


def _prepare_plugin_plans(root: Path, definition) -> dict[str, object]:
    plans: dict[str, object] = {}
    for step, _ in definition.iter_steps():
        if step.kind is not StepKind.PLUGIN:
            continue
        if not step.plugin_id:
            raise ValueError(f"Plugin step '{step.id}' has no plugin id")
        plan, result = execute_plugin_step(
            root,
            step.plugin_id,
            step.id,
            step.plugin_input_values,
            dry_run=True,
        )
        assert result is None
        plans[step.id] = plan
    return plans


def _definition_payload(definition, plugin_plans: dict[str, object]) -> dict[str, object]:
    return {
        "version": 1,
        "name": definition.name,
        "workflow_version": definition.workflow_version,
        "validation_mode": definition.validation_mode.value,
        "inputs": [item.as_dict() for item in definition.input_definitions],
        "resolved_inputs": definition.input_values,
        "components": [item.as_dict() for item in definition.components],
        "inheritance": list(definition.inheritance),
        "overlays": [item.as_dict() for item in definition.overlays],
        "lifecycle_hooks": [item.as_dict() for item in definition.lifecycle_hooks],
        "mandatory_steps": list(definition.mandatory_steps),
        "plugin_plans": {
            step_id: _plugin_plan_payload(plan)
            for step_id, plan in sorted(plugin_plans.items())
        },
        "steps": [
            {
                "id": step.id,
                "type": step.kind.value,
                "parent": parent,
                "capability": step.capability.value if step.capability else None,
                "agent": step.agent_name,
                "mode": step.mode.value if step.capability else None,
                "gate": step.gate or step.quality_gate,
                "plugin": step.plugin_id,
            }
            for step, parent in definition.iter_steps()
        ],
    }


def run_workflow_command(root: Path, args: argparse.Namespace) -> int:
    inputs = _parse_input_values(args.input)
    definition = load_workflow(root, args.name, input_values=inputs)
    plugin_plans = _prepare_plugin_plans(root, definition)
    payload = _definition_payload(definition, plugin_plans)

    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False))
        return 0

    if args.workflow_action == "validate":
        print(
            f"Validated workflow '{definition.name}' version={definition.workflow_version or '-'} "
            f"steps={len(tuple(definition.iter_steps()))} components={len(definition.components)} "
            f"overlays={len(definition.overlays)} hooks={len(definition.lifecycle_hooks)} "
            f"plugins={len(plugin_plans)}"
        )
        return 0

    print(
        f"Workflow {definition.name} version={definition.workflow_version or '-'} "
        f"validation={definition.validation_mode.value}"
    )
    if definition.inheritance:
        print("  inheritance: " + " -> ".join(definition.inheritance))
    if definition.input_definitions:
        print("  inputs:")
        for item in definition.input_definitions:
            value = definition.input_values.get(item.name, "<unset>")
            print(
                f"    {item.name} type={item.type} required={str(item.required).lower()} "
                f"value={value!r}"
            )
    if definition.components:
        print("  components:")
        for item in definition.components:
            print(
                f"    {item.component_id}@{item.version} source={item.source} "
                f"steps={','.join(item.expanded_step_ids)}"
            )
    if definition.overlays:
        print("  overlays:")
        for item in definition.overlays:
            print(
                f"    {item.layer.value}:{item.overlay_id} target={item.target} "
                f"source={item.source}"
            )
    if definition.lifecycle_hooks:
        print("  lifecycle hooks:")
        for item in definition.lifecycle_hooks:
            print(
                f"    {item.point} anchor={item.anchor_step} "
                f"layer={item.layer.value} steps={','.join(item.step_ids)}"
            )
    if definition.mandatory_steps:
        print("  mandatory steps: " + ",".join(definition.mandatory_steps))
    if plugin_plans:
        print("  plugin plans:")
        for step_id, plan in sorted(plugin_plans.items()):
            print(
                f"    {step_id} plugin={plan.plugin.id}@{plan.plugin.version} "
                f"publisher={plan.plugin.publisher} executor={plan.plugin.executor} "
                f"policy={','.join(plan.policy_sources) or '-'}"
            )
    print("  steps:")
    for step, parent in definition.iter_steps():
        parent_text = f" parent={parent}" if parent else ""
        plugin_text = f" plugin={step.plugin_id}" if step.plugin_id else ""
        print(f"    {step.id} type={step.kind.value}{parent_text}{plugin_text}")
    return 0
