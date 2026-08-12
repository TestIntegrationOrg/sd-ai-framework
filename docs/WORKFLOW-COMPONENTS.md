# Reusable Workflow Components and Typed Inputs

SDAI 0.8 adds YAML-first workflow composition on top of the existing declarative workflow engine. Reusable components do not create a second runtime: component steps are expanded first and then parsed by the same workflow step parser that already enforces execution modes, approvals, quality gates, safe save paths, and advisory-only parallel execution.

## Backward compatibility

Existing v5 workflows remain valid and unchanged.

```yaml
version: 5
name: existing-workflow
validation_mode: standard
steps:
  - id: review
    type: agent
    capability: review
    agent: code-reviewer
    profile: codex
    mode: advisory
```

A v5 workflow with no typed inputs or component uses bypasses the v6 interpolation layer entirely. Existing strings containing `${{ ... }}` are therefore not reinterpreted as component expressions.

Typed inputs or `uses: component:<id>` require explicit workflow version 6 or newer.

## Component manifest

Workflow components use the shared `sdai/v1` extension envelope:

```yaml
apiVersion: sdai/v1
kind: WorkflowComponent
metadata:
  id: review-suite
  version: 1.0.0
  description: Reusable review and validation steps
spec:
  inputs:
    prefix:
      type: string
      required: true
  requires: []
  steps:
    - id: "${{ inputs.prefix }}-review"
      type: agent
      agent: code-reviewer
      capability: review
      mode: advisory
      save_as: "ai/${{ inputs.prefix }}-review.md"
    - id: "${{ inputs.prefix }}-validate"
      type: validate
```

## Component discovery

The roadmap-native repository location is:

```text
.sdai/workflow-components/<id>.yaml
```

For backward compatibility with SDAI 0.6 scaffolding, runtime discovery also accepts:

```text
.sdai/extensions/workflow-components/<id>.yaml
```

If the same component ID exists in both locations, SDAI fails closed rather than silently choosing one.

`sdai create workflow-component <id>` intentionally keeps the established 0.6 scaffold path and now emits a minimal reusable component body. Existing scripts/tests that rely on the scaffold path remain compatible.

## Workflow composition

A v6 workflow can consume a component with `uses: component:<id>`:

```yaml
version: 6
name: service-delivery
validation_mode: standard
inputs:
  prefix:
    type: string
    default: service
input_values:
  prefix: signing
steps:
  - uses: component:review-suite
    with:
      prefix: "${{ inputs.prefix }}"
```

The resolution order for workflow input values is:

```text
input declaration default
        ↓
workflow file input_values
        ↓
programmatic/CLI override
```

`load_workflow(..., input_values={...})` provides the programmatic override. The read-only CLI supports overrides for validation/explain:

```bash
sdai workflow validate service-delivery --input prefix=signing
sdai workflow explain service-delivery --input prefix=signing --json
```

This #75 slice does not add new execution flags to the legacy `sdai run` or `sdai step run` commands. Executed workflows use their YAML `input_values`/defaults. Workflow overlay/runtime input injection is expanded in the later overlay lifecycle work.

## Typed inputs

Supported v1 input types:

```text
string
integer
number
boolean
string-list
```

Input declarations support:

```yaml
inputs:
  level:
    type: string
    required: true
    enum: [standard, critical]
  retries:
    type: integer
    default: 2
  enabled:
    type: boolean
    default: true
  tags:
    type: string-list
    default: [api, signing]
  token:
    type: string
    required: true
    sensitive: true
```

Unknown, missing required, wrong-type, and enum-invalid values fail deterministically.

## Expressions

Exact expressions preserve the typed value:

```yaml
some_field: "${{ inputs.enabled }}"
```

Embedded expressions are permitted only for scalar inputs:

```yaml
id: "${{ inputs.prefix }}-review"
```

Lists/mappings cannot be embedded into strings. Unresolved or malformed expressions fail before the existing workflow parser runs.

Sensitive inputs are redacted from component provenance and workflow explain output. They must not be treated as an approval or secret-delivery mechanism; components have no shell/provider/command primitive in this slice.

## Component dependencies

A component may declare other components it expects the workflow to use:

```yaml
spec:
  requires: [foundation]
```

The workflow must explicitly use each required component. Missing, self, duplicate, or cyclic component dependencies fail with deterministic errors.

Nested `uses` inside a component is not supported in v1. Keeping composition at the workflow boundary makes expansion and provenance explicit and avoids hidden recursive execution behavior.

## Execution safety

Reusable component steps are intentionally stricter than normal repository workflows.

Component steps cannot contain:

```text
profile
provider
shell
command / commands
exec / executable
argv
```

This preserves SDAI's semantic-role/provider separation: reusable workflow behavior describes lifecycle work, while provider selection remains controlled by agent definitions, routing, policy, or explicit legacy workflow configuration.

There is no unrestricted shell primitive.

After expansion, every component step passes through the existing workflow parser. As a result:

- agent capabilities/modes must be valid
- `save_as` remains feature-workspace-relative
- parallel groups still support advisory agent children only
- workspace-write parallel children remain rejected
- approval and quality-gate semantics are unchanged
- duplicate IDs across direct/component/parallel steps fail

## Explainability

```bash
sdai workflow validate service-delivery
sdai workflow explain service-delivery
sdai workflow explain service-delivery --json
```

Explain output includes:

- workflow version and validation mode
- typed input declarations
- resolved non-sensitive values
- redacted sensitive values
- each component ID/version/source
- component dependency declarations
- expanded step IDs
- final parsed step types/parents/semantic agents/capabilities/modes/gates

The explain command is read-only and does not execute providers or mutate workflow state.

## Error codes

| Code | Meaning |
|---|---|
| `SDAI-WFCOMP-001` | malformed component/use/envelope/discovery contract |
| `SDAI-WFCOMP-002` | malformed typed-input declaration |
| `SDAI-WFCOMP-003` | missing/unknown/incompatible input value |
| `SDAI-WFCOMP-004` | invalid/unresolved interpolation expression |
| `SDAI-WFCOMP-005` | forbidden/unsupported component step content |
| `SDAI-WFCOMP-006` | component dependency/self/cycle requirement failure |

The next 0.8 slice (#76) builds inheritance/overlays and safe lifecycle hooks on this composition foundation. It must preserve the same organization non-weakening and provider/security boundaries.
