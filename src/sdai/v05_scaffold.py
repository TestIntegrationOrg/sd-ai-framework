from __future__ import annotations

from pathlib import Path

from sdai.architecture_skills import (
    ARCHITECT_V051,
    ARCHITECT_V052,
    ARCHITECT_V053,
    SKILLS as ARCHITECTURE_SKILLS,
    _skill_markdown,
    _skill_metadata,
)
from sdai.artifacts import write_text
from sdai.architecture_artifact_validator import scaffold_architecture_validation


AGENT_ROUTING = """version: 1
routes:
  requirements: requirements-analyst
  architecture: architect
  planning: planner
  coding: developer
  review: code-reviewer
  testing: tester
  security: security-reviewer
  documentation: documentation-writer
"""


AGENTS: dict[str, str] = {
    "requirements-analyst": """---
name: requirements-analyst
description: Analyze product requirements, ambiguity, NFRs, and acceptance criteria without inventing business intent.
capabilities: [requirements]
skills: [requirements-analysis, spec-traceability]
profile: claude
execution_mode: advisory
providers: {}
---
# Requirements Analyst

Convert product/Jira/business intent into explicit requirements, NFRs, constraints, acceptance criteria, assumptions, dependencies, and open questions. Separate facts from assumptions and do not write implementation code.
""",
    "architect": ARCHITECT_V053,
    "planner": """---
name: planner
description: Convert approved specification and architecture into a dependency-aware implementation plan and task graph.
capabilities: [planning]
skills: [implementation-planning, spec-traceability]
profile: codex
execution_mode: advisory
providers: {}
---
# Planner

Plan only from approved requirements and architecture. Produce dependency-aware, independently verifiable tasks with explicit requirement/ADR traceability, sequencing, parallelism, migration considerations, and validation steps. Do not silently change architecture.
""",
    "developer": """---
name: developer
description: Implement approved tasks safely within specification and architecture boundaries.
capabilities: [coding]
skills: [secure-coding, test-design, spec-traceability]
profile: codex
execution_mode: workspace-write
providers: {}
---
# Developer

Implement the smallest change that satisfies approved requirements, architecture, and tasks. Preserve behavior outside scope, add tests, follow repository conventions, and surface any material architecture divergence as a proposed decision rather than silently changing source-of-truth artifacts.
""",
    "code-reviewer": """---
name: code-reviewer
description: Review implementation for correctness, security, tests, architecture drift, and specification traceability.
capabilities: [review]
skills: [architecture-review, secure-coding, test-design, spec-traceability]
profile: copilot
execution_mode: advisory
providers: {}
---
# Code Reviewer

Review repository changes against requirements, architecture, ADRs, security constraints, and tests. Prioritize correctness, regression risk, security, missing tests, architecture drift, and traceability gaps. Provide evidence and actionable findings.
""",
    "tester": """---
name: tester
description: Design risk-based tests that prove acceptance criteria and important failure modes.
capabilities: [testing]
skills: [test-design, spec-traceability]
profile: copilot
execution_mode: advisory
providers: {}
---
# Tester

Design tests from acceptance criteria, architecture contracts, and failure modes. Cover unit, integration, contract, resilience, security, retry/idempotency, and observability behavior when relevant. Flag untestable requirements explicitly.
""",
    "security-reviewer": """---
name: security-reviewer
description: Review architecture and implementation for trust boundaries, abuse cases, least privilege, and security controls.
capabilities: [security, review]
skills: [secure-coding, architecture-review, spec-traceability]
profile: claude
execution_mode: advisory
providers: {}
---
# Security Reviewer

Review trust boundaries, identity, authorization, secrets, data exposure, injection, supply chain, abuse cases, auditability, and least privilege against approved requirements and architecture. Distinguish confirmed findings from hypotheses and do not invent compliance claims.
""",
    "documentation-writer": """---
name: documentation-writer
description: Keep technical documentation aligned with approved architecture, implementation, tests, and operational behavior.
capabilities: [documentation]
skills: [documentation-quality, spec-traceability]
profile: gemini
execution_mode: advisory
providers: {}
---
# Documentation Writer

Create or improve maintainable technical documentation that stays consistent with approved requirements, architecture, ADRs, contracts, implementation, tests, and operations. Identify documentation drift instead of silently normalizing contradictions.
""",
}


