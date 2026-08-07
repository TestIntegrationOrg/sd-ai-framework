from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
import time
from typing import Callable, TypeVar

from sdai.agent_platform import AgentRuntime, ExecutionMode
from sdai.agent_platform.models import AgentInvocation
from sdai.agents import ArchitectAgent, DeveloperAgent, PlannerAgent, RequirementAgent, SecurityAgent
from sdai.agents.base import AgentResult
from sdai.artifacts import write_text
from sdai.conditions import evaluate_condition
from sdai.governance import check_workflow_governance, governance_enforced, load_governance
from sdai.models import FeatureContext, LifecycleMode
from sdai.policy import load_effective_configuration
from sdai.quality_gates import QualityGateResult, QualityGateRunner
from sdai.validation import ValidationFinding, has_blockers, validate
from sdai.workflows import (
    FailureMode,
    RetryPolicy,
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
    attempts: int = 1


T = TypeVar("T")


class Orchestrator:
    def __init__(
        self,
        project_root: Path,
        *,
        agent_runtime: AgentRuntime | None = None,
        quality_gate_runner: QualityGateRunner | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        self.project_root = project_root.resolve()
        self.agent_runtime = agent_runtime or AgentRuntime(self.project_root)
        self.quality_gate_runner = quality_gate_runner or QualityGateRunner(self.project_root)
        self.policy = load_effective_configuration(self.project_root)
        self.sleeper = sleeper

    def context(self, feature_id: str) -> FeatureContext:
        return FeatureContext(self.project_root, feature_id)

    def run_step(self, feature_id: str, step: str) -> AgentResult | list[ValidationFinding]:
        context = self.context(feature_id)
        if step == "validate":
            return validate(context, LifecycleMode.STANDARD)
        if step not in AGENTS:
            raise ValueError(f"Unknown workflow step: {step}")
        return AGENTS[step].run(context)

    def _semantic_enabled(self) -> bool:
        return (self.project_root / ".sdai" / "agents").exists()

    def _build_agent_invocation(
        self,
        feature_id: str,
        capability,
        *,
        profile_name: str | None,
        agent_name: str | None,
        mode: ExecutionMode,
    ):
        kwargs = {"profile_name": profile_name, "mode": mode}
        if agent_name and self._semantic_enabled():
            kwargs["agent_name"] = agent_name
        return self.agent_runtime.build_invocation(feature_id, capability, **kwargs)

    def _execute_agent(
        self,
        feature_id: str,
        capability,
        *,
        profile_name: str | None,
        agent_name: str | None,
        mode: ExecutionMode,
    ):
        kwargs = {"profile_name": profile_name, "mode": mode}
        if agent_name and self._semantic_enabled():
            kwargs["agent_name"] = agent_name
        return self.agent_runtime.execute(feature_id, capability, **kwargs)

    @staticmethod
    def _invalidate_from(state, definition: WorkflowDefinition, step_id: str) -> None:
        top_level_id = definition.top_level_id(step_id)
        top_steps = list(definition.steps)
        ids = [item.id for item in top_steps]
        index = ids.index(top_level_id)
        invalid: set[str] = set()
        for item in top_steps[index:]:
            invalid.add(item.id)
            invalid.update(child.id for child in item.children)
        state.completed_steps = [value for value in state.completed_steps if value not in invalid]
        state.last_status = "running"
        state.paused_at = None

    @staticmethod
    def _prior_approval_gates(definition: WorkflowDefinition, step_id: str) -> list[str]:
        gates: list[str] = []
        top_level_id = definition.top_level_id(step_id)
        for item in definition.steps:
            if item.id == top_level_id:
                break
            if item.kind == StepKind.APPROVAL:
                gates.append(item.gate or item.id)
        return gates

    def _enforce_workspace_write_policy(
        self,
        context: FeatureContext,
        definition: WorkflowDefinition,
        step_id: str,
        *,
        force: bool,
    ) -> None:
        if not self.policy.require_prior_approval_for_workspace_write:
            return
        gates = self._prior_approval_gates(definition, step_id)
        if not gates:
            raise RuntimeError(
                f"Workspace-write step '{step_id}' is blocked: effective policy requires a prior "
                "approval gate, but the workflow defines none before this step."
            )
        pending = [gate for gate in gates if not is_approved(context, gate)]
        if not pending:
            return
        if force and self.policy.allow_force_approval_bypass:
            return
        suffix = " Organization policy does not allow --force bypass." if force else ""
        raise RuntimeError(
            f"Workspace-write step '{step_id}' has unsatisfied prior approval(s): "
            f"{', '.join(pending)}.{suffix}"
        )

    def _retry_call(self, policy: RetryPolicy, operation: Callable[[], T]) -> tuple[T, int]:
        delay = policy.delay_seconds
        last_error: Exception | None = None
        for attempt in range(1, policy.max_attempts + 1):
            try:
                return operation(), attempt
            except Exception as exc:
                last_error = exc
                if attempt >= policy.max_attempts:
                    break
                if delay > 0:
                    self.sleeper(delay)
                delay *= policy.backoff_multiplier
        assert last_error is not None
        raise last_error

    def _persist_agent_output(
        self,
        context: FeatureContext,
        step: WorkflowStep,
        result,
        mode: ExecutionMode,
    ) -> Path:
        artifact = context.artifact(step.save_as or f"ai/{step.id}.md")
        semantic = getattr(result, "agent_name", None) or step.agent_name or "-"
        write_text(
            artifact,
            f"# AI Step — {step.id}\n\n"
            f"- Capability: {step.capability.value if step.capability else '-'}\n"
            f"- Semantic agent: {semantic}\n"
            f"- Profile: {result.profile}\n"
            f"- Provider: {result.provider}\n"
            f"- Mode: {mode.value}\n\n"
            f"## Output\n\n{result.output}\n",
        )
        return artifact

    def _dry_run_parallel_child(
        self,
        feature_id: str,
        definition: WorkflowDefinition,
        child: WorkflowStep,
    ) -> StepExecution:
        context = self.context(feature_id)
        condition = evaluate_condition(child.condition, context=context, workflow=definition.name)
        if not condition.matched:
            return StepExecution(child.id, child.kind, "condition-skipped", message=condition.detail, attempts=0)
        if child.capability is None:
            return StepExecution(child.id, child.kind, "failed", message="parallel agent has no capability")
        invocation = self._build_agent_invocation(
            feature_id,
            child.capability,
            profile_name=child.profile,
            agent_name=child.agent_name,
            mode=child.mode,
        )
        return StepExecution(child.id, child.kind, "dry-run", invocation, attempts=0)

    def _run_parallel_child(
        self,
        feature_id: str,
        definition: WorkflowDefinition,
        child: WorkflowStep,
    ) -> StepExecution:
        context = self.context(feature_id)
        condition = evaluate_condition(child.condition, context=context, workflow=definition.name)
        if not condition.matched:
            return StepExecution(child.id, child.kind, "condition-skipped", message=condition.detail, attempts=0)
        if child.capability is None:
            return StepExecution(child.id, child.kind, "failed", message="parallel agent has no capability")
        try:
            result, attempts = self._retry_call(
                child.retry,
                lambda: self._execute_agent(
                    feature_id,
                    child.capability,
                    profile_name=child.profile,
                    agent_name=child.agent_name,
                    mode=child.mode,
                ),
            )
            artifact = self._persist_agent_output(context, child, result, child.mode)
            return StepExecution(
                child.id,
                child.kind,
                "completed",
                result,
                str(artifact.relative_to(self.project_root)),
                attempts,
            )
        except Exception as exc:
            return StepExecution(child.id, child.kind, "failed", message=str(exc), attempts=child.retry.max_attempts)

    def _execute_workflow_step(
        self,
        feature_id: str,
        definition: WorkflowDefinition,
        step: WorkflowStep,
        *,
        force: bool = False,
        dry_run: bool = False,
        profile_override: str | None = None,
        agent_override: str | None = None,
        mode_override: ExecutionMode | None = None,
    ) -> StepExecution:
        context = self.context(feature_id)
        state = load_workflow_state(context, definition.name)

        if force and state.is_complete(step.id):
            self._invalidate_from(state, definition, step.id)
            save_workflow_state(context, state)

        if not force:
            condition = evaluate_condition(step.condition, context=context, workflow=definition.name)
            if not condition.matched:
                state.mark_complete(step.id)
                save_workflow_state(context, state)
                return StepExecution(step.id, step.kind, "condition-skipped", message=condition.detail, attempts=0)

        if state.is_complete(step.id) and not force and step.kind != StepKind.APPROVAL:
            return StepExecution(
                step_id=step.id,
                kind=step.kind,
                status="skipped",
                message="step already completed; use --force to run it again",
                attempts=0,
            )

        if step.kind == StepKind.DETERMINISTIC:
            if step.action not in AGENTS:
                raise ValueError(f"Unknown deterministic action: {step.action}")
            try:
                result, attempts = self._retry_call(step.retry, lambda: AGENTS[step.action].run(context))
            except Exception as exc:
                state.last_status = "failed"
                state.paused_at = step.id
                save_workflow_state(context, state)
                return StepExecution(step.id, step.kind, "failed", message=str(exc), attempts=step.retry.max_attempts)
            state.mark_complete(step.id)
            save_workflow_state(context, state)
            return StepExecution(step.id, step.kind, "completed", result, attempts=attempts)

        if step.kind == StepKind.AGENT:
            if step.capability is None:
                raise ValueError(f"Agent step '{step.id}' has no capability")
            profile = profile_override or step.profile
            semantic_agent = agent_override or step.agent_name
            mode = mode_override or step.mode
            if not dry_run and mode == ExecutionMode.WORKSPACE_WRITE:
                self._enforce_workspace_write_policy(
                    context, definition, step.id, force=force
                )
            if dry_run:
                invocation = self._build_agent_invocation(
                    feature_id,
                    step.capability,
                    profile_name=profile,
                    agent_name=semantic_agent,
                    mode=mode,
                )
                return StepExecution(step.id, step.kind, "dry-run", invocation, attempts=0)
            try:
                result, attempts = self._retry_call(
                    step.retry,
                    lambda: self._execute_agent(
                        feature_id,
                        step.capability,
                        profile_name=profile,
                        agent_name=semantic_agent,
                        mode=mode,
                    ),
                )
            except Exception as exc:
                state.last_status = "failed"
                state.paused_at = step.id
                save_workflow_state(context, state)
                return StepExecution(step.id, step.kind, "failed", message=str(exc), attempts=step.retry.max_attempts)
            artifact = self._persist_agent_output(context, step, result, mode)
            state.mark_complete(step.id)
            save_workflow_state(context, state)
            return StepExecution(
                step.id,
                step.kind,
                "completed",
                result,
                str(artifact.relative_to(self.project_root)),
                attempts,
            )

        if step.kind == StepKind.APPROVAL:
            gate = step.gate or step.id
            if not is_approved(context, gate):
                state.last_status = "paused"
                state.paused_at = step.id
                state.completed_steps = [value for value in state.completed_steps if value != step.id]
                save_workflow_state(context, state)
                return StepExecution(step.id, step.kind, "paused", message=f"approval '{gate}' is required", attempts=0)
            state.mark_complete(step.id)
            save_workflow_state(context, state)
            return StepExecution(step.id, step.kind, "completed", message=f"approval '{gate}' satisfied")

        if step.kind == StepKind.QUALITY_GATE:
            gate_name = step.quality_gate or step.id
            delay = step.retry.delay_seconds
            for attempt in range(1, step.retry.max_attempts + 1):
                try:
                    result = self.quality_gate_runner.run(gate_name, context=context)
                except Exception as exc:
                    if attempt >= step.retry.max_attempts:
                        state.last_status = "failed"
                        state.paused_at = step.id
                        save_workflow_state(context, state)
                        return StepExecution(step.id, step.kind, "failed", message=str(exc), attempts=attempt)
                else:
                    if result.passed:
                        state.mark_complete(step.id)
                        save_workflow_state(context, state)
                        return StepExecution(step.id, step.kind, "completed", result, attempts=attempt)
                    if attempt >= step.retry.max_attempts:
                        state.last_status = "failed"
                        state.paused_at = step.id
                        save_workflow_state(context, state)
                        return StepExecution(
                            step.id,
                            step.kind,
                            "failed",
                            result,
                            f"quality gate '{gate_name}' failed with exit code {result.return_code}",
                            attempt,
                        )
                if delay > 0:
                    self.sleeper(delay)
                delay *= step.retry.backoff_multiplier
            raise AssertionError("quality gate retry loop exhausted unexpectedly")

        if step.kind == StepKind.PARALLEL:
            if dry_run:
                executions = [self._dry_run_parallel_child(feature_id, definition, child) for child in step.children]
                return StepExecution(step.id, step.kind, "dry-run", executions, attempts=0)

            governance = load_governance(self.project_root)
            max_policy = int((governance.get("workflow") or {}).get("max_parallelism", 4))
            workers = max(1, min(len(step.children), max_policy))
            executions_by_id: dict[str, StepExecution] = {}
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="sdai-agent") as pool:
                future_map = {
                    pool.submit(self._run_parallel_child, feature_id, definition, child): child
                    for child in step.children
                }
                for future in as_completed(future_map):
                    child = future_map[future]
                    try:
                        executions_by_id[child.id] = future.result()
                    except Exception as exc:
                        executions_by_id[child.id] = StepExecution(child.id, child.kind, "failed", message=str(exc))
            executions = [executions_by_id[child.id] for child in step.children]
            for child_execution in executions:
                if child_execution.status in {"completed", "condition-skipped"}:
                    state.mark_complete(child_execution.step_id)

            fatal = any(
                execution.status == "failed" and child.on_failure == FailureMode.STOP
                for child, execution in zip(step.children, executions)
            )
            if fatal:
                state.last_status = "failed"
                state.paused_at = step.id
                save_workflow_state(context, state)
                return StepExecution(step.id, step.kind, "failed", executions, "parallel child failed")
            state.mark_complete(step.id)
            save_workflow_state(context, state)
            warning_count = sum(execution.status == "failed" for execution in executions)
            message = f"{warning_count} non-blocking child failure(s)" if warning_count else ""
            return StepExecution(step.id, step.kind, "completed", executions, message)

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
        agent_override: str | None = None,
        mode_override: ExecutionMode | None = None,
    ) -> StepExecution:
        definition = load_workflow(self.project_root, workflow)
        step = definition.step(step_id)
        context = self.context(feature_id)
        state = load_workflow_state(context, definition.name)
        effective_mode = mode_override or step.mode

        if (
            step.kind == StepKind.AGENT
            and effective_mode == ExecutionMode.WORKSPACE_WRITE
            and not dry_run
            and not state.is_complete(step.id)
        ):
            pending = [
                gate
                for gate in self._prior_approval_gates(definition, step.id)
                if not is_approved(context, gate)
            ]
            if pending and not force:
                gates = ", ".join(pending)
                raise RuntimeError(
                    f"Manual workspace-write step '{step.id}' has unsatisfied prior approval(s): {gates}. "
                    "Grant the approval or use --force to explicitly bypass the gate when policy permits it."
                )
            self._enforce_workspace_write_policy(
                context, definition, step.id, force=force
            )

        return self._execute_workflow_step(
            feature_id,
            definition,
            step,
            force=force,
            dry_run=dry_run,
            profile_override=profile_override,
            agent_override=agent_override,
            mode_override=mode_override,
        )

    def run_workflow(self, feature_id: str, workflow: str | LifecycleMode) -> list[StepExecution]:
        workflow_name = workflow.value if isinstance(workflow, LifecycleMode) else str(workflow)
        definition = load_workflow(self.project_root, workflow_name)
        if governance_enforced(self.project_root):
            findings = check_workflow_governance(self.project_root, definition)
            blockers = [finding for finding in findings if finding.level == "ERROR"]
            if blockers:
                detail = "; ".join(f"{item.code}: {item.message}" for item in blockers)
                raise RuntimeError(f"Workflow governance rejected '{definition.name}': {detail}")

        context = self.context(feature_id)
        executions: list[StepExecution] = []
        for step in definition.steps:
            execution = self._execute_workflow_step(feature_id, definition, step)
            executions.append(execution)
            if execution.status == "paused":
                break
            if execution.status == "failed" and step.on_failure == FailureMode.STOP:
                break

        state = load_workflow_state(context, definition.name)
        if all(state.is_complete(step.id) for step in definition.steps):
            state.last_status = "completed"
            state.paused_at = None
            save_workflow_state(context, state)
        return executions

    def workflow_definition(self, name: str) -> WorkflowDefinition:
        return load_workflow(self.project_root, name)
