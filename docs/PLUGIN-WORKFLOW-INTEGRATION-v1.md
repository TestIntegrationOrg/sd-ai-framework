# PluginStep Workflow Integration v1

Issue #85 exposes the reviewed PluginStep permission SDK as a declarative workflow step. It does not add a second execution path, generic shell step, module import, or provider-specific plugin mechanism.

## Workflow contract

Plugin steps require explicit workflow version 8 or newer:

```yaml
version: 8
name: secure-scan
validation_mode: standard
steps:
  - id: scan
    type: plugin
    plugin: trivy-adapter
    inputs:
      target: src
    retry:
      max_attempts: 2
      delay_seconds: 1
    on_failure: stop
```

Allowed fields are limited to:

- `id`
- `type` / `kind`
- `plugin`
- `inputs`
- `description`
- `if` / `condition`
- `retry`
- `on_failure`

Plugin workflow records cannot name a provider/profile, execution mode, shell/command/argv, executable, module/callable, or output path. Those capabilities remain owned by the reviewed PluginStep manifest/policy/runtime boundary.

Reusable workflow components may contain PluginStep records, but the final expanded workflow still requires version 8 and every expanded plugin is independently policy-prepared. Organization plugin denial therefore cannot be bypassed by hiding a PluginStep inside a component.

Workflow overlays and lifecycle hooks cannot introduce PluginStep execution in v1. Overlay/hook step allowlists continue to accept only their previously reviewed step types.

## Validation and explain

`sdai workflow validate` and `sdai workflow explain` perform SDK dry-run preparation for every effective PluginStep. Validation therefore fails when a plugin is missing, malformed, denied, published by an untrusted publisher, or requests permissions outside effective policy.

No registered executor is required for validation/dry-run, and no plugin side effects or feature evidence are written.

Explain output includes:

- plugin ID/version/publisher/executor/source
- effective permissions
- policy provenance
- plugin input **keys**

It intentionally does not echo plugin input values.

## Orchestrator execution

The orchestrator executes a PluginStep only through `prepare_plugin_step()` / `execute_plugin_step()` from the reviewed SDK.

Execution order is:

```text
workflow condition/state checks
        ↓
SDK prepare + effective plugin policy
        ↓
if workspace-write: existing SDAI prior-approval policy
        ↓
SDK trusted executor lookup + permission-checked services
        ↓
retry/backoff using workflow step policy
        ↓
structured PluginResult
        ↓
framework-owned evidence + workflow state
```

A plugin requesting `workspace_write` is subject to the same organization-level prior-approval rule used for AI workspace-write steps. A trusted executor is not called until that approval requirement is satisfied.

`PluginResult(status="failed")` is a failed attempt and participates in retry/backoff. After retries are exhausted, the existing workflow `on_failure: stop|continue` behavior determines whether later workflow steps run.

## Evidence

Real executions write framework-owned evidence to:

```text
specs/<FEATURE>/plugin/<STEP-ID>.json
```

The evidence includes:

- feature/workflow/step identity
- plugin identity/version/publisher/executor/source
- effective permissions
- plugin-policy provenance
- plugin input keys
- SHA-256 of the complete prepared plan
- attempt count
- structured result or final error

Raw plugin input values are not persisted in the plan evidence. The structured plugin result is retained because it is the executor's declared engineering evidence/output.

The plugin SDK itself protects `specs/**` from framework-mediated plugin writes, so the PluginStep cannot rewrite its framework-owned evidence through `PluginExecutionServices`.

## Security invariants

- no generic workflow shell primitive
- no direct workflow subprocess execution
- no provider/profile pin on PluginStep
- no YAML module/callable loading
- no lifecycle-hook PluginStep injection in v1
- component expansion cannot bypass plugin policy
- organization deny/trust/permission policy is reevaluated by the SDK at execution
- dry-run is side-effect free and does not require the executor
- real execution requires an already registered trusted executor
- protected-path, symlink, command-path, finite-JSON, and network-deny behavior remains owned by the reviewed SDK
