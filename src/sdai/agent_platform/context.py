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


def _read_bounded(path: Path, max_chars_per_file: int) -> str:
    text = path.read_text(encoding="utf-8")
    if len(text) > max_chars_per_file:
        text = text[:max_chars_per_file] + "\n\n[truncated by SD-AI]"
    return text


def collect_feature_context(context: FeatureContext, *, max_chars_per_file: int = 30_000) -> str:
    sections: list[str] = []
    seen: set[Path] = set()
    for relative in _CONTEXT_ARTIFACTS:
        path = context.artifact(relative)
        if not path.exists() or not path.is_file():
            continue
        sections.append(f"## Artifact: {relative}\n{_read_bounded(path, max_chars_per_file)}")
        seen.add(path.resolve())

    # External-agent outputs are durable lifecycle context for downstream agents.
    ai_root = context.artifact("ai")
    if ai_root.exists():
        for path in sorted(ai_root.rglob("*.md")):
            if path.resolve() in seen:
                continue
            relative = path.relative_to(context.feature_dir).as_posix()
            sections.append(f"## Artifact: {relative}\n{_read_bounded(path, max_chars_per_file)}")

    # Quality-gate reports are also relevant to review/security/documentation agents.
    gate_root = context.artifact("quality-gates")
    if gate_root.exists():
        for path in sorted(gate_root.rglob("*.md")):
            relative = path.relative_to(context.feature_dir).as_posix()
            sections.append(f"## Artifact: {relative}\n{_read_bounded(path, max_chars_per_file)}")
    return "\n\n".join(sections)


def load_governance_context(project_root: Path) -> str:
    sections: list[str] = []
    for relative in (
        ".sdai/constitution.yaml",
        ".sdai/policies.yaml",
        ".sdai/governance.yaml",
        ".sdai/approval-policies.yaml",
        ".sdai/quality-gates.yaml",
        ".sdai/integrations.yaml",
    ):
        path = project_root / relative
        if path.exists():
            sections.append(f"## {relative}\n{path.read_text(encoding='utf-8')}")
    return "\n\n".join(sections)
