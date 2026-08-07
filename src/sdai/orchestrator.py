from __future__ import annotations

from pathlib import Path

from sdai.agents import ArchitectAgent, DeveloperAgent, PlannerAgent, RequirementAgent, SecurityAgent
from sdai.agents.base import AgentResult
from sdai.config import load_yaml
from sdai.models import FeatureContext, LifecycleMode
from sdai.validation import ValidationFinding, validate


AGENTS = {
    "specify": RequirementAgent(),
    "architect": ArchitectAgent(),
    "plan": PlannerAgent(),
    "implement": DeveloperAgent(),
    "security": SecurityAgent(),
}


class Orchestrator:
    def __init__(self, project_root: Path):
        self.project_root = project_root.resolve()

    def context(self, feature_id: str) -> FeatureContext:
        return FeatureContext(self.project_root, feature_id)

    def run_step(self, feature_id: str, step: str) -> AgentResult | list[ValidationFinding]:
        context = self.context(feature_id)
        if step == "validate":
            return validate(context, LifecycleMode.STANDARD)
        if step not in AGENTS:
            raise ValueError(f"Unknown workflow step: {step}")
        return AGENTS[step].run(context)

    def run_workflow(self, feature_id: str, workflow: LifecycleMode) -> list[tuple[str, object]]:
        path = self.project_root / ".sdai" / "workflows" / f"{workflow.value}.yaml"
        definition = load_yaml(path)
        results: list[tuple[str, object]] = []
        context = self.context(feature_id)
        for step in definition.get("steps", []):
            if step == "validate":
                result = validate(context, workflow)
            else:
                if step not in AGENTS:
                    raise ValueError(f"Unknown workflow step: {step}")
                result = AGENTS[step].run(context)
            results.append((step, result))
        return results
