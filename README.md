# SD-AI Framework

**One open-source framework for Spec-Driven Development (SDD) + AI-Driven Development Life Cycle (AI-DLC).**

SD-AI treats approved specifications and architecture artifacts as the source of truth. Semantic agent roles, provider profiles, reusable skills, declarative workflows, approval gates, quality gates, and enterprise integrations operate around that source of truth.

> Project status: **0.5.0 / canonical agent files + shared skills**.

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
 Semantic Agent Roles
            ↓
 ┌──────────┼───────────┐
 ▼          ▼           ▼
Claude    Codex       Copilot      Gemini / Custom
 └──────────┬───────────┘
            ↓
   Human Approval Gates
            ↓
        Plan / Tasks
            ↓
  AI Workspace Implementation
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
3. **Semantic role != provider** — `architect` is a role; Claude/Codex/Copilot/Gemini are execution choices.
4. **Skills and prompts are code** — reusable agent behavior is version controlled and reviewable.
5. **Provider neutral** — workflows can change providers without duplicating semantic agent instructions.
6. **Least privilege** — advisory mode is read-only; workspace writes are explicit.
7. **Human governance** — risky workflows can pause for role-backed approvals.
8. **Manual control remains available** — every named workflow step can be run independently.
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

## v0.5 canonical agent files

SD-AI now has a canonical semantic-agent layer:

```text
.sdai/
├── agents/
│   ├── requirements-analyst.agent.md
│   ├── architect.agent.md
│   ├── planner.agent.md
│   ├── developer.agent.md
│   ├── code-reviewer.agent.md
│   ├── tester.agent.md
│   ├── security-reviewer.agent.md
│   └── documentation-writer.agent.md
└── agent-routing.yaml
```

Example:

```markdown
---
name: architect
description: Generate architecture options, trade-offs, and ADR proposals.
capabilities: [architecture, review]
skills: [architecture-design, architecture-review, spec-traceability]
profile: claude
execution_mode: advisory
providers: {}
---
# Architect

Derive architecture drivers from approved requirements and NFRs.
Generate viable alternatives, compare trade-offs, and propose ADRs.
```

The semantic agent describes the **role and behavior**. Provider profiles in `.sdai/agents.yaml` still describe **how the work executes**.

### Semantic-agent routing

`.sdai/agent-routing.yaml`:

```yaml
routes:
  requirements: requirements-analyst
  architecture: architect
  planning: planner
  coding: developer
  review: code-reviewer
  testing: tester
  security: security-reviewer
  documentation: documentation-writer
```

Provider routing remains separate in `.sdai/routing.yaml`.

This lets you keep one Architect definition and run it through different providers:

```bash
sdai agents run architecture FEATURE-123 --agent architect --profile claude --dry-run
sdai agents run architecture FEATURE-123 --agent architect --profile codex --dry-run
sdai agents run architecture FEATURE-123 --agent architect --profile copilot --dry-run
```

## Shared skills

Canonical skills live under the shared `.agents/skills` tree:

```text
.agents/skills/
├── requirements-analysis/
├── architecture-design/
├── architecture-review/
├── implementation-planning/
├── spec-traceability/
├── secure-coding/
├── test-design/
└── documentation-quality/
```

Each skill uses portable `SKILL.md` frontmatter:

```markdown
---
name: architecture-design
description: Design architecture from explicit drivers and trade-offs.
---
# Architecture Design

- Derive architecture drivers before selecting technology.
- Generate multiple viable options for material decisions.
- Record material choices as ADR proposals.
```

SD-AI capability metadata is kept in an adjacent `sdai.yaml`:

```yaml
version: 1
capabilities: [architecture]
```

Canonical `.agents/skills` takes precedence over the legacy `.sdai/skills` representation when names collide, so older projects continue to work while migrating.

Inspect skills:

```bash
sdai skills list
sdai skills show architecture-design
sdai skills validate
```

## Provider-native synchronization

One canonical agent definition can be exported to native provider formats:

```bash
sdai agents sync --provider all
```

Generated locations:

```text
Codex       .codex/agents/<name>.toml
Copilot     .github/agents/<name>.agent.md
Claude      .claude/agents/<name>.md
Gemini      .gemini/agents/<name>.md
```

Claude project skills are mirrored into `.claude/skills/` during Claude synchronization. The shared `.agents/skills` source remains canonical.

Generate only one provider:

```bash
sdai agents sync --provider codex
sdai agents sync --provider copilot
sdai agents sync --provider claude
sdai agents sync --provider gemini
```

