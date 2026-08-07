from __future__ import annotations

from pathlib import Path

from sdai.models import FeatureContext
from sdai.path_safety import ensure_within_project


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


def _read_bounded(path: Path, max_chars_per_file: int, *, root: Path) -> str:
    safe = ensure_within_project(root, path, label="agent context file")
    text = safe.read_text(encoding="utf-8")
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
        sections.append(
            f"## Artifact: {relative}\n"
            f"{_read_bounded(path, max_chars_per_file, root=context.feature_dir)}"
        )
        seen.add(path.resolve())

    # External-agent outputs are durable lifecycle context for downstream agents.
    ai_root = context.artifact("ai")
    if ai_root.exists():
        for path in sorted(ai_root.rglob("*.md")):
            safe = ensure_within_project(context.feature_dir, path, label="AI artifact context")
            if safe.resolve() in seen or not safe.is_file():
                continue
            relative = safe.relative_to(context.feature_dir).as_posix()
            sections.append(
                f"## Artifact: {relative}\n"
                f"{_read_bounded(safe, max_chars_per_file, root=context.feature_dir)}"
            )

    # Quality-gate reports are also relevant to review/security/documentation agents.
    gate_root = context.artifact("quality-gates")
    if gate_root.exists():
        for path in sorted(gate_root.rglob("*.md")):
            safe = ensure_within_project(context.feature_dir, path, label="quality-gate context")
            if not safe.is_file():
                continue
            relative = safe.relative_to(context.feature_dir).as_posix()
            sections.append(
                f"## Artifact: {relative}\n"
                f"{_read_bounded(safe, max_chars_per_file, root=context.feature_dir)}"
            )
    return "\n\n".join(sections)


def load_governance_context(project_root: Path, *, max_chars_per_file: int = 30_000) -> str:
    project_root = project_root.resolve()
    sections: list[str] = []
    for relative in (
        ".sdai/constitution.yaml",
        ".sdai/policies.yaml",
        ".sdai/governance.yaml",
        ".sdai/approval-policies.yaml",
        ".sdai/quality-gates.yaml",
        ".sdai/integrations.yaml",
        ".sdai/policy.yaml",
    ):
        path = ensure_within_project(
            project_root, project_root / relative, label=f"governance context {relative}"
        )
        if path.exists() and path.is_file():
            sections.append(
                f"## {relative}\n"
                f"{_read_bounded(path, max_chars_per_file, root=project_root)}"
            )
    return "\n\n".join(sections)
