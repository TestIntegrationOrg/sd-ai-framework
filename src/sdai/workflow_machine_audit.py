from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
from typing import Mapping

from sdai.audit_ledger import AuditLedger
from sdai.audit_provenance import AuditAction, AuditActor, AuditBinding, AuditEvent, AuditExecution
from sdai.execution_ledger import ExecutionLedger, ExecutionState, load_execution_run
from sdai.workflow_execution import WorkflowExecutionStatus, WorkflowLeafExecutor
from sdai.workflow_machine import WorkflowResumeResult, resume_workflow_run


def _hash_json(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + sha256(encoded).hexdigest()


def _execution_file_binding(ledger: ExecutionLedger, path: Path) -> AuditBinding | None:
    if not path.exists():
        return None
    binding = ledger.binding_for_file(path, kind="evidence")
    return AuditBinding("evidence", binding.source, binding.sha256)


def _ledger_bindings(ledger: ExecutionLedger, state: ExecutionState, *, phase: str) -> tuple[AuditBinding, ...]:
    bindings: list[AuditBinding] = [
        AuditBinding("evidence", f"workflow-engine2/ledger-head/{phase}", state.last_sha256),
        AuditBinding("context", f"workflow-engine2/ledger-state/{phase}", _hash_json(state.as_dict())),
    ]
    for path in (ledger.manifest_path, ledger.events_path, ledger.checkpoint_path):
        binding = _execution_file_binding(ledger, path)
        if binding is not None:
            bindings.append(binding)
    return tuple(sorted(bindings, key=lambda item: (item.kind, item.source, item.sha256)))


def _actor(workflow: str) -> AuditActor:
    return AuditActor(
        "workflow",
        f"workflow-engine2:{workflow}",
        semantic_role="workflow-engine2",
    )


def _start_event(
    audit: AuditLedger,
    ledger: ExecutionLedger,
    state: ExecutionState,
) -> AuditEvent:
    return audit.append(
        category="workflow",
        actor=_actor(ledger.manifest.workflow),
        action=AuditAction("workflow.engine2.resume.started", f"run:{ledger.manifest.run_id}"),
        execution=AuditExecution(
            run_id=ledger.manifest.run_id,
            workflow=ledger.manifest.workflow,
            git_commit=ledger.manifest.baseline_commit,
        ),
        bindings=_ledger_bindings(ledger, state, phase="before"),
        metadata={
            "status": "started",
            "engine": "workflow-engine2",
            "ledgerStatus": state.status,
            "lastSequence": state.last_sequence,
        },
    )


def _terminal_action(status: str) -> str:
    return {
        WorkflowExecutionStatus.SUCCEEDED.value: "workflow.engine2.resume.completed",
        WorkflowExecutionStatus.PAUSED.value: "workflow.engine2.resume.paused",
        WorkflowExecutionStatus.BLOCKED.value: "workflow.engine2.resume.blocked",
        WorkflowExecutionStatus.CANCELLED.value: "workflow.engine2.resume.cancelled",
        WorkflowExecutionStatus.FAILED.value: "workflow.engine2.resume.failed",
    }.get(status, "workflow.engine2.resume.failed")


def _terminal_event(
    audit: AuditLedger,
    ledger: ExecutionLedger,
    state: ExecutionState,
    started: AuditEvent,
    *,
    result: WorkflowResumeResult | None = None,
    failure: BaseException | None = None,
) -> AuditEvent:
    status = result.execution.status.value if result is not None else "failed"
    bindings: list[AuditBinding] = [
        AuditBinding("evidence", "workflow-engine2/resume-start", started.sha256),
        *_ledger_bindings(ledger, state, phase="after"),
    ]
    metadata: dict[str, object] = {
        "status": status,
        "engine": "workflow-engine2",
        "ledgerStatus": state.status,
        "lastSequence": state.last_sequence,
    }
    if result is not None:
        execution = result.execution.as_dict()
        run_status = result.run_status.as_dict()
        resume = result.as_dict()
        graph_sha = execution.get("graphSha256")
        input_sha = execution.get("inputSha256")
        execution_sha = execution.get("executionSha256")
        status_sha = run_status.get("statusSha256")
        resume_sha = resume.get("resumeSha256")
        for label, kind, digest in (
            ("workflow-engine2/graph", "workflow", graph_sha),
            ("workflow-engine2/input", "input", input_sha),
            ("workflow-engine2/execution", "output", execution_sha),
            ("workflow-engine2/run-status", "evidence", status_sha),
            ("workflow-engine2/resume-result", "evidence", resume_sha),
        ):
            if isinstance(digest, str):
                bindings.append(AuditBinding(kind, label, digest))
        metadata.update(
            {
                "graphSha256": graph_sha,
                "inputSha256": input_sha,
                "executionSha256": execution_sha,
                "statusSha256": status_sha,
                "resumeSha256": resume_sha,
            }
        )
    if failure is not None:
        metadata["failureType"] = type(failure).__name__[:128] or "Exception"
    return audit.append(
        category="workflow",
        actor=_actor(ledger.manifest.workflow),
        action=AuditAction(_terminal_action(status), f"run:{ledger.manifest.run_id}"),
        execution=AuditExecution(
            run_id=ledger.manifest.run_id,
            workflow=ledger.manifest.workflow,
            git_commit=ledger.manifest.baseline_commit,
        ),
        bindings=tuple(sorted(bindings, key=lambda item: (item.kind, item.source, item.sha256))),
        metadata=metadata,
    )


def audited_resume_workflow_run(
    project_root: Path,
    feature_id: str,
    run_id: str,
    *,
    input_values: Mapping[str, object] | None = None,
    leaf_executor: WorkflowLeafExecutor | None = None,
) -> WorkflowResumeResult:
    """Resume Workflow Engine 2 with hash-only audit provenance.

    The execution ledger and Workflow Engine 2 remain authoritative. Audit start is
    durable before execution; the underlying resume is invoked exactly once; terminal
    audit failure never causes a retry of workflow execution.
    """

    root = project_root.resolve()
    execution_ledger = load_execution_run(root, feature_id, run_id)
    before = execution_ledger.reconstruct()
    audit = AuditLedger(root, feature_id)
    started = _start_event(audit, execution_ledger, before)

    try:
        result = resume_workflow_run(
            root,
            feature_id,
            run_id,
            input_values=input_values,
            leaf_executor=leaf_executor,
        )
    except BaseException as exc:
        after = execution_ledger.reconstruct()
        _terminal_event(
            audit,
            execution_ledger,
            after,
            started,
            failure=exc,
        )
        raise

    after = execution_ledger.reconstruct()
    _terminal_event(
        audit,
        execution_ledger,
        after,
        started,
        result=result,
    )
    return result


__all__ = ["audited_resume_workflow_run"]
