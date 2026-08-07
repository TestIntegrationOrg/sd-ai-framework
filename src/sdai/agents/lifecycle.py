from __future__ import annotations

import re

from sdai.agents.base import Agent, AgentResult
from sdai.artifacts import read_text, write_text
from sdai.models import FeatureContext


def _section(text: str, key: str, default: str = "TBD") -> str:
    pattern = rf"^## {re.escape(key)}\s*$\n(.*?)(?=\n## |\Z)"
    match = re.search(pattern, text, flags=re.MULTILINE | re.DOTALL)
    return match.group(1).strip() if match else default


class RequirementAgent(Agent):
    name = "requirement"

    def run(self, context: FeatureContext) -> AgentResult:
        intake = read_text(context.artifact("00-intake.md"))
        title = _section(intake, "Title", context.feature_id)
        description = _section(intake, "Description")
        spec = f"""# Specification — {context.feature_id}

## Title
{title}

## Problem
{description}

## Goals
- Deliver the requested capability with observable, testable behavior.
- Preserve traceability from requirement through architecture, tasks, and validation.

## Non-Goals
- Unapproved scope expansion.
- Architecture changes not documented by an ADR.

## Functional Requirements
- FR-001: The implementation MUST satisfy the stated problem and goals.
- FR-002: User-visible behavior MUST have acceptance criteria.

## Non-Functional Requirements
- NFR-001: Security and privacy constraints MUST be evaluated.
- NFR-002: Failure modes and observability MUST be considered.
- NFR-003: Performance/scalability assumptions MUST be stated before implementation.

## Acceptance Criteria
- AC-001: Functional requirements are implemented and tested.
- AC-002: Required architecture and security reviews pass for this lifecycle mode.
- AC-003: `sdai validate {context.feature_id}` reports no blocking violations.

## Open Questions
- Confirm domain-specific scale, latency, availability, and compliance targets.
- Confirm integration contracts and backward-compatibility requirements.
"""
        path = write_text(context.artifact("specification.md"), spec)
        return AgentResult(self.name, [path], "Created specification and acceptance baseline")


class ArchitectAgent(Agent):
    name = "architect"

    def run(self, context: FeatureContext) -> AgentResult:
        spec = read_text(context.artifact("specification.md"))
        problem = _section(spec, "Problem")
        architecture = f"""# Architecture — {context.feature_id}

## Architecture Drivers
- Requirements and acceptance criteria in `../specification.md`.
- Security, reliability, operability, and maintainability.
- Minimize irreversible decisions until constraints justify them.

## Problem Context
{problem}

## Candidate Options

### Option A — Modular component within the existing deployable
Best when coupling is local, scale is aligned with the current service, and independent deployment is not required.

### Option B — Independently deployable service
Best when ownership, scaling, failure isolation, or deployment cadence must be independent.

### Option C — Event-driven capability
Best when asynchronous processing, buffering, fan-out, or failure isolation dominate the requirements.

## Recommendation
Choose only after replacing the default scores in `decision-matrix.md` with evidence from real constraints. The framework intentionally does not let an AI silently turn assumptions into architecture decisions.

## Components
- API / ingress boundary
- Domain/application service
- Persistence/integration boundary
- Observability boundary

## Data and Integration
Document synchronous APIs with OpenAPI and asynchronous contracts with AsyncAPI/JSON Schema when applicable.

## Security
Apply least privilege, secret isolation, encryption, input validation, auditable operations, and explicit trust boundaries.

## Reliability
Document retry behavior, idempotency, timeouts, backpressure, and recovery expectations.

## Observability
Define structured logs, metrics, traces, correlation identifiers, and actionable alerts.

## Traceability
Architecture decisions MUST reference requirement IDs (FR/NFR) and material changes MUST create or update an ADR.
"""
        matrix = """# Architecture Decision Matrix

Replace sample scores with evidence. Score 1 (poor) to 5 (strong).

| Quality Attribute | Option A | Option B | Option C | Evidence / Notes |
|---|---:|---:|---:|---|
| Simplicity | 5 | 3 | 3 | TBD |
| Independent scaling | 2 | 5 | 5 | TBD |
| Failure isolation | 2 | 5 | 4 | TBD |
| Operational cost | 5 | 3 | 3 | TBD |
| Async resilience | 2 | 3 | 5 | TBD |
| Change isolation | 2 | 5 | 4 | TBD |

## Decision
TBD — a human architect or approved policy must record the selected option and rationale.
"""
        context_mmd = """flowchart LR
    User[User / Calling System] --> System[Feature System]
    System --> External[External Dependencies]
"""
        container_mmd = """flowchart LR
    Ingress[API / Ingress] --> App[Application / Domain]
    App --> Data[(Data Store)]
    App --> Integration[Integration Boundary]
    App --> Obs[Observability]
"""
        adr = f"""# ADR-001: Initial architecture for {context.feature_id}

- Status: Proposed
- Date: TBD

## Context
See `../specification.md` and `../architecture/decision-matrix.md`.

## Decision
TBD after architecture review.

## Consequences
TBD. Record positive consequences, negative consequences, risks, and follow-up actions.
"""
        paths = [
            write_text(context.artifact("architecture/architecture.md"), architecture),
            write_text(context.artifact("architecture/decision-matrix.md"), matrix),
            write_text(context.artifact("architecture/context.mmd"), context_mmd),
            write_text(context.artifact("architecture/container.mmd"), container_mmd),
            write_text(context.artifact("adr/ADR-001-initial-architecture.md"), adr),
        ]
        return AgentResult(self.name, paths, "Created architecture options, decision matrix, diagrams, and ADR")


