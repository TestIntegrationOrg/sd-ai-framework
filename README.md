# SD-AI Framework

**One open-source framework for Spec-Driven Development (SDD) + AI-Driven Development Life Cycle (AI-DLC).**

SD-AI treats the **approved specification and architecture artifacts as the source of truth**. Deterministic lifecycle agents create and validate those artifacts; interchangeable external AI agents can perform requirements analysis, architecture, planning, coding, testing, review, security, and documentation work around them.

> Project status: **0.3.0 / declarative multi-agent orchestration**.

## Lifecycle

```text
Business Intent
   ↓
Requirement
   ↓
Specification ───────────────┐
   ↓                         │
Architecture + ADRs          │ source of truth
   ↓                         │
Plan + Tasks                 │
   ↓                         │
AI Agents                    │
   ↓                         │
Code / Test / Review         │
   ↓                         │
Spec/Architecture Validation ┘
   ↓
PR / CI/CD
```

## Principles

1. **Spec first** — important implementation decisions trace to an approved specification.
2. **Architecture as code** — architecture, ADRs, Mermaid/C4, OpenAPI/AsyncAPI belong in Git.
3. **Agent separation of duties** — lifecycle capabilities are explicit and independently routable.
4. **Human approval where risk requires it** — workflows can pause and resume at durable approval gates.
5. **Provider neutral** — Codex, Copilot, Claude, Gemini, local models, and future providers sit behind adapters.
6. **Skills and prompts are code** — reusable agent behavior is version controlled and reviewable.
7. **Manual control is always available** — any named workflow step can be inspected or run independently at any time.
8. **Validation closes the loop** — implementation is checked against specification and architecture.

## v0.3 declarative orchestration

Workflow YAML can mix deterministic SDD steps, external AI agents, human approval gates, and validation:

```yaml
version: 3
name: agentic
validation_mode: critical
steps:
  - id: spec-baseline
    type: deterministic
    action: specify

  - id: architecture-review
    type: agent
    capability: architecture
    mode: advisory

  - id: architecture-approval
    type: approval
    gate: architecture

  - id: implementation
    type: agent
    capability: coding
    profile: codex
    mode: workspace-write

  - id: code-review
    type: agent
    capability: review
    profile: copilot
    mode: advisory

  - id: validate
    type: validate
```

Workflow state is persisted under the feature workspace. If an approval is missing, the workflow stops safely; after approval, the next `sdai run` resumes and skips completed steps.

### Manual step execution

Every named step can be run independently, even when earlier workflow steps are incomplete:

```bash
sdai step list SCRIPT-123 --workflow agentic
sdai step run SCRIPT-123 architecture-review --workflow agentic --dry-run
sdai step run SCRIPT-123 architecture-review --workflow agentic
sdai step run SCRIPT-123 implementation --workflow agentic --profile codex
```

Completed steps are protected from accidental reruns. Use `--force` when the rerun is intentional:

```bash
sdai step run SCRIPT-123 architecture-review --workflow agentic --force
```

This manual path does not enforce predecessor ordering. It is intended for targeted investigation, repair, review, experimentation, and controlled reruns.

### Human approval and resume

```bash
sdai run SCRIPT-123 --workflow agentic
# workflow pauses at architecture-approval

sdai approve SCRIPT-123 architecture --by "architect@example.com" --note "Reviewed ADR and threat model"

sdai run SCRIPT-123 --workflow agentic
# workflow resumes after the approval gate
```

Approval records are persisted under `specs/<feature>/approvals/`.

## Multi-agent architecture

```text
                      SD-AI
                        │
              ┌─────────┴─────────┐
              │                   │
          SDD Control         AI Execution
              │                   │
      Spec / Arch / ADR       Capability Router
      Plan / Validation            │
              │          ┌─────────┼──────────┐
              │          ▼         ▼          ▼
              │       Codex     Copilot     Claude
              │          │         │          │
              │          └─────────┼──────────┘
              │                    ▼
              │              Prompt + Skills
              └────────────────────┘
```

Built-in adapters support **Codex**, **GitHub Copilot CLI**, **Claude Code**, **Gemini CLI**, custom/local command agents, and Python provider plugins via the `sdai.providers` entry-point group.

## Quick start

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -e .

sdai init
sdai feature SCRIPT-123 --title "KMS-backed script signing" \
  --description "Sign PowerShell artifacts without exporting the private key" \
  --workflow agentic