BASE_SKILLS: dict[str, dict[str, object]] = {
    "requirements-analysis": {
        "description": "Turn product intent into explicit requirements, NFRs, acceptance criteria, assumptions, and open questions.",
        "capabilities": ["requirements"],
        "instructions": """# Requirements Analysis

- Separate business facts, assumptions, constraints, dependencies, and open questions.
- Make acceptance criteria observable and testable.
- Identify NFRs that materially affect architecture: scale, latency, availability, security, compliance, operability, data lifecycle, cost, and compatibility.
- Do not invent business behavior to fill an ambiguity; surface it for clarification.
- Assign or preserve stable requirement/acceptance identifiers when the repository uses them.
""",
    },
    "implementation-planning": {
        "description": "Create dependency-aware implementation plans tied to approved requirements and architecture decisions.",
        "capabilities": ["planning"],
        "instructions": """# Implementation Planning

- Decompose work into small, verifiable tasks with explicit dependencies.
- Trace material tasks to requirements, acceptance criteria, ADRs, contracts, or risks.
- Identify parallelizable work, migrations, compatibility requirements, feature flags, rollout, rollback, and verification.
- Keep architecture decisions out of implementation tasks unless they are already approved; unresolved architecture belongs in an ADR/RFC proposal.
""",
    },
    "spec-traceability": {
        "description": "Keep requirements, architecture, ADRs, tasks, code, tests, and review findings traceable.",
        "capabilities": ["requirements", "architecture", "planning", "coding", "review", "testing", "security", "documentation"],
        "instructions": """# Spec Traceability

- Reference requirement/acceptance IDs when proposing or implementing material work.
- Treat approved specifications and ADRs as source-of-truth artifacts.
- Flag code, tests, contracts, or diagrams that cannot be traced to approved intent.
- Never resolve a conflict by silently changing the requirement or architecture.
""",
    },
    "secure-coding": {
        "description": "Apply secure implementation and review practices.",
        "capabilities": ["coding", "review", "security"],
        "instructions": """# Secure Coding

- Apply least privilege and explicit trust boundaries.
- Do not expose credentials, tokens, private keys, or sensitive production data.
- Validate untrusted inputs and use safe output/context encoding.
- Prefer safe APIs over ad-hoc parsing, unsafe deserialization, or shell execution.
- Treat dependency, build, and supply-chain changes as security relevant.
""",
    },
    "test-design": {
        "description": "Build risk-based tests tied to acceptance criteria, contracts, and failure modes.",
        "capabilities": ["testing", "coding", "review"],
        "instructions": """# Test Design

- Trace tests to acceptance criteria and material NFRs.
- Cover success, boundary, failure, timeout, retry/idempotency, authorization, and concurrency paths where relevant.
- Add integration/contract/resilience tests where unit tests cannot prove system behavior.
- Prefer deterministic tests and explicit test data.
- Flag acceptance criteria that cannot be observed or verified.
""",
    },
    "documentation-quality": {
        "description": "Produce durable technical documentation that stays aligned with the implemented and approved system.",
        "capabilities": ["documentation", "review"],
        "instructions": """# Documentation Quality

- Keep terminology, component names, contracts, and operational behavior consistent with approved architecture and code.
- Prefer concise version-controlled source over screenshots when the source can be rendered.
- Include prerequisites, failure behavior, verification, migration, and rollback guidance when relevant.
- Identify documentation drift explicitly instead of repeating stale claims.
""",
    },
}


SKILLS: dict[str, dict[str, object]] = {**BASE_SKILLS, **ARCHITECTURE_SKILLS}
SHARED_SKILLS = SKILLS
AGENT_FILES = AGENTS


def _write_missing(path: Path, content: str, created: list[Path]) -> None:
    if not path.exists():
        created.append(write_text(path, content, overwrite=False))


def install_v05_scaffold(root: Path) -> list[Path]:
    """Install canonical semantic agents and shared skills without overwriting team customizations.

    v0.5.3 extends the v0.5 scaffold with the architecture authoring skill pack and
    architecture-artifact lifecycle validation. An
    existing Architect definition is upgraded only when it exactly matches the stock
    v0.5.1 definition; customized agent files and skill files remain untouched.
    """
    if not (root / ".sdai" / "config.yaml").exists():
        raise FileNotFoundError("Not an SD-AI project. Run `sdai init` first.")

    created: list[Path] = []
    _write_missing(root / ".sdai" / "agent-routing.yaml", AGENT_ROUTING, created)
    _write_missing(
        root / ".sdai" / "architecture-validation.yaml",
        scaffold_architecture_validation(),
        created,
    )

    for name, content in AGENTS.items():
        path = root / ".sdai" / "agents" / f"{name}.agent.md"
        if name == "architect" and path.exists():
            existing = path.read_text(encoding="utf-8").strip()
            if existing in {ARCHITECT_V051.strip(), ARCHITECT_V052.strip()}:
                created.append(write_text(path, ARCHITECT_V053, overwrite=True))
                continue
        _write_missing(path, content, created)

    for name, spec in SKILLS.items():
        skill_root = root / ".agents" / "skills" / name
        _write_missing(
            skill_root / "SKILL.md",
            _skill_markdown(name, str(spec["description"]), str(spec["instructions"])),
            created,
        )
        _write_missing(
            skill_root / "sdai.yaml",
            _skill_metadata(list(spec["capabilities"])),
            created,
        )

    return created


# Compatibility aliases for callers/tests that imported an earlier helper name.
install_v05 = install_v05_scaffold
scaffold_v05 = install_v05_scaffold
