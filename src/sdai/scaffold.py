from __future__ import annotations

from pathlib import Path

from sdai.artifacts import write_text


CONSTITUTION = """version: 2
principles:
  - spec-first
  - architecture-as-code
  - human-approval-for-critical-changes
  - least-privilege-agents
  - provider-neutral-agent-routing
engineering:
  language: unspecified
  architecture_style: unspecified
security:
  secrets_in_prompts: forbidden
  private_keys_in_prompts: forbidden
  external_agent_default_mode: advisory
quality:
  tests_required: true
  traceability_required: true
"""

CONFIG = """version: 2
default_workflow: standard
artifact_root: specs
agent_platform:
  enabled: true
  default_execution_mode: advisory
  max_context_chars_per_file: 30000
"""

POLICIES = """version: 2
change_classification:
  light:
    examples: [bug, logging, small-refactor]
  standard:
    examples: [feature, api-change, integration]
  critical:
    examples: [security, architecture, data-model, cross-service]
approvals:
  standard: [spec]
  critical: [spec, architecture, security]
agent_execution:
  advisory:
    repository_write: false
  workspace-write:
    repository_write: true
    requires_explicit_cli_flag: true
"""

WORKFLOWS = {
    "light": """name: light\nsteps:\n  - implement\n  - validate\n""",
    "standard": """name: standard\nsteps:\n  - specify\n  - architect\n  - plan\n  - implement\n  - validate\n""",
    "critical": """name: critical\nsteps:\n  - specify\n  - architect\n  - security\n  - plan\n  - implement\n  - validate\n""",
}

AGENTS = """version: 1
profiles:
  codex:
    provider: codex
    enabled: true
    prompt: auto
    capabilities: [requirements, architecture, planning, coding, review, testing, security, documentation]
    skills: [spec-traceability, architecture-review, secure-coding, test-design]
    timeout_seconds: 900
    startup_timeout_seconds: 10
    first_output_timeout_seconds: 60
    idle_output_timeout_seconds: 120
    termination_grace_seconds: 1

  copilot:
    provider: copilot
    enabled: true
    prompt: auto
    capabilities: [requirements, architecture, planning, coding, review, testing, security, documentation]
    skills: [spec-traceability, architecture-review, secure-coding, test-design]
    timeout_seconds: 900
    startup_timeout_seconds: 10
    first_output_timeout_seconds: 60
    idle_output_timeout_seconds: 120
    termination_grace_seconds: 1

  claude:
    provider: claude
    enabled: true
    prompt: auto
    capabilities: [requirements, architecture, planning, coding, review, testing, security, documentation]
    skills: [spec-traceability, architecture-review, secure-coding, test-design]
    timeout_seconds: 900
    startup_timeout_seconds: 10
    first_output_timeout_seconds: 60
    idle_output_timeout_seconds: 120
    termination_grace_seconds: 1

  gemini:
    provider: gemini
    enabled: true
    prompt: auto
    capabilities: [requirements, architecture, planning, coding, review, testing, security, documentation]
    skills: [spec-traceability, architecture-review, secure-coding, test-design]
    timeout_seconds: 900
    startup_timeout_seconds: 10
    first_output_timeout_seconds: 60
    idle_output_timeout_seconds: 120
    termination_grace_seconds: 1

  local-command:
    provider: command
    enabled: false
    command: [my-agent]
    prompt: auto
    capabilities: [requirements, architecture, planning, coding, review, testing, security, documentation]
    skills: [spec-traceability]
    timeout_seconds: 900
    startup_timeout_seconds: 10
    first_output_timeout_seconds: 60
    idle_output_timeout_seconds: 120
    termination_grace_seconds: 1
"""

ROUTING = """version: 1
routes:
  requirements: claude
  architecture: claude
  planning: codex
  coding: codex
  review: copilot
  testing: copilot
  security: claude
  documentation: gemini
"""

PROMPTS = {
    "requirements.md": """# Requirement Analysis Task

Feature: {{feature_id}}

Analyze the feature artifacts below. Produce a requirement analysis that identifies ambiguity, missing acceptance criteria, hidden assumptions, non-functional requirements, dependencies, and questions that materially affect implementation.

Do not invent business requirements. Clearly separate facts, assumptions, recommendations, and open questions.

{{artifacts}}
""",
    "architect.md": """# Architecture Task

Feature: {{feature_id}}

Act as an architecture advisor. Derive architecture drivers from the approved requirements, then produce at least two viable architecture options, trade-offs, risks, trust boundaries, data/integration choices, reliability considerations, and ADR recommendations. Prefer evidence over generic patterns.

Do not silently change approved requirements. Material decisions must be proposed as ADRs.

{{artifacts}}
""",
    "planner.md": """# Planning Task

Feature: {{feature_id}}

Create or improve an implementation plan and dependency-aware task graph. Every material task must trace to a requirement, acceptance criterion, architecture decision, or risk. Identify parallelizable work and explicit verification steps.

{{artifacts}}
""",
    "developer.md": """# Implementation Task

Feature: {{feature_id}}

Implement or propose the smallest change that satisfies the approved specification and architecture. Preserve existing behavior outside scope, add automated tests, follow repository conventions, and report any conflict between code and source-of-truth artifacts.

In advisory mode, return a concrete implementation/patch plan and do not modify files. In workspace-write mode, you may modify files inside the project workspace when the provider permits it.

{{artifacts}}
""",
    "reviewer.md": """# Review Task

Feature: {{feature_id}}

Review the current repository state against the specification, architecture, tasks, security constraints, and tests. Prioritize correctness, regressions, missing tests, architecture drift, security issues, and traceability gaps. Report actionable findings with evidence.

{{artifacts}}
""",
    "tester.md": """# Testing Task

Feature: {{feature_id}}

Design or implement tests that prove the acceptance criteria and important failure modes. Cover unit, integration, contract, resilience, and security tests where applicable. Identify untestable requirements or missing observability.

{{artifacts}}
""",
    "security.md": """# Security Task

Feature: {{feature_id}}

Perform a focused security design/review against the approved specification and architecture. Analyze trust boundaries, authentication, authorization, data exposure, secrets, injection risks, supply-chain risk, abuse cases, auditability, and least privilege. Distinguish confirmed findings from hypotheses.

{{artifacts}}
""",
    "documentation.md": """# Documentation Task

Feature: {{feature_id}}

Create or improve technical documentation that stays consistent with the specification, architecture, ADRs, implementation, tests, and operational behavior. Identify documentation drift explicitly.

{{artifacts}}
""",
    "general.md": """# SD-AI Task

Feature: {{feature_id}}
Capability: {{capability}}

Perform the requested lifecycle capability while following the specification, architecture, governance, and attached skills.

{{artifacts}}
""",
}

