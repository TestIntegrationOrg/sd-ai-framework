# SD-AI Framework

**One open-source framework for Spec-Driven Development (SDD) + AI-Driven Development Life Cycle (AI-DLC).**

SD-AI treats approved specification and architecture artifacts as source of truth. Semantic agent roles, provider profiles, reusable skills, declarative workflows, approvals, quality gates, and integrations operate around that source of truth.

<!-- sdai-release-version: 1.0.0 -->
> Project status: **1.0.0 / stable enterprise-ready identity-independent SDAI lifecycle**.

SDAI 1.0 has completed the stable extension/JSON compatibility boundaries, migration safety, Tier-1 documentation, security/policy hardening, mandatory E2E journeys, and Ubuntu/Windows/macOS package-install confidence gates recorded in [1.0 release readiness](docs/releases/1.0-release-readiness.md). The held 0.18/#25 identity-backed enterprise approval capability is explicitly not part of 1.0.

## One framework, same capabilities

SD-AI does not have a reduced "individual edition" and a separate "enterprise edition".
The same runtime and CLI support:

- Codex, Claude, GitHub Copilot, Gemini, local/custom commands, and provider plugins
- canonical `.sdai/agents/*.agent.md` semantic agents
- shared `.agents/skills/*/SKILL.md` skills
- version-controlled prompts
- custom/declarative workflows
- manual execution of any named workflow step
- advisory and workspace-write execution
- explicit UTF-8 provider and repository-text boundaries across Windows and Linux
- approvals, retries, conditions, parallel advisory agents, and pause/resume
- GitHub/Jira integrations
- test, Trivy, and Sonar quality gates
- provider/model overrides

Configuration determines **who controls the allowed choices**:

```text
                     SD-AI runtime
                          │
                   Effective policy
                          │
             ┌────────────┴────────────┐
             │                         │
        Individual                 Enterprise
             │                         │
      Engineer policy          Organization policy
      + repo/user config       + repo/user config
             │                         │
             └────────────┬────────────┘
                          │
                   Same workflows
                   Same agents/skills
                   Same provider router
```

In individual mode, the engineer may use any configured provider/profile. In enterprise mode, the employee may choose among providers/models approved by organization policy. Repository/user policy may narrow organization permissions but cannot expand them.

See [Configuration modes](docs/CONFIGURATION-MODES.md), [Enterprise policy](docs/ENTERPRISE-POLICY.md), and [Execution security](docs/EXECUTION-SECURITY.md).

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
4. **Same capabilities, configuration-driven control** — individuals and enterprises use one engine; policy changes who may widen permissions.
5. **Skills and prompts are code** — reusable agent behavior is version controlled and reviewable.
6. **Least privilege** — advisory is the default; workspace writes are explicit and protected source-of-truth paths are framework-owned.
7. **Human governance** — workflows can pause for approvals and enterprise policy can prohibit force-bypass.
8. **Manual control remains available** — every named workflow step can be run independently when effective policy permits it.
9. **Validation closes the loop** — quality and specification checks run before delivery.

## Quick start

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -e .

sdai --version
sdai init
sdai feature FEATURE-123 \
  --title "Add governed signing workflow" \
  --description "Implement the feature with architecture and security review" \
  --workflow enterprise

sdai step list FEATURE-123 --workflow enterprise
sdai run FEATURE-123 --workflow enterprise
```

Upgrade without overwriting team customizations:

```bash
sdai upgrade
```

## Individual configuration

Individual is the default; `operating_mode` may be omitted or set explicitly:

```yaml
# .sdai/config.yaml
operating_mode: individual
```

Provider choice remains developer-controlled:

```bash
sdai agents run architecture FEATURE-123 --agent architect --profile claude --dry-run
sdai agents run architecture FEATURE-123 --agent architect --profile codex --dry-run
sdai agents run architecture FEATURE-123 --agent architect --profile copilot --dry-run
```

Repository policy is optional and may still enforce approvals, required skills, protected paths, or provider allowlists for an individual project.

## Enterprise configuration

Enterprise uses the same commands. A company supplies an organization policy file outside the repository:

```text
SDAI_OPERATING_MODE=enterprise
SDAI_ORG_POLICY_PATH=/company-managed/sdai/organization-policy.yaml
```

or `.sdai/config.yaml` may set:

```yaml
operating_mode: enterprise
```

with `SDAI_ORG_POLICY_PATH` still required.

Example organization policy:

```yaml
version: 1
providers:
  allowed_profiles: [claude-enterprise, codex-enterprise, copilot-enterprise]
  allowed_providers: [claude, codex, copilot]
  allowed_models:
    claude: [approved-architecture-model]
    codex: [approved-coding-model]

capabilities:
  architecture:
    allowed_profiles: [claude-enterprise, codex-enterprise]
  coding:
    allowed_profiles: [codex-enterprise, copilot-enterprise]

