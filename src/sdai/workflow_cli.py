from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
import sys
from typing import Mapping

import yaml

from sdai.plugin_steps import execute_plugin_step
from sdai.policy import load_effective_configuration
from sdai.workflow_graph import WorkflowGraphResolution, load_workflow_graph
from sdai.workflow_machine import inspect_workflow_run, resume_workflow_run
from sdai.workflows import StepKind, WorkflowConfigError, load_workflow


WORKFLOW_ERROR_API_VERSION = "sdai.workflow-error/v2"
WORKFLOW_VALIDATION_API_VERSION = "sdai.workflow-validation/v2"
WORKFLOW_STEP_PLAN_API_VERSION = "sdai.workflow-step-plan/v2"
EXIT_EXACT = 0
EXIT_ACTION_REQUIRED = 2
EXIT_NOT_FOUND = 3
EXIT_INVALID_UNSAFE = 4
EXIT_EXECUTION_FAILED = 5


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

    for action_name in ("graph", "resolve"):
        action = actions.add_parser(action_name)
        action.add_argument("name")
        action.add_argument("--input", action="append", default=[])
        action.add_argument("--json", action="store_true")
        action.add_argument("--path")

    for action_name in ("status", "resume"):
        action = actions.add_parser(action_name)
        action.add_argument("feature")
        action.add_argument("--run", required=True, dest="run_id")
        if action_name == "resume":
            action.add_argument("--input", action="append", default=[])
        action.add_argument("--json", action="store_true")
        action.add_argument("--path")