SKILLS = {
    "spec-traceability": {
        "manifest": """name: spec-traceability\ndescription: Keep requirements, architecture, tasks, code, tests, and review findings traceable.\ncapabilities: [requirements, architecture, planning, coding, review, testing, security, documentation]\n""",
        "instructions": """# Spec Traceability

- Reference requirement/acceptance IDs when proposing or implementing material work.
- Treat approved specs and ADRs as source-of-truth artifacts.
- Flag code or test behavior that cannot be traced to an approved requirement.
- Never resolve a conflict by silently changing the requirement.
""",
    },
    "architecture-review": {
        "manifest": """name: architecture-review\ndescription: Generate and evaluate architecture options using explicit quality attributes and ADRs.\ncapabilities: [architecture, review]\n""",
        "instructions": """# Architecture Review

- Start from architecture drivers and constraints.
- Compare at least two viable options when a material design choice exists.
- Evaluate scalability, reliability, security, operability, cost, complexity, and reversibility.
- Record material recommendations as ADR proposals with consequences and risks.
- Prefer C4/Mermaid and textual contracts that can be version controlled.
""",
    },
    "secure-coding": {
        "manifest": """name: secure-coding\ndescription: Apply secure implementation and review practices.\ncapabilities: [coding, review, security]\n""",
        "instructions": """# Secure Coding

- Apply least privilege and explicit trust boundaries.
- Do not expose credentials, tokens, private keys, or sensitive production data.
- Validate untrusted inputs and handle output/context encoding appropriately.
- Prefer safe APIs over ad-hoc parsing or shell execution.
- Treat dependency and supply-chain changes as security-relevant.
""",
    },
    "test-design": {
        "manifest": """name: test-design\ndescription: Build risk-based tests tied to acceptance criteria and failure modes.\ncapabilities: [testing, coding, review]\n""",
        "instructions": """# Test Design

- Trace tests to acceptance criteria and material NFRs.
- Cover success, boundary, failure, retry/idempotency, and authorization paths where relevant.
- Prefer deterministic tests and explicit test data.
- Flag acceptance criteria that cannot be observed or verified.
""",
    },
}


def init_project(root: Path) -> list[Path]:
    created: list[Path] = []
    created.append(write_text(root / ".sdai" / "constitution.yaml", CONSTITUTION, overwrite=False))
    created.append(write_text(root / ".sdai" / "config.yaml", CONFIG, overwrite=False))
    created.append(write_text(root / ".sdai" / "policies.yaml", POLICIES, overwrite=False))
    created.append(write_text(root / ".sdai" / "agents.yaml", AGENTS, overwrite=False))
    created.append(write_text(root / ".sdai" / "routing.yaml", ROUTING, overwrite=False))
    for name, content in WORKFLOWS.items():
        created.append(write_text(root / ".sdai" / "workflows" / f"{name}.yaml", content, overwrite=False))
    for name, content in PROMPTS.items():
        created.append(write_text(root / ".sdai" / "prompts" / name, content, overwrite=False))
    for name, content in SKILLS.items():
        created.append(write_text(root / ".sdai" / "skills" / name / "skill.yaml", content["manifest"], overwrite=False))
        created.append(write_text(root / ".sdai" / "skills" / name / "SKILL.md", content["instructions"], overwrite=False))
    (root / "specs").mkdir(parents=True, exist_ok=True)
    return created


def upgrade_project(root: Path) -> list[Path]:
    """Add v0.2 agent-platform files to an existing SD-AI project without overwriting user files."""
    if not (root / ".sdai" / "config.yaml").exists():
        raise FileNotFoundError("Not an SD-AI project. Run `sdai init` first.")
    created: list[Path] = []

    defaults = {
        root / ".sdai" / "agents.yaml": AGENTS,
        root / ".sdai" / "routing.yaml": ROUTING,
    }
    for path, content in defaults.items():
        if not path.exists():
            created.append(write_text(path, content, overwrite=False))

    for name, content in PROMPTS.items():
        path = root / ".sdai" / "prompts" / name
        if not path.exists():
            created.append(write_text(path, content, overwrite=False))

    for name, content in SKILLS.items():
        manifest = root / ".sdai" / "skills" / name / "skill.yaml"
        instructions = root / ".sdai" / "skills" / name / "SKILL.md"
        if not manifest.exists():
            created.append(write_text(manifest, content["manifest"], overwrite=False))
        if not instructions.exists():
            created.append(write_text(instructions, content["instructions"], overwrite=False))
    return created