class PlannerAgent(Agent):
    name = "planner"

    def run(self, context: FeatureContext) -> AgentResult:
        read_text(context.artifact("specification.md"))
        read_text(context.artifact("architecture/architecture.md"))
        plan = f"""# Implementation Plan — {context.feature_id}

## Preconditions
- Specification reviewed.
- Architecture decision recorded for material design choices.
- Open questions that can change implementation are resolved.

## Workstreams
1. Contracts and domain model
2. Core implementation
3. Integration/persistence
4. Tests
5. Security and resilience
6. Observability
7. Documentation and rollout

## Exit Criteria
- All blocking tasks complete.
- Tests pass.
- Security findings resolved or accepted.
- Specification and architecture validation pass.
"""
        tasks = """version: 1
tasks:
  - id: T-001
    title: Implement contracts and domain behavior
    traces_to: [FR-001]
    status: todo
  - id: T-002
    title: Add automated tests for acceptance criteria
    traces_to: [AC-001]
    status: todo
  - id: T-003
    title: Implement observability and failure handling
    traces_to: [NFR-002]
    status: todo
  - id: T-004
    title: Complete security review
    traces_to: [NFR-001]
    status: todo
  - id: T-005
    title: Run SD-AI validation
    traces_to: [AC-003]
    status: todo
"""
        paths = [
            write_text(context.artifact("plan.md"), plan),
            write_text(context.artifact("tasks.yaml"), tasks),
        ]
        return AgentResult(self.name, paths, "Created implementation plan and traceable task graph")


class DeveloperAgent(Agent):
    name = "developer"

    def run(self, context: FeatureContext) -> AgentResult:
        read_text(context.artifact("00-intake.md"))
        spec = context.artifact("specification.md")
        architecture = context.artifact("architecture/architecture.md")
        plan = context.artifact("plan.md")
        brief = f"""# AI Implementation Brief — {context.feature_id}

## Source of Truth
- Intake: `{context.artifact('00-intake.md').relative_to(context.project_root)}`
- Specification: `{spec.relative_to(context.project_root) if spec.exists() else 'not-created (light workflow)'}`
- Architecture: `{architecture.relative_to(context.project_root) if architecture.exists() else 'not-created (light workflow)'}`
- Plan: `{plan.relative_to(context.project_root) if plan.exists() else 'not-created (light workflow)'}`

## Agent Rules
1. Do not invent new requirements.
2. Do not change a material architecture decision without proposing an ADR.
3. Prefer the smallest change that satisfies acceptance criteria.
4. Add/update automated tests with code changes.
5. Never place secrets, credentials, private keys, or sensitive production data in prompts or logs.
6. Report assumptions and unresolved blockers explicitly.

## Execution Boundary
This MVP creates the implementation contract but does not autonomously modify application source. A coding-agent adapter will consume this artifact in a future milestone.
"""
        path = write_text(context.artifact("implementation-brief.md"), brief)
        return AgentResult(self.name, [path], "Created bounded implementation brief for a coding agent")


class SecurityAgent(Agent):
    name = "security"

    def run(self, context: FeatureContext) -> AgentResult:
        read_text(context.artifact("specification.md"))
        review = """# Security Review

## Trust Boundaries
- Identify callers, services, data stores, queues/topics, external systems, and administrative paths.

## Required Checks
- [ ] Authentication and authorization documented
- [ ] Least privilege applied
- [ ] Input validation / output encoding considered
- [ ] Secret handling documented
- [ ] Encryption in transit and at rest evaluated
- [ ] Sensitive logging prohibited
- [ ] Dependency / supply-chain risk considered
- [ ] Abuse and denial-of-service scenarios considered
- [ ] Auditability requirements documented

## Findings
No finding may be marked resolved without evidence or an explicit risk acceptance.
"""
        path = write_text(context.artifact("security-review.md"), review)
        return AgentResult(self.name, [path], "Created security review checklist")
