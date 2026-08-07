from __future__ import annotations

from pathlib import Path

from sdai.models import FeatureContext


_CONTEXT_ARTIFACTS = (
    "00-intake.md",
    "specification.md",
    "architecture/architecture.md",
    "architecture/decision-matrix.md",
    "adr/ADR-001-initial-architecture.md",
    "plan.md",
    "tasks.yaml",
    "security-review.md",
    "implementation-brief.md",
)


def collect_feature_context(context: FeatureContext, *, max_chars_per_file: int = 30_000) -> str:
    sections: list[str] = []
    for relative in _CONTEXT_ARTIFACTS:
        path = context.artifact(relative)
        if not path.exists() or not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        if len(text) > max_chars_per_file:
            text = text[:max_chars_per_file] + "\n\n[truncated by SD-AI]"
        sections.append(f"## Artifact: {relative}\n{text}")
    return "\n\n".join(sections)


def load_governance_context(project_root: Path) -> str:
    sections: list[str] = []
    for relative in (".sdai/constitution.yaml", ".sdai/policies.yaml"):
        path = project_root / relative
        if path.exists():
            sections.append(f"## {relative}\n{path.read_text(encoding='utf-8')}")
    return "\n\n".join(sections)