execution:
  workspace_write: true
  require_prior_approval_for_workspace_write: true
  allow_force_approval_bypass: false
  protected_paths:
    - .github/workflows/**

skills:
  required:
    coding: [secure-coding]
```

The employee still chooses any allowed option:

```bash
sdai step run FEATURE-123 architecture-review \
  --workflow enterprise --agent architect --profile claude-enterprise

sdai step run FEATURE-123 architecture-review \
  --workflow enterprise --agent architect --profile codex-enterprise
```

If `SDAI_ORG_POLICY_PATH` is present, organization policy is applied even if a repository says `operating_mode: individual`; repo-local configuration cannot weaken the company boundary.

## Agent files and skills

The default Architect now composes a dedicated architecture skill pack for RFCs, ADRs, C4 views, editable Draw.io XML, PlantUML sequence diagrams, API/event contracts, and threat modeling. Critical/standard lifecycle validation can make those artifacts required through the deterministic architecture-artifact validator. See [Architecture skills](docs/ARCHITECTURE-SKILLS.md) and [Architecture artifact validation](docs/ARCHITECTURE-VALIDATION.md).

Canonical semantic agents:

```text
.sdai/
├── agent-routing.yaml
└── agents/
    ├── requirements-analyst.agent.md
    ├── architect.agent.md
    ├── planner.agent.md
    ├── developer.agent.md
    ├── code-reviewer.agent.md
    ├── tester.agent.md
    ├── security-reviewer.agent.md
    └── documentation-writer.agent.md
```

Canonical shared skills:

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

Organization/repository/user policy may add mandatory skills to a capability. Mandatory skills are added to semantic-agent/profile skills; lower policy layers cannot remove them.

## Provider-native synchronization

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

Generated files are managed derivatives of canonical SD-AI agent files. Unmanaged native files are not overwritten unless `--force` is explicit.

## Secure external execution

SDAI's hardened execution boundary includes:

- prompt names are contained inside `.sdai/prompts`
- feature artifacts resolve symlinks and stay inside `specs/<feature>`
- invocation construction runs prompt-secret checks before dry-run can print content
- provider CLI processes receive a minimal environment rather than all developer/CI secrets
- built-in provider `extra_args` cannot override SD-AI sandbox/tool/approval controls
- workspace-write agents cannot persist changes to framework/source-of-truth protected paths
- organization/repository/user policy can add more protected paths

Framework lifecycle commands may update source-of-truth artifacts; external workspace-writing agents may not. See [Execution Security Reference](docs/EXECUTION-SECURITY.md) for the exact built-in protected paths, restoration behavior, environment rules, and limitations.

## Run any step manually

```bash
sdai step list FEATURE-123 --workflow enterprise

sdai step run FEATURE-123 architecture-review \
  --workflow enterprise --dry-run

sdai step run FEATURE-123 architecture-review \
  --workflow enterprise --agent architect --profile codex --dry-run
```

Manual execution remains available in both modes. Enterprise policy may require an approval before a workspace-write step or prohibit `--force` from bypassing that mandatory gate.

## Quality gates and integrations

```bash
sdai gates list
sdai gates run tests --feature-id FEATURE-123
sdai integrations doctor
sdai policy check --workflow enterprise
```

Supported integrations include GitHub issue/PR workflows and Jira HTTPS intake. Test, Trivy, and Sonar gates remain declarative and optional/mandatory according to configuration and policy.

## Documentation

- [Contributing to SDAI](CONTRIBUTING.md)
- [Extension authoring guide](docs/EXTENSION-AUTHORING.md)
- [SDAI 1.0 release readiness](docs/releases/1.0-release-readiness.md)
- [Configuration modes](docs/CONFIGURATION-MODES.md)
- [Enterprise policy reference](docs/ENTERPRISE-POLICY.md)
- [Execution security reference](docs/EXECUTION-SECURITY.md)
- [Architecture skills](docs/ARCHITECTURE-SKILLS.md)
- [Architecture artifact validation](docs/ARCHITECTURE-VALIDATION.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Agent platform](docs/AGENT-PLATFORM.md)
- [Agent files and skills](docs/AGENT-FILES.md)
- [Workflow orchestration](docs/WORKFLOWS.md)
- [Enterprise governance](docs/ENTERPRISE.md)
- [Provider adapters](docs/PROVIDERS.md)
- [Skills](docs/SKILLS.md)
- [Prompts](docs/PROMPTS.md)
- [Release/version workflow](docs/RELEASING.md)
- [Security policy](SECURITY.md)

## Roadmap

- [x] SDD lifecycle and validation
- [x] Architecture/ADR generation
- [x] Codex, Copilot, Claude, Gemini and custom provider adapters
- [x] Declarative agent/approval/quality/validation workflows
- [x] Conditions, retries, parallel reviews and pause/resume
- [x] Manual execution of top-level and nested workflow steps
- [x] GitHub/Jira integrations
- [x] Sonar/Trivy/test quality gates
- [x] Role-backed local approvals and workflow policy checks
- [x] Canonical `.agent.md` semantic role files
- [x] Shared `.agents/skills` skill source
- [x] Provider-native agent synchronization
- [x] RFC/ADR/C4/Draw.io/PlantUML/API/threat-model architecture skill pack
- [x] Deterministic architecture-artifact validation for standard/critical features
- [x] Configuration-driven individual + enterprise provider/model policy
- [x] Protected source-of-truth workspace writes
- [x] External provider environment isolation
- [x] Enterprise architecture authoring skill pack
- [x] Extension manifest/registry foundation
- [x] Extension scaffolding and validation CLI
- [x] Engineering constitution, clarification, and requirements-quality checks
- [x] Behavioral skill/agent evaluation foundation
- [x] OpenAPI / AsyncAPI / JSON Schema / Protobuf contract validation and compatibility analysis
- [x] Requirement → architecture → task → code → test/evidence traceability graph
- [x] Architecture drift detection and policy integration
- [x] Specification stores and multi-repository feature graph
- [ ] Identity-backed enterprise approvals — 0.18/#25 held by explicit scope decision
- [ ] Web control plane

## License

Apache-2.0.
