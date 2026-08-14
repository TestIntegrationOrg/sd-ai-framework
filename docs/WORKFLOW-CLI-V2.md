# Workflow Engine 2 machine APIs and CLI

SDAI 0.14 exposes canonical graph resolution and durable execution through one set of library contracts and `sdai workflow` commands. JSON is UTF-8, key-sorted, finite, and deterministic; stdout contains only the JSON document when `--json` is selected.

## Commands

```text
sdai workflow graph NAME [--input NAME=YAML_VALUE] [--json]
sdai workflow resolve NAME [--input NAME=YAML_VALUE] [--json]
sdai workflow validate NAME [--input NAME=YAML_VALUE] [--json]
sdai workflow explain NAME [--input NAME=YAML_VALUE] [--json]
sdai workflow status FEATURE --run RUN_ID [--json]
sdai workflow resume FEATURE --run RUN_ID [--input NAME=YAML_VALUE] [--json]
```

`graph` returns `sdai.workflow-graph/v2` plus its `graphSha256`. `resolve` returns `sdai.workflow-resolution/v2`, `resolutionSha256`, and deterministic leaf plans. `validate` returns `sdai.workflow-validation/v2`, binding the graph, resolution, node count, and plans. Existing pre-v9 `workflow explain` human and JSON fields remain compatible; v9 explain is a human-oriented view of the v2 resolution.

Inputs use YAML scalars or finite structured values. A sensitive declared input is represented by its sensitivity marker and SHA-256; its value is never emitted. Plugin plans expose plugin identity, effective permissions, input keys, and policy sources without input values. Safe-command plans expose direct executable structure, environment variable names, network denial, workspace-write requirement, policy sources, and a stable plan hash; environment values are never emitted.

## Durable status and resume

The library functions `inspect_workflow_run(...)` and `resume_workflow_run(...)` operate on the existing execution ledger. Status returns `sdai.workflow-run-status/v2` with:

- normalized and underlying ledger states;
- ledger/checkpoint hashes and current/stale/missing checkpoint state;
- output-redacted node records with output hashes and evidence references;
- task state in original registration order;
- structured branch, item, and iteration scopes derived from durable execution identities;
- the next executable, resumable, or re-evaluated task.

The status contract deliberately omits leaf output values. `workflow resume` resolves the workflow named by the immutable run manifest, supplies any explicit inputs, and calls the bounded executor. Its `sdai.workflow-resume-result/v2` response also redacts leaf outputs. The default adapter completes deterministic/validation leaves, pauses approval and quality-gate leaves, and blocks agent/plugin/safe-command leaves until a governed adapter is supplied through the library API.

A stale checkpoint remains inspectable as `checkpointStatus: stale`, but returns the invalid/unsafe exit class and is never used as trusted node state.

## Exit codes

| Code | Meaning |
|---:|---|
| `0` | Exact/valid resolution or succeeded execution. |
| `2` | Paused, blocked, active, or other action-required state. |
| `3` | Workflow or execution run not found. |
| `4` | Malformed, stale, invalid, or unsafe input/state. |
| `5` | Execution failed or was cancelled. |

For compatibility, legacy pre-v9 human-only `workflow validate` and `workflow explain` errors retain their historical exit code `1`. Automation should use `--json`, which returns the v2 error contract and the stable exit classes above.

Error JSON uses `sdai.workflow-error/v2` with a normalized category, stable SDAI code, message, and `errorSha256`. Errors do not mix human diagnostics into JSON stdout.
