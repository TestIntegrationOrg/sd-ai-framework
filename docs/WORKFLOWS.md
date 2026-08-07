# Declarative Workflow Orchestration

SD-AI v0.3 introduces a typed workflow engine that can mix deterministic SDD steps, external AI-agent capabilities, human approval gates, and validation.

## Step types

### `deterministic`

Runs an SD-AI lifecycle action such as `specify`, `architect`, `plan`, `implement`, or `security`.

```yaml
- id: specification
  type: deterministic
  action: specify
```

### `agent`

Routes a lifecycle capability through the configured multi-agent platform.

```yaml
- id: architecture-review
  type: agent
  capability: architecture
  mode: advisory
  save_as: ai/architecture-review.md
```

Optional `profile` pins a provider profile for that step; otherwise capability routing applies.

```yaml
- id: implementation
  type: agent
  capability: coding
  profile: codex
  mode: workspace-write
```

Agent outputs are persisted under the feature workspace. Markdown outputs under `ai/` are added to the bounded context supplied to downstream agents. `save_as` is restricted to a relative path inside the feature workspace.

### `approval`

Pauses a workflow until an approval artifact exists.

```yaml
- id: architecture-approval
  type: approval
  gate: architecture
```

Grant the approval with:

```bash
sdai approve FEATURE-123 architecture --by "architect@example.com" --note "Reviewed design and risks"
```

The next workflow run resumes and skips completed steps. Approval steps are re-evaluated against the approval artifact on later runs; removing/revoking that artifact makes the workflow pause again.

### `validate`

Runs SD-AI validation using the workflow's `validation_mode`.

```yaml
validation_mode: critical
steps:
  - id: validate
    type: validate
```

`validation_mode` must be `light`, `standard`, or `critical`, even when the workflow has a custom name.

## Manual execution at any time

A core v0.3 rule is that workflow orchestration never removes manual control.

```bash
sdai step list FEATURE-123 --workflow agentic
sdai step run FEATURE-123 architecture-review --workflow agentic --dry-run
sdai step run FEATURE-123 architecture-review --workflow agentic
```

Deterministic and advisory/read-only agent steps can run without predecessor steps being marked complete. This supports targeted investigations, architecture experiments, recovery, review, and partial reruns.

For a manual `workspace-write` agent step, an earlier unsatisfied approval gate requires an explicit `--force` bypass:

```bash
sdai step run FEATURE-123 implementation --workflow agentic --force
```

This preserves the ability to run any step at any time while making a write-capable governance bypass explicit.

A completed step is not rerun accidentally:

```bash
sdai step run FEATURE-123 architecture-review --workflow agentic
# skipped: already completed

sdai step run FEATURE-123 architecture-review --workflow agentic --force
# intentionally reruns it
```

When `--force` reruns an already-completed step, SD-AI removes completion markers for that step and all downstream workflow steps. This prevents stale architecture, plans, reviews, tests, or validation results from remaining marked complete after an upstream artifact changes.

For an agent step, manual execution can also override provider routing or execution mode:

```bash
sdai step run FEATURE-123 architecture-review \
  --workflow agentic \
  --profile codex \
  --mode advisory \
  --force
```

`--dry-run` renders the effective prompt, skills, governance, feature context, profile, and provider without invoking the external agent.

## Persisted state

Workflow execution state is stored under:

```text
specs/<feature>/.sdai/workflows/<workflow>.yaml
```

Approval records are stored under:

```text
specs/<feature>/approvals/<gate>.yaml
```

The state contains completed step IDs and pause/failure position. Generated state is feature-scoped so it can be inspected and version controlled when a team chooses to do so.

## Backward compatibility

v0.1/v0.2 workflow files containing simple string step lists are still supported:

```yaml
name: standard
steps:
  - specify
  - architect
  - plan
  - implement
  - validate
```

They are interpreted as deterministic steps plus validation.

## Built-in `agentic` example

`sdai init` and `sdai upgrade` install an `agentic` workflow when it does not already exist. It demonstrates:

```text
Specification baseline
        ↓
AI requirements review
        ↓
Architecture baseline
        ↓
AI architecture review
        ↓
AI security review
        ↓
Human architecture approval
        ↓
Plan
        ↓
Codex implementation
        ↓
Copilot review
        ↓
Copilot testing
        ↓
Critical validation
```

The example is intentionally editable. Provider names are configuration, not control-plane dependencies.

## Custom workflows

Teams can create any `.sdai/workflows/<name>.yaml` file and run it directly:

```bash
sdai run FEATURE-123 --workflow my-enterprise-flow
```

Workflow names, step IDs, and approval gate identifiers are validated before they are used in generated paths.

This lets an organization encode separate workflows for API changes, security-sensitive features, data migrations, infrastructure changes, or architecture-critical initiatives without modifying the framework core.
