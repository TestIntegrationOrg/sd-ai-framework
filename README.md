# SD-AI Framework

**One open-source framework for Spec-Driven Development (SDD) + AI-Driven Development Life Cycle (AI-DLC).**

SD-AI treats the **approved specification and architecture artifacts as the source of truth**. AI agents perform bounded work around that source of truth: requirements analysis, architecture, planning, implementation guidance, testing, security review, and validation.

> Project status: **0.1.0 / foundation MVP**. The CLI and core lifecycle are runnable. External coding-agent integrations are intentionally provider-neutral and will evolve behind adapters.

## Why SD-AI?

Most AI coding workflows jump directly from issue/prompt to code. SD-AI introduces a governed path:

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
AI Implementation            │
   ↓                         │
Test + Security Review       │
   ↓                         │
Spec/Architecture Validation ┘
   ↓
PR / CI/CD
```

## Principles

1. **Spec first** — important implementation decisions must trace to an approved specification.
2. **Architecture as code** — architecture, ADRs, Mermaid/C4, OpenAPI/AsyncAPI belong in Git.
3. **Agent separation of duties** — requirement, architect, planner, developer, test, security, and validator agents have bounded responsibilities.
4. **Human approval where risk requires it** — not every change needs the same ceremony.
5. **Provider neutral** — SD-AI orchestrates agents; it does not make one LLM vendor your control plane.
6. **Validation closes the loop** — implementation artifacts are checked against specification and architecture artifacts.

## Three lifecycle modes

| Mode | Typical use | Flow |
|---|---|---|
| `light` | bug, logging, small refactor | intake → implement → validate |
| `standard` | normal feature/API change | specify → architect → plan → implement → validate |
| `critical` | security, data model, cross-service architecture | specify → architect → security → approval → plan → implement → validate |

## Quick start

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\\Scripts\\activate
pip install -e .

sdai init
sdai feature SCRIPT-123 --title "KMS-backed script signing" \
  --description "Sign PowerShell artifacts without exporting the private key"
sdai run SCRIPT-123 --workflow standard
sdai validate SCRIPT-123
```

Generated artifacts live under `specs/<feature-id>/`.

## CLI

```text
sdai init
sdai feature <id> --title ... --description ...
sdai specify <id>
sdai architect <id>
sdai plan <id>
sdai implement <id>
sdai security <id>
sdai validate <id>
sdai run <id> --workflow light|standard|critical
```

## Repository created by `sdai init`

```text
.sdai/
├── constitution.yaml
├── config.yaml
├── policies.yaml
└── workflows/
    ├── light.yaml
    ├── standard.yaml
    └── critical.yaml
specs/
```

## Agent model

```text
                Orchestrator
                    │
   ┌────────────────┼────────────────┐
   ▼                ▼                ▼
Requirement      Architect         Planner
 Agent            Agent             Agent
                                     │
                                     ▼
                                Developer
                                  Agent
                                     │
                  ┌──────────────────┼───────────────┐
                  ▼                  ▼               ▼
                Test             Security        Validator
                Agent              Agent            Agent
```

The MVP ships deterministic agents so the workflow can be exercised without sending source code to a model. Provider adapters implement the same interface and can be added independently.

## Architecture outputs

`sdai architect` produces:

- `architecture/architecture.md`
- `architecture/context.mmd`
- `architecture/container.mmd`
- `architecture/decision-matrix.md`
- `adr/ADR-001-initial-architecture.md`

The architect workflow is designed to force **alternatives + trade-offs**, not just a single AI-generated answer.

## Roadmap

- [x] CLI foundation
- [x] Feature workspace and artifact model
- [x] Requirement/specification agent
- [x] Architecture agent with ADR + Mermaid outputs
- [x] Planning agent
- [x] Security review artifact
- [x] Spec/architecture validator
- [x] Declarative workflow engine
- [x] Light / standard / critical workflows
- [ ] External model-provider adapters
- [ ] GitHub/Jira integration adapters
- [ ] Coding-agent execution sandbox
- [ ] OpenAPI/AsyncAPI validators
- [ ] Policy-as-code engine
- [ ] Human approval state + signatures
- [ ] Multi-repository feature graph
- [ ] Sonar/Trivy integration
- [ ] Web control plane

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Early contributions should preserve the core rule: **AI may propose and execute work, but approved specs and architecture govern the work.**

## License

Apache-2.0.
