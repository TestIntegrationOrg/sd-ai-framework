# SD-AI Framework

**One open-source framework for Spec-Driven Development (SDD) + AI-Driven Development Life Cycle (AI-DLC).**

SD-AI treats approved specifications and architecture artifacts as the source of truth. Deterministic lifecycle steps, interchangeable AI agents, approval gates, quality gates, and enterprise integrations operate around that source of truth.

> Project status: **0.4.0 / enterprise integrations and governance**.

## Lifecycle

```text
Business / Jira / GitHub Issue
            ↓
      Feature Intake
            ↓
      Specification
            ↓
  Architecture + ADRs
            ↓
 ┌──────────┴──────────┐
 │ Parallel AI Reviews │
 │ Architecture/Security
 └──────────┬──────────┘
            ↓
   Human Approval Gates
            ↓
        Plan / Tasks
            ↓
  AI Workspace Implementation
            ↓
 ┌──────────┴──────────┐
 │ Parallel Code/Test  │
 │ Review Agents       │
 └──────────┬──────────┘
            ↓
 Test / Trivy / Sonar Gates
            ↓
 Spec + Architecture Validation
            ↓
        GitHub PR
```

## Principles

1. **Spec first** — implementation and tests trace to approved requirements.
2. **Architecture as code** — architecture, ADRs, Mermaid/C4 and contracts live in Git.
3. **Provider neutral** — Codex, Copilot, Claude, Gemini and custom agents sit behind adapters.
4. **Skills and prompts are code** — reusable agent behavior is version controlled.
5. **Least privilege** — advisory mode is read-only; workspace writes are explicit.
6. **Human governance** — risky workflows can pause for role-backed approvals.
7. **Manual control remains available** — any named top-level workflow step can be run independently.
8. **Policy is reviewable** — workflow and approval rules live under `.sdai/`.
9. **Validation closes the loop** — quality gates and spec/architecture checks run before delivery.

## Quick start

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -e .

sdai init
sdai feature FEATURE-123 \
  --title "Add governed signing workflow" \
  --description "Implement the feature with architecture and security review" \
  --workflow enterprise

sdai step list FEATURE-123 --workflow enterprise
sdai run FEATURE-123 --workflow enterprise
```

Upgrade an existing SD-AI project without overwriting team customizations:

```bash
sdai upgrade
```

## Multi-agent support

Built-in adapters support:

- Codex
- GitHub Copilot CLI
- Claude Code
- Gemini CLI
- custom/local command agents
- Python provider plugins through the `sdai.providers` entry-point group

Default routing remains configurable:

```yaml
routes:
  requirements: claude
  architecture: claude
  planning: codex
  coding: codex
  review: copilot
  testing: copilot
  security: claude
  documentation: gemini
```

Override any agent manually:

```bash
sdai agents run architecture FEATURE-123 --profile codex --dry-run
sdai agents run coding FEATURE-123 --profile claude --mode workspace-write
```

## v0.4 workflow features

Workflow YAML supports deterministic steps, AI-agent steps, approvals, quality gates, validation, conditions, retries, failure handling, and bounded parallel advisory agents.

```yaml
version: 4
name: enterprise
validation_mode: critical
steps:
  - id: specification
    type: deterministic
    action: specify

  - id: design-reviews
    type: parallel
    steps:
      - id: architecture-review
        type: agent
        capability: architecture
        profile: claude
        mode: advisory
      - id: security-review
        type: agent
        capability: security
        profile: copilot
        mode: advisory

  - id: architecture-approval
    type: approval
    gate: enterprise-architecture

  - id: implementation
    type: agent
    capability: coding
    profile: codex
    mode: workspace-write
    retry:
      max_attempts: 2
      delay_seconds: 1

  - id: tests
    type: quality-gate
    gate: tests

  - id: trivy
    type: quality-gate
    gate: trivy
    if: env:SDAI_TRIVY

  - id: validate
    type: validate
```

### Conditions

The condition language is deliberately small and does **not** use Python `eval`.

```text
always
never
env:NAME
env:NAME=value
artifact:relative/path
approved:gate
step:step-id=completed
not:<condition>
```

Example:

```yaml
- id: trivy
  type: quality-gate
  gate: trivy
  if: env:SDAI_TRIVY
```

### Retry and failure handling

```yaml
retry:
  max_attempts: 3
  delay_seconds: 2
  backoff_multiplier: 2
on_failure: stop   # or continue
```

### Parallel agents

Parallel groups currently allow **advisory/read-only agent children only**. This prevents concurrent agents from racing on repository writes.

```yaml
- id: independent-reviews
  type: parallel
  steps:
    - id: architecture
      type: agent
      capability: architecture
      profile: claude
      mode: advisory
    - id: security
      type: agent
      capability: security
      profile: copilot
      mode: advisory
```

## Run any step manually

Manual control is a core framework rule:

```bash
sdai step list FEATURE-123 --workflow enterprise
sdai step run FEATURE-123 design-reviews --workflow enterprise
sdai step run FEATURE-123 tests --workflow enterprise
sdai step run FEATURE-123 implementation --workflow enterprise --profile codex
```

A completed step is protected from accidental rerun. Use `--force` deliberately:

```bash
sdai step run FEATURE-123 architecture-baseline --workflow enterprise --force
```

A forced upstream rerun invalidates downstream completion markers. A manual workspace-writing AI step before an unsatisfied prior approval also requires `--force`, so the governance bypass is explicit.

`--force` can also intentionally bypass a false step condition for targeted recovery or investigation.

## Role-backed approvals

Approval policies live in:

```text
.sdai/approval-policies.yaml
```

The existing `architecture` gate remains backward compatible. The enterprise workflow demonstrates role-backed gates:

```bash
sdai approve FEATURE-123 enterprise-architecture \
  --by architect@example.com \
  --role architect \
  --note "Reviewed ADRs and architecture risks"

