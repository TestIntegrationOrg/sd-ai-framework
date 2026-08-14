# Workflow Engine 2 Inheritance, Overlays, and Safe Lifecycle Hooks

SDAI 0.8 introduced the compatibility pipeline below. SDAI 0.14 extends its overlay stage to edit nested Workflow Engine 2 graphs without changing the established layer order:

```text
base workflow / inheritance
        ↓
organization overlays
        ↓
repository overlays
        ↓
user overlays
        ↓
safe lifecycle-hook insertion
        ↓
workflow-component expansion
        ↓
existing workflow step parser
        ↓
existing policy/provider/orchestrator runtime
```

This ordering makes extension provenance deterministic while preserving the existing execution and security boundaries.

## Workflow inheritance

A derived workflow can extend another repository workflow without copying its steps:

```yaml
version: 7
name: payments
extends: enterprise
validation_mode: critical
steps:
  - id: payments-review
    type: agent
    agent: code-reviewer
    capability: review
    mode: advisory
```

Inheritance is additive:

- parent steps are retained and child steps are appended
- `inputs`, `input_values`, and `lifecycle` mappings are merged
- child values override the same mapping key
- validation mode may be strengthened but not lowered
- inheritance cycles fail deterministically
- inherited/component/overlay step-ID collisions still fail in the existing workflow parser

When a derived workflow extends `enterprise`, organization overlays targeted at `enterprise` also apply to the derived workflow. This prevents inheritance from bypassing organization controls.

## Overlay sources

Overlay precedence is:

```text
organization → repository → user
```

Organization overlays are loaded from an absolute file or directory path:

```text
SDAI_ORG_WORKFLOW_OVERLAY_PATH
```

Repository overlays live under:

```text
.sdai/workflow-overlays/*.yaml
```

Optional user overlays are loaded from:

```text
SDAI_USER_WORKFLOW_OVERLAY_PATH
```

External org/user paths must be absolute and must not be symlinks. Repository overlay files must remain inside the project and must be regular non-symlink files.

## Overlay format

```yaml
version: 1
id: org-security
workflow: enterprise
required_steps:
  - org-security-approval
operations:
  - op: insert-before
    target: delivery-approval
    step:
      id: org-security-approval
      type: approval
      gate: org-security
hooks:
  before:architecture:
    - id: org-architecture-policy
      type: agent
      agent: security-reviewer
      capability: security
      mode: advisory
```

Canonical Workflow Engine 2 operation types are:

```text
insert-before
insert-after
replace
remove
```

Legacy `prepend`, `append`, `add-before`, `add-after`, and `disable` operations remain compatible; `add-before`/`add-after`/`disable` normalize to `insert-before`/`insert-after`/`remove`.

Targets may use a globally unambiguous step ID or an exact canonical path such as `pipeline/checks/unit`, `decision/$then/review`, `selector/$case/0/validate`, or `loop/$body/build`. Ambiguous IDs fail with the candidate canonical paths. `replace` must retain the target step ID. Multiple replace/remove mutations of the same canonical target in one layer fail even when one uses an ID and another uses a path.

Documents are ordered by layer, inheritance target, and overlay ID rather than filename. Each operation is applied to an isolated graph copy, and any invalid operation rejects the complete resolution. This makes successful output independent of source enumeration/filename order and prevents partial application.

## Organization non-weakening

Organization overlays are authoritative.

The following automatically become organization-mandated:

- steps added by organization operations
- steps replaced by organization operations
- IDs listed in `required_steps`
- organization lifecycle-hook steps
- anchors required by organization lifecycle hooks

Repository and user overlays cannot replace or disable those steps.

The same rule applies to a parent control node containing a mandatory step; removing or replacing the parent cannot erase the nested requirement indirectly.

`required_steps` is accepted only from the organization layer.

## Protected workflow semantics

Even when no explicit organization overlay exists, repository/user overlays cannot disable or replace these protected existing steps:

- approval steps
- quality-gate steps
- validation steps
- parallel/fan-out groups
- security-capability agent steps

For other replaceable agent steps, lower layers cannot:

- change the lifecycle capability
- change the semantic agent identity
- widen `advisory` execution to `workspace-write`

A lower layer may make a `workspace-write` agent more restrictive by replacing it with the same agent/capability in `advisory` mode.

Overlay operations cannot introduce a workspace-writing branch beneath a concurrent `parallel` or `fan-out` node, including insertion into an existing concurrent node.

For deterministic steps, lower layers cannot replace the action with a different action.

## Provider and shell separation

Overlay step definitions cannot contain:

```text
profile
provider
shell
command / commands
exec / executable
argv
```

Overlay files therefore cannot introduce provider pinning or an unrestricted command/shell primitive. Provider/model choice remains controlled by semantic agent routing, provider profiles, and enterprise policy.

The final effective steps still pass through the existing workflow parser and orchestrator governance.

## Lifecycle anchors

A workflow that accepts lifecycle hooks declares explicit anchor step IDs:

```yaml
lifecycle:
  requirements: requirements-review
  architecture: architecture-review
  implementation: implementation
  verify: validate
  delivery: delivery-approval
  pr: create-pr
```

Supported hook points are:

```text
before:requirements
after:requirements
before:architecture
after:architecture
before:implementation
after:implementation
before:verify
after:verify
before:delivery
after:delivery
before:pr
after:pr
```

A hook fails closed if its workflow does not declare the required lifecycle anchor.

## Safe hook steps

Lifecycle hooks are intentionally narrower than general workflow steps. They may contain only:

- advisory agent steps
- approval steps
- quality-gate steps
- validation steps

Hooks cannot run workspace-writing agents, deterministic mutation actions, shell/commands, provider-pinned steps, or nested component uses.

Organization hook steps and their anchor steps become mandatory so lower layers cannot silently remove the control point.

Repository hooks protect their anchors from later user-layer mutation as well.

## Overlay/inheritance versioning

Workflows that use inheritance or have an applied overlay are resolved as effective workflow version 7 or newer. Existing v5/v6 workflows without these features continue to load through their prior compatibility paths.

## Explainability

The existing read-only workflow commands now include inheritance and overlay provenance:

```bash
sdai workflow validate enterprise
sdai workflow explain enterprise
sdai workflow explain enterprise --json
```

Explain output includes:

- inheritance chain
- effective validation mode/version
- organization-mandated step IDs
- overlay layer, ID, target, source, and operations
- per-operation canonical target plus pre/post resolved-graph SHA-256, and overlay-level pre/post graph hashes
- lifecycle hook point, anchor, layer, source, and inserted step IDs
- typed inputs and components from #75
- final parsed workflow steps

No provider is executed by `workflow validate` or `workflow explain`.

## Error families

| Code | Meaning |
|---|---|
| `SDAI-WFOVER-001` | malformed overlay/source/document contract |
| `SDAI-WFOVER-002` | inheritance/version/validation-mode contract failure |
| `SDAI-WFOVER-003` | invalid/ambiguous overlay step operation |
| `SDAI-WFOVER-004` | organization/protected-step non-weakening violation |
| `SDAI-WFOVER-005` | overlay target step not found |
| `SDAI-WFOVER-006` | lifecycle anchor/inheritance resolution failure |
| `SDAI-WFOVER-007` | unsafe/unsupported lifecycle hook |
| `SDAI-WFOVER-008` | unsafe overlay source path |

This slice does not add new executable plugin step types. The next plugin-permission milestone owns the permission contract for genuinely new runtime mechanisms.
