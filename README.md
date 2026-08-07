# SD-AI Framework

**One open-source framework for Spec-Driven Development (SDD) + AI-Driven Development Life Cycle (AI-DLC).**

SD-AI treats the **approved specification and architecture artifacts as the source of truth**. Deterministic lifecycle agents create and validate those artifacts; interchangeable external AI agents can perform requirements analysis, architecture, planning, coding, testing, review, security, and documentation work around them.

> Project status: **0.2.0 / multi-agent foundation**.

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
4. **Human approval where risk requires it** — not every change needs the same ceremony.
5. **Provider neutral** — Codex, Copilot, Claude, Gemini, local models, and future providers sit behind adapters.
6. **Skills and prompts are code** — reusable agent behavior is version controlled and reviewable.
7. **Validation closes the loop** — implementation is checked against specification and architecture.

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

Every named profile can support the same capability set, so routing is configurable rather than hard-coded to one vendor.

## Quick start

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -e .

sdai init
sdai feature SCRIPT-123 --title "KMS-backed script signing" \
  --description "Sign PowerShell artifacts without exporting the private key"
sdai run SCRIPT-123 --workflow standard
sdai validate SCRIPT-123
```

For a project initialized with v0.1, add the v0.2 agent scaffold without overwriting custom files:

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

These are defaults only. Edit `.sdai/routing.yaml` or override with `--profile`.

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

## Three lifecycle modes

| Mode | Typical use | Flow |
|---|---|---|
| `light` | bug, logging, small refactor | intake → implement → validate |
| `standard` | normal feature/API change | specify → architect → plan → implement → validate |
| `critical` | security, data model, cross-service architecture | specify → architect → security → approval → plan → implement → validate |

## CLI

```text
sdai init
sdai upgrade
sdai feature <id> --title ... --description ...
sdai specify <id>
sdai architect <id>
sdai plan <id>
sdai implement <id>
sdai security <id>
sdai validate <id>
sdai run <id> --workflow light|standard|critical

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
- [ ] External-agent execution as declarative workflow steps
- [ ] GitHub/Jira integration adapters
- [ ] OpenAPI/AsyncAPI validators
- [ ] Policy-as-code engine
- [ ] Human approval state + signatures
- [ ] Multi-repository feature graph
- [ ] Sonar/Trivy integration
- [ ] Web control plane

## License

Apache-2.0.