sdai approve FEATURE-123 enterprise-security \
  --by security@example.com \
  --role security
```

Policies support:

```yaml
enterprise-architecture:
  min_approvals: 1
  required_roles: [architect]
  allowed_approvers: []
```

Repeated approval by the same identity does not count as multiple distinct approvals.

## Policy-as-code

Organization-level governance lives in:

```text
.sdai/governance.yaml
```

It can define:

- maximum parallelism
- allowed workspace-write profiles
- required quality gates by lifecycle rigor
- opt-in automatic workflow policy enforcement

Check a workflow manually:

```bash
sdai policy check --workflow enterprise
```

Automatic enforcement is **off by default** for backward compatibility and can be enabled in `governance.yaml`.

## Quality gates

Quality gates are argument-list commands, never shell strings. Defaults are stored in:

```text
.sdai/quality-gates.yaml
```

Scaffolded gates include:

- `tests` — enabled by default
- `trivy` — disabled until configured/enabled
- `sonar` — disabled until configured/enabled

Inspect or run them independently:

```bash
sdai gates list
sdai gates run tests --feature-id FEATURE-123
sdai gates run trivy --feature-id FEATURE-123 --show-output
```

Gate results are persisted under `specs/<feature>/quality-gates/` and become context for downstream review agents.

## GitHub integration

GitHub integration uses the local `gh` authentication context; SD-AI does not store a GitHub token.

Create an intake from an issue:

```bash
sdai intake github FEATURE-123 \
  --repo my-org/my-repo \
  --issue 123 \
  --workflow enterprise
```

Create a draft pull request from the feature artifacts:

```bash
sdai pr create FEATURE-123 \
  --repo my-org/my-repo \
  --base main
```

Use `--ready` to create a ready-for-review PR rather than a draft.

## Jira integration

Jira intake uses environment-based authentication. Credentials are not written to `.sdai`.

Configure one supported authentication path:

```text
JIRA_BASE_URL
JIRA_EMAIL + JIRA_API_TOKEN
```

or:

```text
JIRA_BASE_URL
JIRA_BEARER_TOKEN
```

Then:

```bash
sdai intake jira PROJ-123 --workflow enterprise
```

Check local integration readiness:

```bash
sdai integrations doctor
```

## Skills

Provider-neutral skills live under `.sdai/skills/` and are injected only for matching capabilities.

```bash
sdai skills list
sdai skills show architecture-review
```

## Prompts

Version-controlled prompts live under `.sdai/prompts/`.

```bash
sdai prompts list
sdai prompts show architect.md
```

## Built-in workflows

| Workflow | Typical use | Flow |
|---|---|---|
| `light` | bug, logging, small refactor | intake → implementation brief → validation |
| `standard` | normal feature/API change | spec → architecture → plan → implementation → validation |
| `critical` | security/data/architecture change | spec → architecture → security → plan → implementation → validation |
| `agentic` | governed multi-agent AI-DLC | spec → AI reviews → approval → AI implementation/review/test → validation |
| `enterprise` | governed enterprise AI-DLC | spec → parallel reviews → role approvals → AI implementation → parallel review → quality gates → validation |

Teams can add any custom `.sdai/workflows/<name>.yaml` file.

## CLI overview

```text
sdai init
sdai upgrade
sdai feature <id> --title ... --description ... [--workflow NAME]

sdai intake github <id> --repo OWNER/REPO --issue N
sdai intake jira ISSUE-KEY [--feature-id ID]

sdai run <id> --workflow NAME
sdai step list <id> --workflow NAME
sdai step run <id> <step-id> --workflow NAME [--force] [--dry-run] [--profile NAME] [--mode advisory|workspace-write]

sdai approve <id> <gate> --by IDENTITY [--role ROLE] [--note ...]

sdai agents list
sdai agents doctor
sdai agents run <capability> <id> [--profile NAME] [--mode advisory|workspace-write] [--dry-run]

sdai gates list
sdai gates run <gate> [--feature-id ID]
sdai policy check --workflow NAME
sdai integrations doctor
sdai pr create <id> --repo OWNER/REPO [--base main] [--head BRANCH] [--ready]

sdai skills list
sdai skills show <name>
sdai prompts list
sdai prompts show <name>
```

## Documentation

- [Architecture](docs/ARCHITECTURE.md)
- [Agent platform](docs/AGENT-PLATFORM.md)
- [Workflow orchestration](docs/WORKFLOWS.md)
- [Enterprise governance](docs/ENTERPRISE.md)
- [Provider adapters](docs/PROVIDERS.md)
- [Skills](docs/SKILLS.md)
- [Prompts](docs/PROMPTS.md)
- [Governance](docs/GOVERNANCE.md)

## Roadmap

- [x] SDD lifecycle and validation
- [x] Architecture/ADR generation
- [x] Codex/Copilot/Claude/Gemini/custom agent adapters
- [x] Skills and version-controlled prompts
- [x] Declarative deterministic/agent/approval/validation workflows
- [x] Manual execution of any named workflow step
- [x] Workflow conditions, retries and failure handling
- [x] Parallel advisory-agent execution
- [x] Role/minimum approval policies
- [x] Policy-as-code checks
- [x] Test/Trivy/Sonar quality-gate framework
- [x] GitHub issue intake and PR automation
- [x] Jira issue intake
- [ ] Signed or externally verified approval identities
- [ ] OpenAPI/AsyncAPI contract validators
- [ ] Multi-repository feature graph
- [ ] Remote orchestration/control plane
- [ ] Web UI

## License

Apache-2.0.
