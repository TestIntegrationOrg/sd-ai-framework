from __future__ import annotations

from pathlib import Path

from sdai.models import FeatureContext
from sdai.path_safety import ensure_within_project
from sdai.text import read_utf8_text


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


_ARCHITECTURE_CONTEXT_ROOTS = ("rfc", "architecture", "adr", "contracts", "security")
_ARCHITECTURE_CONTEXT_SUFFIXES = {
    ".md",
    ".mmd",
    ".puml",
    ".plantuml",
    ".yaml",
    ".yml",
    ".json",
    ".proto",
}


def _read_bounded(path: Path, max_chars_per_file: int, *, root: Path) -> str:
    safe = ensure_within_project(root, path, label="agent context file")
    text = read_utf8_text(safe)
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

    # RFCs, architecture-as-code diagrams, ADRs, contracts, and threat-model artifacts
    # are durable design context for downstream developer/test/security/review agents.
    # Draw.io XML is intentionally not injected by default because it is typically a
    # presentation derivative and can be very large; the corresponding C4/PlantUML/
    # Mermaid sources should carry the machine-readable architecture semantics.
    for relative_root in _ARCHITECTURE_CONTEXT_ROOTS:
        root = context.artifact(relative_root)
        if not root.exists() or not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            safe = ensure_within_project(
                context.feature_dir, path, label="architecture artifact context"
            )
            if not safe.is_file() or safe.suffix.lower() not in _ARCHITECTURE_CONTEXT_SUFFIXES:
                continue
            if safe.resolve() in seen:
                continue
            relative = safe.relative_to(context.feature_dir).as_posix()
            sections.append(
                f"## Artifact: {relative}\n"
                f"{_read_bounded(safe, max_chars_per_file, root=context.feature_dir)}"
            )
            seen.add(safe.resolve())

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