sdai step list SCRIPT-123 --workflow agentic
sdai run SCRIPT-123 --workflow agentic
```

For an existing project, add missing current scaffold files without overwriting custom files:

```bash
sdai upgrade
```

## External agents

```bash
sdai agents list
sdai agents doctor
sdai agents run architecture SCRIPT-123 --dry-run
sdai agents run architecture SCRIPT-123 --profile codex --dry-run
sdai agents run coding SCRIPT-123 --profile claude --dry-run
sdai agents run review SCRIPT-123 --profile copilot --dry-run
```

External execution defaults to **advisory** mode. Repository writes require an explicit mode:

```bash
sdai agents run coding SCRIPT-123 --profile codex --mode workspace-write
```

Provider-specific permissions still apply. SD-AI does not automatically grant broad unrestricted permissions.

## Capability routing

The scaffold demonstrates different agents for different lifecycle jobs:

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

These are defaults only. Edit `.sdai/routing.yaml`, set a workflow step profile, or override a manual invocation with `--profile`.

## Skills

Provider-neutral skills live under `.sdai/skills/`:

```text
spec-traceability/
architecture-review/
secure-coding/
test-design/
```

Each skill contains a `skill.yaml` manifest and `SKILL.md`. A profile can attach several skills; only skills applicable to the requested capability are injected.

```bash
sdai skills list
sdai skills show architecture-review
```

## Prompts

Reusable prompts live under `.sdai/prompts/`:

```text
requirements.md
architect.md
planner.md
developer.md
reviewer.md
tester.md
security.md
documentation.md
general.md
```

`prompt: auto` selects the capability-specific prompt.

```bash
sdai prompts list
sdai prompts show architect.md
```

## Lifecycle modes and workflows

The built-in deterministic workflows remain available:

| Workflow | Typical use | Flow |
|---|---|---|
| `light` | bug, logging, small refactor | intake → implement → validate |
| `standard` | normal feature/API change | specify → architect → plan → implement → validate |
| `critical` | security, data model, cross-service architecture | specify → architect → security → plan → implement → validate |
| `agentic` | governed AI-DLC | spec → AI reviews → approval → AI implementation/review/test → validation |

Teams can add any custom file under `.sdai/workflows/<name>.yaml`. `validation_mode` remains `light`, `standard`, or `critical` even when the workflow name is custom.

## CLI

```text
sdai init
sdai upgrade
sdai feature <id> --title ... --description ... [--workflow NAME]

sdai specify <id>
sdai architect <id>
sdai plan <id>
sdai implement <id>
sdai security <id>
sdai validate <id>

sdai run <id> --workflow NAME
sdai step list <id> --workflow NAME
sdai step run <id> <step-id> --workflow NAME [--force] [--dry-run] [--profile NAME] [--mode advisory|workspace-write]
sdai approve <id> <gate> --by APPROVER [--note ...]

sdai agents list
sdai agents doctor
sdai agents run <capability> <id> [--profile NAME] [--mode advisory|workspace-write] [--dry-run]
sdai skills list
sdai skills show <name>
sdai prompts list
sdai prompts show <name>
```

## Documentation

- [Architecture](docs/ARCHITECTURE.md)
- [Agent platform](docs/AGENT-PLATFORM.md)
- [Workflow orchestration](docs/WORKFLOWS.md)
- [Provider adapters](docs/PROVIDERS.md)
- [Skills](docs/SKILLS.md)
- [Prompts](docs/PROMPTS.md)
- [Governance](docs/GOVERNANCE.md)

## Roadmap

- [x] SDD lifecycle and validation
- [x] Architecture/ADR generation
- [x] Provider-neutral external agent runtime
- [x] Codex, Copilot, Claude, Gemini adapters
- [x] Custom command and Python provider extension points
- [x] Capability routing and profile overrides
- [x] Provider-neutral skills
- [x] Version-controlled prompt templates
- [x] Advisory/workspace-write execution modes
- [x] Declarative deterministic/agent/approval/validation workflow steps
- [x] Persisted workflow state and pause/resume
- [x] Manual execution of any named workflow step
- [ ] Signed/role-based approval policy
- [ ] GitHub/Jira integration adapters
- [ ] OpenAPI/AsyncAPI validators
- [ ] Policy-as-code engine
- [ ] Multi-repository feature graph
- [ ] Sonar/Trivy integration
- [ ] Web control plane

## License

Apache-2.0.
