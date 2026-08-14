# Workflow Engine 2 operational leaves

SDAI 0.14 normalizes executable and decision leaves behind the versioned `sdai.workflow-operational-step/v2` contract.

Supported canonical leaf kinds are `deterministic`, `agent`, `approval`, `validator`, `quality-gate`, `plugin`, and `safe-command`. Existing `validate` YAML remains accepted and normalizes to `validator`; existing deterministic/agent/approval/quality/plugin parsing remains authoritative.

## Safe command

`safe-command` is an explicit executable + argument-array declaration. It never accepts a shell command string, shell flag, script interpolation field, or template substitution. Dynamic input uses the already-reviewed Integration execution modes (`none`, `stdin`, `argument`, or ephemeral `file`) and therefore remains data rather than executable structure.

Example:

```yaml
id: inspect-json
type: safe-command
executable: python
args_before_input:
  - -X
  - utf8
  - -c
  - "import json,sys; print(json.dumps({'value': sys.stdin.read()}, ensure_ascii=False))"
input_mode: stdin
output_mode: json-stdout
timeout_seconds: 30
environment: []
workspace_write: false
cwd: .
```

The v2 contract intentionally requires `cwd: .`. Subdirectory execution is fail-closed until the shared execution boundary can preserve project-root containment and protected-path semantics for alternate working directories.

## Machine contracts

`WorkflowOperationalStep.to_json()` emits canonical compact UTF-8 JSON and a stable SHA-256. `build_workflow_leaf_plan()` emits `sdai.workflow-leaf-plan/v2`, binding the normalized step hash, input hash/length, and effective policy sources. Raw runtime input and environment values are not serialized in the plan.

`execute_safe_command_leaf()` adapts the workflow plan to SDAI's existing Integration execution engine. The workflow layer does not introduce another subprocess runner. It inherits direct argv / `shell=False`, UTF-8 decoding, JSON parsing, timeout handling, environment-name allowlisting, project containment, protected-path restoration, nonzero-exit normalization, and policy revalidation immediately before launch. A leaf with `workspace_write: false` is enforced as read-only across the whole project; any mutation is restored and normalized as a policy violation.

Results use `sdai.workflow-leaf-result/v2` and normalize status, exit code, output, and structured error metadata. Arbitrary command stderr is not copied into machine error metadata.

## Policy monotonicity

A leaf declaration can request workspace write or named environment variables, but it cannot grant those permissions. Planning fails when the effective SDAI policy denies a requirement, and execution revalidates policy so a plan cannot be used after policy becomes stricter.

Plugin leaves retain their reviewed PluginStep permission boundary. Their canonical operational contract includes plugin ID, input keys, and an input SHA-256; plugin input values are not copied into the operational-step JSON.

## Compatibility

This slice is additive. The legacy `WorkflowDefinition` loader and existing workflow orchestrator continue to own v0.1-v0.13 execution. Workflow Engine 2 control-flow execution consumes this operational-leaf contract in the later bounded-execution slice, avoiding a flag-day migration and preserving historical release gates.
