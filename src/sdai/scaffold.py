from __future__ import annotations

from pathlib import Path

from sdai.artifacts import write_text


CONSTITUTION = """version: 1
principles:
  - spec-first
  - architecture-as-code
  - human-approval-for-critical-changes
  - least-privilege-agents
engineering:
  language: unspecified
  architecture_style: unspecified
security:
  secrets_in_prompts: forbidden
  private_keys_in_prompts: forbidden
quality:
  tests_required: true
  traceability_required: true
"""

CONFIG = """version: 1
default_workflow: standard
provider: deterministic
artifact_root: specs
"""

POLICIES = """version: 1
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
"""

WORKFLOWS = {
    "light": """name: light\nsteps:\n  - implement\n  - validate\n""",
    "standard": """name: standard\nsteps:\n  - specify\n  - architect\n  - plan\n  - implement\n  - validate\n""",
    "critical": """name: critical\nsteps:\n  - specify\n  - architect\n  - security\n  - plan\n  - implement\n  - validate\n""",
}


def init_project(root: Path) -> list[Path]:
    created: list[Path] = []
    created.append(write_text(root / ".sdai" / "constitution.yaml", CONSTITUTION, overwrite=False))
    created.append(write_text(root / ".sdai" / "config.yaml", CONFIG, overwrite=False))
    created.append(write_text(root / ".sdai" / "policies.yaml", POLICIES, overwrite=False))
    for name, content in WORKFLOWS.items():
        created.append(write_text(root / ".sdai" / "workflows" / f"{name}.yaml", content, overwrite=False))
    (root / "specs").mkdir(parents=True, exist_ok=True)
    return created