def _canonical_json(payload: object) -> str:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _hash_json(payload: object) -> str:
    return "sha256:" + sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _emit_json(payload: object) -> None:
    print(_canonical_json(payload))


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
            raise ValueError(f"workflow input '{name}' is invalid YAML") from exc
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
        "manifest_sha256": plan.plugin.manifest_sha256,
        "plan_sha256": plan.sha256,
        "input_sha256": plan.input_sha256,
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
    sensitive = {
        item.name for item in definition.input_definitions if item.sensitive
    }
    public_inputs = {
        name: (
            {"sensitive": True, "sha256": _hash_json(value)}
            if name in sensitive
            else value
        )
        for name, value in sorted(definition.input_values.items())
    }
    return {
        "version": 1,
        "name": definition.name,
        "workflow_version": definition.workflow_version,
        "validation_mode": definition.validation_mode.value,
        "inputs": [item.as_dict() for item in definition.input_definitions],
        "resolved_inputs": public_inputs,
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


def _step_plans(
    root: Path,
    resolution: WorkflowGraphResolution,
) -> list[dict[str, object]]:
    policy = load_effective_configuration(root)
    result: list[dict[str, object]] = []
    control_kinds = {
        "sequence",
        "if",
        "switch",
        "parallel",
        "fan-out",
        "fan-in",
        "foreach",
        "bounded-while",
    }
    for node in resolution.graph.nodes:
        if node.path == resolution.graph.root or node.kind in control_kinds:
            continue
        permissions: dict[str, object] | None = None
        operational: object = node.config
        if node.kind == "safe-command":
            operational = node.config.get("operationalStep")
            if not isinstance(operational, Mapping):
                raise ValueError(f"safe-command node '{node.path}' has no operational contract")
            config = operational.get("config")
            if not isinstance(config, Mapping):
                raise ValueError(f"safe-command node '{node.path}' has invalid permissions")
            environment = config.get("environmentNames") or []
            if not isinstance(environment, list):
                raise ValueError(f"safe-command node '{node.path}' has invalid environment names")
            requested_write = config.get("requiresWorkspaceWrite") is True
            if requested_write and not policy.workspace_write:
                raise ValueError(
                    f"safe-command node '{node.path}' requires workspace-write but policy denies it"
                )
            denied_environment = []
            if policy.environment_allowlist is not None:
                denied_environment = sorted(
                    set(str(item) for item in environment)
                    - set(policy.environment_allowlist)
                )
            if denied_environment:
                raise ValueError(
                    f"safe-command node '{node.path}' environment denied by policy: "
                    + ", ".join(denied_environment)
                )
            permissions = {
                "network": False,
                "workspaceWrite": requested_write,
                "environmentNames": list(environment),
                "policySources": list(policy.sources),
            }
        elif node.kind == "plugin":
            plugin_id = node.config.get("plugin")
            input_keys = node.config.get("inputKeys") or []
            if not isinstance(plugin_id, str) or not isinstance(input_keys, list):
                raise ValueError(f"plugin node '{node.path}' has invalid graph metadata")
            plugin_plan, plugin_result = execute_plugin_step(
                root,
                plugin_id,
                node.id,
                resolution.plugin_inputs.get(
                    node.path,
                    {str(key): None for key in input_keys},
                ),
                dry_run=True,
            )
            assert plugin_result is None
            permissions = _plugin_plan_payload(plugin_plan)
        body: dict[str, object] = {
            "apiVersion": WORKFLOW_STEP_PLAN_API_VERSION,
            "nodePath": node.path,
            "stepId": node.id,
            "kind": node.kind,
            "operationalStep": operational,
            "permissions": permissions,
        }
        body["planSha256"] = _hash_json(body)
        result.append(body)
    return result


def _graph_payload(resolution: WorkflowGraphResolution) -> dict[str, object]:
    payload = resolution.graph.as_dict()
    payload["graphSha256"] = resolution.graph.sha256
    return payload


def _resolution_payload(
    root: Path,
    resolution: WorkflowGraphResolution,
) -> dict[str, object]:
    payload = resolution.as_dict()
    payload["resolutionSha256"] = resolution.sha256
    payload["stepPlans"] = _step_plans(root, resolution)
    return payload


def _validation_payload(
    root: Path,
    resolution: WorkflowGraphResolution,
) -> dict[str, object]:
    step_plans = _step_plans(root, resolution)
    body: dict[str, object] = {
        "apiVersion": WORKFLOW_VALIDATION_API_VERSION,
        "status": "valid",
        "workflow": resolution.name,
        "workflowVersion": resolution.workflow_version,
        "resolutionSha256": resolution.sha256,
        "graphSha256": resolution.graph.sha256,
        "nodeCount": len(resolution.graph.nodes),
        "stepPlans": step_plans,
    }
    body["validationSha256"] = _hash_json(body)
    return body


def _error_payload(category: str, exc: BaseException) -> dict[str, object]:
    message = str(exc)
    prefix, separator, detail = message.partition(":")
    code = prefix if separator and prefix.startswith("SDAI-") else "SDAI-WF2-CLI-001"
    body: dict[str, object] = {
        "apiVersion": WORKFLOW_ERROR_API_VERSION,
        "category": category,
        "error": {"code": code, "message": (detail.strip() if separator else message)},
    }
    body["errorSha256"] = _hash_json(body)
    return body


def _is_not_found(exc: BaseException) -> bool:
    text = str(exc).casefold()
    return isinstance(exc, FileNotFoundError) or any(
        phrase in text
        for phrase in (
            "does not exist",
            "not found",
            "no such file",
            "cannot find",
        )
    )


def _run_v2_workflow_command(root: Path, args: argparse.Namespace) -> int:
    action = args.workflow_action
    if action == "status":
        status = inspect_workflow_run(root, args.feature, args.run_id)
        if args.json:
            print(status.to_json(), end="")
        else:
            next_work = status.body.get("nextWork")
            print(
                f"Workflow run {status.body['runId']} status={status.status} "
                f"checkpoint={status.body['checkpointStatus']}"
            )
            print(f"  next: {next_work if next_work is not None else 'none'}")
        return status.exit_code

    if action == "resume":
        inputs = _parse_input_values(args.input)
        result = resume_workflow_run(
            root,
            args.feature,
            args.run_id,
            input_values=inputs,
        )
        if args.json:
            print(result.to_json(), end="")
        else:
            print(
                f"Workflow run {result.run_status.body['runId']} "
                f"status={result.execution.status.value}"
            )
            next_work = result.run_status.body.get("nextWork")
            if next_work is not None:
                print(f"  next: {next_work}")
        return result.exit_code

    inputs = _parse_input_values(args.input)
    resolution = load_workflow_graph(root, args.name, input_values=inputs)
    if action == "graph":
        payload = _graph_payload(resolution)
        if args.json:
            _emit_json(payload)
        else:
            print(
                f"Workflow graph {resolution.name} nodes={len(resolution.graph.nodes)} "
                f"edges={len(resolution.graph.edges)} sha256={resolution.graph.sha256}"
            )
            for node in resolution.graph.nodes:
                print(f"  {node.path} kind={node.kind} parent={node.parent or '-'}")
        return EXIT_EXACT
    if action == "resolve":
        payload = _resolution_payload(root, resolution)
        if args.json:
            _emit_json(payload)
        else:
            print(
                f"Resolved workflow {resolution.name} version={resolution.workflow_version or '-'} "
                f"graph={resolution.graph.sha256} resolution={resolution.sha256}"
            )
            print(
                f"  overlays={len(resolution.overlays)} components={len(resolution.components)} "
                f"step-plans={len(payload['stepPlans'])}"
            )
        return EXIT_EXACT
    raise ValueError(f"Unknown Workflow Engine 2 action: {action}")


def _run_legacy_explain(root: Path, args: argparse.Namespace) -> int:
    inputs = _parse_input_values(args.input)
    definition = load_workflow(root, args.name, input_values=inputs)
    plugin_plans = _prepare_plugin_plans(root, definition)
    payload = _definition_payload(definition, plugin_plans)

    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False))
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
            value = (
                "<redacted>"
                if item.sensitive and item.name in definition.input_values
                else definition.input_values.get(item.name, "<unset>")
            )
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


