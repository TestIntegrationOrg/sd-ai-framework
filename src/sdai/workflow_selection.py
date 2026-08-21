from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import TextIO

from sdai.config import load_yaml
from sdai.models import FeatureContext
from sdai.workflows import StepKind, load_workflow


_REQUESTED = re.compile(r"^## Requested Lifecycle\s*\n([^\n]+)", re.MULTILINE)
_PURPOSES = {
    "light": "Deterministic; minimal lifecycle for small, low-risk changes",
    "standard": "Deterministic; specification, architecture, plan, implementation artifact, validation",
    "critical": "Deterministic; standard plus security analysis and critical validation",
    "agentic": "Agent-enabled; reviews, approval, workspace implementation, review, and testing",
    "enterprise": "Agent-enabled; parallel reviews, approvals, retries, and quality/security gates",
}


@dataclass(frozen=True)
class WorkflowChoice:
    name: str
    summary: str


def configured_default(project_root: Path) -> str:
    data = load_yaml(project_root.resolve() / ".sdai" / "config.yaml")
    value = str(data.get("default_workflow") or "standard").strip()
    if not value:
        raise ValueError("default_workflow must be non-empty")
    return value


def workflow_choices(project_root: Path) -> tuple[WorkflowChoice, ...]:
    root = project_root.resolve()
    directory = root / ".sdai" / "workflows"
    choices: list[WorkflowChoice] = []
    for path in sorted(directory.glob("*.yaml"), key=lambda item: item.name):
        definition = load_workflow(root, path.stem)
        summary = _PURPOSES.get(definition.name)
        if summary is None:
            agent_steps = sum(
                1 for step, _ in definition.iter_steps() if step.kind == StepKind.AGENT
            )
            approvals = sum(
                1 for step, _ in definition.iter_steps() if step.kind == StepKind.APPROVAL
            )
            quality = sum(
                1 for step, _ in definition.iter_steps() if step.kind == StepKind.QUALITY_GATE
            )
            style = "Agent-enabled" if agent_steps else "Deterministic"
            summary = (
                f"{style}; {len(tuple(definition.iter_steps()))} steps, "
                f"{approvals} approvals, {quality} quality gates"
            )
        choices.append(WorkflowChoice(definition.name, summary))
    if not choices:
        raise ValueError("No workflows found under .sdai/workflows")
    return tuple(choices)


def select_workflow(
    project_root: Path,
    *,
    requested: str | None,
    interactive: bool,
    input_stream: TextIO,
    output_stream: TextIO,
) -> str:
    default = configured_default(project_root)
    try:
        choices = workflow_choices(project_root)
    except ValueError:
        if requested is None and not interactive:
            print(f"Resolved workflow '{default}' from .sdai/config.yaml", file=output_stream)
            return default
        raise
    names = [choice.name for choice in choices]
    if default not in names:
        raise ValueError(
            f"Configured default workflow '{default}' is unavailable; valid workflows: "
            + ", ".join(names)
        )
    if requested is not None:
        if requested not in names:
            raise ValueError(
                f"Unknown workflow '{requested}'; valid workflows: " + ", ".join(names)
            )
        return requested
    if not interactive:
        print(f"Resolved workflow '{default}' from .sdai/config.yaml", file=output_stream)
        return default

    print("Select a workflow:\n", file=output_stream)
    for index, choice in enumerate(choices, start=1):
        suffix = " [default]" if choice.name == default else ""
        print(f"  {index}. {choice.name:<12} {choice.summary}{suffix}", file=output_stream)
    default_index = names.index(default) + 1
    while True:
        print(f"\nWorkflow [{default_index}]: ", end="", file=output_stream, flush=True)
        value = input_stream.readline()
        if value == "":
            raise ValueError("Workflow selection ended before a choice was made; use --workflow")
        value = value.strip()
        if not value:
            return default
        if value in names:
            return value
        if value.isdigit() and 1 <= int(value) <= len(choices):
            return choices[int(value) - 1].name
        print(
            f"Invalid selection '{value}'. Enter 1..{len(choices)} or a workflow name.",
            file=output_stream,
        )


def feature_workflow(project_root: Path, feature_id: str) -> str | None:
    path = FeatureContext(project_root.resolve(), feature_id).artifact("00-intake.md")
    if not path.exists():
        return None
    match = _REQUESTED.search(path.read_text(encoding="utf-8"))
    return match.group(1).strip() if match else None


def resolve_feature_workflow(
    project_root: Path,
    feature_id: str,
    requested: str | None,
) -> str:
    return requested or feature_workflow(project_root, feature_id) or configured_default(project_root)


__all__ = [
    "WorkflowChoice",
    "configured_default",
    "feature_workflow",
    "resolve_feature_workflow",
    "select_workflow",
    "workflow_choices",
]