Generated files contain an SD-AI marker. SD-AI refuses to overwrite an unmarked hand-authored native agent unless you explicitly use:

```bash
sdai agents sync --provider copilot --force
```

## Agent CLI

Provider profiles:

```bash
sdai agents list
sdai agents doctor
```

Semantic agent definitions:

```bash
sdai agents definitions
sdai agents show architect
```

Run or inspect:

```bash
sdai agents run architecture FEATURE-123 --dry-run
sdai agents run architecture FEATURE-123 --agent architect --dry-run
sdai agents run architecture FEATURE-123 --agent architect --profile codex --dry-run
```

External execution defaults to **advisory** mode. Repository write behavior must still be selected explicitly:

```bash
sdai agents run coding FEATURE-123 \
  --agent developer \
  --profile codex \
  --mode workspace-write
```

Provider-specific sandbox and permission controls still apply.

## Workflow integration

Agent steps can name semantic agent independently from provider profile:

```yaml
- id: architecture-review
  type: agent
  agent: architect
  capability: architecture
  mode: advisory
```

Pin a provider only where policy requires it:

```yaml
- id: implementation
  type: agent
  agent: developer
  capability: coding
  profile: codex
  mode: workspace-write
```

Built-in `agentic` and `enterprise` workflows use v0.5 semantic agent roles.

### Advanced orchestration

Workflow YAML also supports:

- deterministic lifecycle steps
- human approval gates
- quality gates
- validation steps
- safe `if` conditions without Python `eval`
- retry/backoff
- `on_failure: stop|continue`
- bounded parallel advisory agents
- persisted pause/resume state

Example:

```yaml
version: 5
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
        agent: architect
        capability: architecture
        mode: advisory
      - id: security-review
        type: agent
        agent: security-reviewer
        capability: security
        mode: advisory

  - id: architecture-approval
    type: approval
    gate: enterprise-architecture

  - id: implementation
    type: agent
    agent: developer
    capability: coding
    mode: workspace-write

  - id: tests
    type: quality-gate
    gate: tests

  - id: validate
    type: validate
```

## Run any step manually

Manual execution remains a core rule:

```bash
sdai step list FEATURE-123 --workflow enterprise

sdai step run FEATURE-123 architecture-review \
  --workflow enterprise \
  --dry-run

sdai step run FEATURE-123 architecture-review \
  --workflow enterprise \
  --agent architect \
  --profile codex \
  --dry-run
```

A completed step is protected from accidental rerun. Use `--force` intentionally. Forced upstream reruns invalidate downstream completion markers so stale derived work cannot remain marked complete.

A manual workspace-writing AI step before an unsatisfied prior approval also requires `--force`, making the governance bypass explicit and auditable.

## Enterprise governance and integrations

v0.4 capabilities remain available:

- role-backed human approvals and approver allowlists
- policy-as-code under `.sdai/governance.yaml`
- test / Trivy / Sonar quality gates
- GitHub issue intake and pull-request creation
- Jira HTTPS intake with environment-only credentials
- path traversal protection
- prompt-injection boundary for external artifacts
- quality-gate output redaction

Useful commands:

```bash
sdai policy check --workflow enterprise
sdai gates list
sdai gates run tests --feature-id FEATURE-123
sdai integrations doctor
```

## Documentation

- [Architecture](docs/ARCHITECTURE.md)
- [Agent platform](docs/AGENT-PLATFORM.md)
- [Agent files and skills](docs/AGENT-FILES.md)
- [Workflow orchestration](docs/WORKFLOWS.md)
- [Enterprise governance](docs/ENTERPRISE.md)
- [Provider adapters](docs/PROVIDERS.md)
- [Skills](docs/SKILLS.md)
- [Prompts](docs/PROMPTS.md)

## Roadmap

- [x] SDD lifecycle and validation
- [x] Architecture/ADR generation
- [x] Codex, Copilot, Claude, Gemini and custom provider adapters
- [x] Declarative agent/approval/quality/validation workflows
- [x] Conditions, retries, parallel reviews and pause/resume
- [x] Manual execution of top-level and nested workflow steps
- [x] GitHub/Jira integrations
- [x] Sonar/Trivy/test quality gates
- [x] Role-backed approvals and workflow policy checks
- [x] Canonical `.agent.md` semantic role files
- [x] Shared `.agents/skills` skill source
- [x] Provider-native agent synchronization
- [ ] OpenAPI / AsyncAPI / JSON Schema contract validation
- [ ] Requirement → ADR → task → code → test traceability graph
- [ ] Architecture drift detection
- [ ] Multi-repository feature graph
- [ ] Web control plane

## License

Apache-2.0.
