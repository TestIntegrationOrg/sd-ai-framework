from __future__ import annotations

from pathlib import Path

from sdai.artifacts import write_text


AGENTIC_WORKFLOW = """version: 5
name: agentic
validation_mode: critical
steps:
  - id: spec-baseline
    type: deterministic
    action: specify
    description: Create the traceable specification baseline.

  - id: requirements-review
    type: agent
    agent: requirements-analyst
    capability: requirements
    mode: advisory
    save_as: ai/requirements-review.md

  - id: architecture-baseline
    type: deterministic
    action: architect

  - id: architecture-review
    type: agent
    agent: architect
    capability: architecture
    mode: advisory
    save_as: ai/architecture-review.md

  - id: security-review
    type: agent
    agent: security-reviewer
    capability: security
    mode: advisory
    save_as: ai/security-review.md

  - id: architecture-approval
    type: approval
    gate: architecture
    description: Human approval required before workspace-writing implementation agents.

  - id: plan
    type: deterministic
    action: plan

  - id: implementation
    type: agent
    agent: developer
    capability: coding
    profile: codex
    mode: workspace-write
    save_as: ai/implementation.md

  - id: code-review
    type: agent
    agent: code-reviewer
    capability: review
    profile: copilot
    mode: advisory
    save_as: ai/code-review.md

  - id: testing
    type: agent
    agent: tester
    capability: testing
    profile: copilot
    mode: workspace-write
    save_as: ai/testing.md

  - id: validate
    type: validate
"""


ENTERPRISE_WORKFLOW = """version: 5
name: enterprise
validation_mode: critical
steps:
  - id: specification
    type: deterministic
    action: specify

  - id: requirements-review
    type: agent
    agent: requirements-analyst
    capability: requirements
    profile: claude
    mode: advisory
    retry:
      max_attempts: 2
      delay_seconds: 1
    save_as: ai/requirements-review.md

  - id: architecture-baseline
    type: deterministic
    action: architect

  - id: design-reviews
    type: parallel
    description: Independent read-only architecture and security reviews.
    steps:
      - id: architecture-review
        type: agent
        agent: architect
        capability: architecture
        profile: claude
        mode: advisory
        retry: 2
        save_as: ai/enterprise-architecture-review.md
      - id: security-review
        type: agent
        agent: security-reviewer
        capability: security
        profile: copilot
        mode: advisory
        retry: 2
        save_as: ai/enterprise-security-review.md

  - id: architecture-approval
    type: approval
    gate: enterprise-architecture

  - id: security-approval
    type: approval
    gate: enterprise-security

  - id: plan
    type: deterministic
    action: plan

  - id: implementation
    type: agent
    agent: developer
    capability: coding
    profile: codex
    mode: workspace-write
    retry:
      max_attempts: 2
      delay_seconds: 1
    save_as: ai/implementation.md

  - id: post-implementation-review
    type: parallel
    steps:
      - id: code-review
        type: agent
        agent: code-reviewer
        capability: review
        profile: copilot
        mode: advisory
        save_as: ai/code-review.md
      - id: test-review
        type: agent
        agent: tester
        capability: testing
        profile: claude
        mode: advisory
        save_as: ai/test-review.md

  - id: tests
    type: quality-gate
    gate: tests
    retry:
      max_attempts: 2
      delay_seconds: 1

  - id: trivy
    type: quality-gate
    gate: trivy
    if: env:SDAI_TRIVY
    on_failure: stop

  - id: sonar
    type: quality-gate
    gate: sonar
    if: env:SDAI_SONAR
    on_failure: stop

  - id: validate
    type: validate
"""


def install_v03_workflows(root: Path) -> list[Path]:
    created: list[Path] = []
    path = root / ".sdai" / "workflows" / "agentic.yaml"
    if not path.exists():
        created.append(write_text(path, AGENTIC_WORKFLOW, overwrite=False))
    return created


def install_v04_workflows(root: Path) -> list[Path]:
    created: list[Path] = []
    path = root / ".sdai" / "workflows" / "enterprise.yaml"
    if not path.exists():
        created.append(write_text(path, ENTERPRISE_WORKFLOW, overwrite=False))
    return created


def install_current_workflows(root: Path) -> list[Path]:
    return [*install_v03_workflows(root), *install_v04_workflows(root)]
