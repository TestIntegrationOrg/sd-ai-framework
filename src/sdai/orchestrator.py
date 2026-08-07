from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sdai.agent_platform import AgentRuntime, ExecutionMode
from sdai.agent_platform.models import AgentInvocation
from sdai.agents import ArchitectAgent, DeveloperAgent, PlannerAgent, RequirementAgent, SecurityAgent
from sdai.agents.base import AgentResult
from sdai.artifacts import write_text
from sdai.models import FeatureContext, LifecycleMode
from sdai.validation import ValidationFinding, has_blockers, validate
from sdai.workflows import (
    StepKind,
    WorkflowDefinition,
    WorkflowStep,
    is_approved,
    load_workflow,
    load_workflow_state,
    save_workflow_state,
)


AGENTS = {
    "specify": RequirementAgent(),
    "architect": ArchitectAgent(),
    "plan": PlannerAgent(),
    "implement": DeveloperAgent(),
    "security": SecurityAgent(),
}


@dataclass(frozen=True)
class StepExecution:
    step_id: str
    kind: StepKind
    status: str
    result: object | None = None
    message: str = ""


class Orchestrator:
    def __init__(self, project_root: Path, *, agent_runtime: AgentRuntime | None = None):
        self.project_root = project_root.resolve()
        self.agent_runtime = agent_runtime or AgentRuntime(self.project_root)

    def context(self, feature_id: str) -> FeatureContext:
        return FeatureContext(self.project_root, feature_id)

    def run_step(self, feature_id: str, step: str) -> AgentResult | list[ValidationFinding]:
        """Backward-compatible direct deterministic step execution."""
        context = self.context(feature_id)
        if step == "validate":
            return validate(context, LifecycleMode.STANDARD)
        if step not in AGENTS:
            raise ValueError(f"Unknown workflow step: {step}")
        return AGENTS[step].run(context)

    @staticmethod
    def _invalidate_from(state, definition: WorkflowDefinition, step_id: str) -> None:
        ids = [item.id for item in definition.steps]
        index = ids.index(step_id)
        invalid = set(ids[index:])
        state.completed_steps = [value for value in state.completed_steps if value not in invalid]
        state.last_status = "running"
        state.paused_at = None

    @staticmethod
    def _prior_approval_gates(definition: WorkflowDefinition, step_id: str) -> list[str]:
        gates: list[str] = []
        for item in definition.steps:
            if item.id == step_id:
                break
            if item.kind == StepKind.APPROVAL:
                gates.append(item.gate or item.id)
        return gates

    def _execute_workflow_step(
        self,
        feature_id: str,
        definition: WorkflowDefinition,
        step: WorkflowStep,
        *,
        force: bool = False,
        dry_run: bool = False,
        profile_override: str | None = None,
        mode_override: ExecutionMode | None = None,
    ) -> StepExecution:
        context = self.context(feature_id)
        state = load_workflow_state(context, definition.name)

        if force and state.is_complete(step.id):
            # A deliberate rerun invalidates this step and all downstream completion
            # markers so later workflow execution cannot rely on stale derived artifacts.
            self._invalidate_from(state, definition, step.id)
            save_workflow_state(context, state)

        # Approval steps are always re-evaluated against the durable approval artifact.
        # Deleting/revoking that artifact therefore makes a later run pause again.
        if state.is_complete(step.id) and not force and step.kind != StepKind.APPROVAL:
            return StepExecution(
                step_id=step.id,
                kind=step.kind,
                status="skipped",
                message="step already completed; use --force to run it again",
            )

        if step.kind == StepKind.DETERMINISTIC:
            if step.action not in AGENTS:
                raise ValueError(f"Unknown deterministic action: {step.action}")
            result = AGENTS[step.action].run(context)
            state.mark_complete(step.id)
            save_workflow_state(context, state)
            return StepExecution(step.id, step.kind, "completed", result)

        if step.kind == StepKind.AGENT:
            if step.capability is None:
                raise ValueError(f"Agent step '{step.id}' has no capability")
            profile = profile_override or step.profile
            mode = mode_override or step.mode
            if dry_run:
                invocation = self.agent_runtime.build_invocation(
                    feature_id,
                    step.capability,
                    profile_name=profile,
                    mode=mode,
                )
                return StepExecution(step.id, step.kind, "dry-run", invocation)
            result = self.agent_runtime.execute(
                feature_id,
                step.capability,
                profile_name=profile,
                mode=mode,
            )
            artifact = context.artifact(step.save_as or f"ai/{step.id}.md")
            write_text(
                artifact,
                f"# AI Step — {step.id}\n\n"
                f"- Capability: {step.capability.value}\n"
                f"- Profile: {result.profile}\n"
                f"- Provider: {result.provider}\n"
                f"- Mode: {mode.value}\n\n"
                f"## Output\n\n{result.output}\n",
            )
            state.mark_complete(step.id)
            save_workflow_state(context, state)
            return StepExecution(step.id, step.kind, "completed", result, str(artifact.relative_to(self.project_root)))

        if step.kind == StepKind.APPROVAL:
            gate = step.gate or step.id
            if not is_approved(context, gate):
                state.last_status = "paused"
                state.paused_at = step.id
                # If an approval used to be complete but the artifact was revoked,
                # remove its completion marker before persisting the paused state.
                state.completed_steps = [value for value in state.completed_steps if value != step.id]
                save_workflow_state(context, state)
                return StepExecution(
                    step.id,
                    step.kind,
                    "paused",
                    message=f"approval '{gate}' is required",
                )
            state.mark_complete(step.id)
            save_workflow_state(context, state)
            return StepExecution(step.id, step.kind, "completed", message=f"approval '{gate}' satisfied")

        findings = validate(context, definition.validation_mode)
        if has_blockers(findings):
            state.last_status = "failed"
            state.paused_at = step.id
            save_workflow_state(context, state)
            return StepExecution(step.id, step.kind, "failed", findings, "validation blockers found")
        state.mark_complete(step.id)
        save_workflow_state(context, state)
        return StepExecution(step.id, step.kind, "completed", findings)

    def run_manual_step(
        self,
        feature_id: str,
        workflow: str,
        step_id: str,
        *,
        force: bool = False,
        dry_run: bool = False,
        profile_override: str | None = None,
        mode_override: ExecutionMode | None = None,
    ) -> StepExecution:
        """Run one named workflow step independently of predecessor state.

        Read-only/advisory and deterministic steps can be run out of order directly.
        A write-capable external-agent step can also be run at any time, but if an
        earlier approval gate is unsatisfied the caller must use ``force`` to make
        that governance bypass explicit.
        """
        definition = load_workflow(self.project_root, workflow)
        step = definition.step(step_id)
        context = self.context(feature_id)
        state = load_workflow_state(context, definition.name)
        effective_mode = mode_override or step.mode

        if (
            step.kind == StepKind.AGENT
            and effective_mode == ExecutionMode.WORKSPACE_WRITE
            and not dry_run
            and not force
            and not state.is_complete(step.id)
        ):
            pending = [
                gate
                for gate in self._prior_approval_gates(definition, step.id)
                if not is_approved(context, gate)
            ]
            if pending:
                gates = ", ".join(pending)
                raise RuntimeError(
                    f"Manual workspace-write step '{step.id}' has unsatisfied prior approval(s): {gates}. "
                    "Grant the approval or use --force to explicitly bypass the gate."
                )

        return self._execute_workflow_step(
            feature_id,
            definition,
            step,
            force=force,
            dry_run=dry_run,
            profile_override=profile_override,
            mode_override=mode_override,
        )

    def run_workflow(self, feature_id: str, workflow: str | LifecycleMode) -> list[StepExecution]:
        workflow_name = workflow.value if isinstance(workflow, LifecycleMode) else str(workflow)
        definition = load_workflow(self.project_root, workflow_name)
        context = self.context(feature_id)
        executions: list[StepExecution] = []

        for step in definition.steps:
            execution = self._execute_workflow_step(feature_id, definition, step)
            executions.append(execution)
            if execution.status in {"paused", "failed"}:
                break

        state = load_workflow_state(context, definition.name)
        if all(state.is_complete(step.id) for step in definition.steps):
            state.last_status = "completed"
            state.paused_at = None
            save_workflow_state(context, state)
        return executions

    def workflow_definition(self, name: str) -> WorkflowDefinition:
        return load_workflow(self.project_root, name)
