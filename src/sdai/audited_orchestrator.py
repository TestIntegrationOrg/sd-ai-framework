from __future__ import annotations

from sdai.agent_platform.models import ExecutionMode
from sdai.orchestrator import Orchestrator, StepExecution
from sdai.workflows import (
    FailureMode,
    StepKind,
    WorkflowDefinition,
    WorkflowStep,
    is_approved,
    load_workflow,
    load_workflow_state,
)
from sdai.workflow_audit import WorkflowAuditRecorder


class AuditedOrchestrator(Orchestrator):
    """Orchestrator facade that records audit/provenance without changing execution authority."""

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
        recorder = WorkflowAuditRecorder.optional_for(
            self.project_root,
            feature_id,
            self.policy,
        )
        provenance = recorder.prepare(definition) if recorder is not None else None
        state_before = load_workflow_state(context, definition.name)
        started = (
            recorder.step_started(
                definition,
                step,
                provenance,
                state_before,
                force=force,
                dry_run=dry_run,
                effective_mode=(mode_override or step.mode).value,
            )
            if recorder is not None and provenance is not None
            else None
        )
        try:
            result = super()._execute_workflow_step(
                feature_id,
                definition,
                step,
                force=force,
                dry_run=dry_run,
                profile_override=profile_override,
                agent_override=agent_override,
                mode_override=mode_override,
            )
        except BaseException as exc:
            if recorder is not None and provenance is not None and started is not None:
                state_after = load_workflow_state(context, definition.name)
                recorder.step_terminal(
                    context,
                    definition,
                    step,
                    provenance,
                    state_after,
                    StepExecution(step.id, step.kind, "failed", attempts=0),
                    started_event=started,
                    failure=exc,
                )
            raise

        if recorder is not None and provenance is not None and started is not None:
            state_after = load_workflow_state(context, definition.name)
            recorder.step_terminal(
                context,
                definition,
                step,
                provenance,
                state_after,
                result,
                started_event=started,
            )
        return result

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
        recorder = WorkflowAuditRecorder.optional_for(
            self.project_root,
            feature_id,
            self.policy,
        )
        provenance = recorder.prepare(definition) if recorder is not None else None
        started = (
            recorder.step_started(
                definition,
                step,
                provenance,
                state,
                force=force,
                dry_run=dry_run,
                effective_mode=effective_mode.value,
            )
            if recorder is not None and provenance is not None
            else None
        )

        try:
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
                    context,
                    definition,
                    step.id,
                    force=force,
                )

            result = super()._execute_workflow_step(
                feature_id,
                definition,
                step,
                force=force,
                dry_run=dry_run,
                profile_override=profile_override,
                agent_override=agent_override,
                mode_override=mode_override,
            )
        except BaseException as exc:
            if recorder is not None and provenance is not None and started is not None:
                state_after = load_workflow_state(context, definition.name)
                recorder.step_terminal(
                    context,
                    definition,
                    step,
                    provenance,
                    state_after,
                    StepExecution(step.id, step.kind, "failed", attempts=0),
                    started_event=started,
                    failure=exc,
                )
            raise

        if recorder is not None and provenance is not None and started is not None:
            state_after = load_workflow_state(context, definition.name)
            recorder.step_terminal(
                context,
                definition,
                step,
                provenance,
                state_after,
                result,
                started_event=started,
            )
        return result

    def run_workflow(self, feature_id: str, workflow: str) -> list[StepExecution]:
        workflow_name = workflow.value if hasattr(workflow, "value") else str(workflow)
        definition = load_workflow(self.project_root, workflow_name)
        context = self.context(feature_id)
        recorder = WorkflowAuditRecorder.optional_for(
            self.project_root,
            feature_id,
            self.policy,
        )
        provenance = recorder.prepare(definition) if recorder is not None else None
        state_before = load_workflow_state(context, definition.name)
        started = (
            recorder.workflow_started(definition, provenance, state_before)
            if recorder is not None and provenance is not None
            else None
        )

        try:
            executions = super().run_workflow(feature_id, workflow)
        except BaseException as exc:
            if recorder is not None and provenance is not None and started is not None:
                state_after = load_workflow_state(context, definition.name)
                recorder.workflow_terminal(
                    definition,
                    provenance,
                    state_after,
                    started_event=started,
                    status="failed",
                    failure=exc,
                )
            raise

        if any(item.status == "failed" for item in executions):
            status = "failed"
        elif any(item.status == "paused" for item in executions):
            status = "paused"
        else:
            status = "completed"
        if recorder is not None and provenance is not None and started is not None:
            state_after = load_workflow_state(context, definition.name)
            recorder.workflow_terminal(
                definition,
                provenance,
                state_after,
                started_event=started,
                status=status,
            )
        return executions


__all__ = ["AuditedOrchestrator"]
