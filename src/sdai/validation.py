from __future__ import annotations

from dataclasses import dataclass

from sdai.config import load_yaml
from sdai.models import FeatureContext, LifecycleMode


@dataclass(frozen=True)
class ValidationFinding:
    level: str
    code: str
    message: str


REQUIRED = {
    LifecycleMode.LIGHT: ["00-intake.md", "implementation-brief.md"],
    LifecycleMode.STANDARD: [
        "00-intake.md",
        "specification.md",
        "architecture/architecture.md",
        "architecture/decision-matrix.md",
        "adr/ADR-001-initial-architecture.md",
        "plan.md",
        "tasks.yaml",
        "implementation-brief.md",
    ],
    LifecycleMode.CRITICAL: [
        "00-intake.md",
        "specification.md",
        "architecture/architecture.md",
        "architecture/decision-matrix.md",
        "adr/ADR-001-initial-architecture.md",
        "security-review.md",
        "plan.md",
        "tasks.yaml",
        "implementation-brief.md",
    ],
}


def validate(context: FeatureContext, mode: LifecycleMode) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    for relative in REQUIRED[mode]:
        if not context.artifact(relative).exists():
            findings.append(ValidationFinding("ERROR", "ARTIFACT_MISSING", f"Missing {relative}"))

    tasks_path = context.artifact("tasks.yaml")
    if tasks_path.exists():
        tasks = load_yaml(tasks_path).get("tasks", [])
        for item in tasks:
            if not item.get("traces_to"):
                findings.append(
                    ValidationFinding("ERROR", "TASK_NOT_TRACEABLE", f"Task {item.get('id', '?')} has no traces_to")
                )

    spec_path = context.artifact("specification.md")
    if spec_path.exists():
        spec = spec_path.read_text(encoding="utf-8")
        for marker in ("FR-001", "NFR-001", "AC-001"):
            if marker not in spec:
                findings.append(ValidationFinding("ERROR", "SPEC_BASELINE", f"Specification missing {marker}"))

    adr_path = context.artifact("adr/ADR-001-initial-architecture.md")
    if adr_path.exists() and "Status: Proposed" in adr_path.read_text(encoding="utf-8"):
        findings.append(
            ValidationFinding("WARN", "ADR_PROPOSED", "Initial architecture ADR is still Proposed; approve it before critical implementation")
        )

    return findings


def has_blockers(findings: list[ValidationFinding]) -> bool:
    return any(f.level == "ERROR" for f in findings)