def _run_validate(root: Path, args: argparse.Namespace) -> int:
    inputs = _parse_input_values(args.input)
    resolution = load_workflow_graph(root, args.name, input_values=inputs)
    payload = _validation_payload(root, resolution)
    plugin_count = sum(1 for node in resolution.graph.nodes if node.kind == "plugin")
    try:
        definition = load_workflow(root, args.name, input_values=inputs)
    except WorkflowConfigError:
        if resolution.workflow_version is None or resolution.workflow_version < 9:
            raise
    else:
        compatibility = _definition_payload(
            definition,
            _prepare_plugin_plans(root, definition),
        )
        for key, value in compatibility.items():
            if key not in payload:
                payload[key] = value
    if args.json:
        _emit_json(payload)
    else:
        print(
            f"Validated workflow '{resolution.name}' version={resolution.workflow_version or '-'} "
            f"nodes={len(resolution.graph.nodes)} components={len(resolution.components)} "
            f"overlays={len(resolution.overlays)} hooks={len(resolution.lifecycle_hooks)} "
            f"plugins={plugin_count} graph={resolution.graph.sha256}"
        )
    return EXIT_EXACT


def run_workflow_command(root: Path, args: argparse.Namespace) -> int:
    try:
        if args.workflow_action in {"graph", "resolve", "status", "resume"}:
            return _run_v2_workflow_command(root, args)
        if args.workflow_action == "validate":
            return _run_validate(root, args)
        if args.workflow_action == "explain":
            inputs = _parse_input_values(args.input)
            resolution = load_workflow_graph(root, args.name, input_values=inputs)
            if resolution.workflow_version is not None and resolution.workflow_version >= 9:
                payload = _resolution_payload(root, resolution)
                if args.json:
                    _emit_json(payload)
                else:
                    print(
                        f"Workflow {resolution.name} version={resolution.workflow_version} "
                        f"validation={resolution.validation_mode} graph={resolution.graph.sha256}"
                    )
                    for node in resolution.graph.nodes:
                        print(f"  {node.path} type={node.kind}")
                return EXIT_EXACT
            return _run_legacy_explain(root, args)
        raise ValueError(f"Unknown workflow action: {args.workflow_action}")
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        category = "not-found" if _is_not_found(exc) else "invalid-unsafe"
        code = EXIT_NOT_FOUND if category == "not-found" else EXIT_INVALID_UNSAFE
        if args.workflow_action in {"validate", "explain"} and not getattr(args, "json", False):
            code = 1
        if getattr(args, "json", False):
            _emit_json(_error_payload(category, exc))
        else:
            print(f"error: {exc}", file=sys.stderr)
        return code
