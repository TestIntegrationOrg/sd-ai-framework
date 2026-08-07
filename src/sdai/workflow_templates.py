from __future__ import annotations

from pathlib import Path

from sdai.artifacts import write_text


AGENTIC_WORKFLOW = """version: 3
name: agentic
validation_mode: critical
steps:
  - id: spec-baseline
    type: deterministic
    action: specify
    description: Create the traceable specification baseline.

  - id: requirements-review
    type: agent
    capability: requirements
    mode: advisory
    save_as: ai/requirements-review.md

  - id: architecture-baseline
    type: deterministic
    action: architect

  - id: architecture-review
    type: agent
    capability: architecture
    mode: advisory
    save_as: ai/architecture-review.md

  - id: security-review
    type: agent
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
    capability: coding
    profile: codex
    mode: workspace-write
    save_as: ai/implementation.md

  - id: code-review
    type: agent
    capability: review
    profile: copilot
    mode: advisory
    save_as: ai/code-review.md

  - id: testing
    type: agent
    capability: testing
    profile: copilot
    mode: workspace-write
    save_as: ai/testing.md

  - id: validate
    type: validate
"""


def install_v03_workflows(root: Path) -> list[Path]:
    """Install v0.3 workflow examples without overwriting team customizations."""
    created: list[Path] = []
    path = root / ".sdai" / "workflows" / "agentic.yaml"
    if not path.exists():
        created.append(write_text(path, AGENTIC_WORKFLOW, overwrite=False))
    return created
