from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Mapping

from sdai.execution_ledger import ExecutionLedgerError, LedgerEvent, load_execution_run
from sdai.workflow_execution import (
    WorkflowExecutionResult,
    WorkflowExecutionStatus,
    WorkflowLeafExecutor,
    execute_workflow_graph,
)
from sdai.workflow_graph import load_workflow_graph


WORKFLOW_RUN_STATUS_API_VERSION = "sdai.workflow-run-status/v2"
WORKFLOW_RESUME_API_VERSION = "sdai.workflow-resume-result/v2"
_ITEM_SCOPE = re.compile(r":item-(\d{4})-([0-9a-f]{12})(?=::|:|$)")
_ITERATION_SCOPE = re.compile(r":iteration-(\d{4})(?=::|:|$)")
_BRANCH_SCOPE = re.compile(r":(then|else|\$default|\$case/\d+)(?=::|:|$)")
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _hash_json(value: object) -> str:
    return "sha256:" + sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _registration_map(events: tuple[LedgerEvent, ...]) -> dict[str, LedgerEvent]:
    return {
        event.task_id: event
        for event in events
        if event.kind == "task.registered" and event.task_id is not None
    }


def _execution_scope(identity: object) -> dict[str, object]:
    text = identity if isinstance(identity, str) else ""
    return {
        "branches": [match.group(1) for match in _BRANCH_SCOPE.finditer(text)],
        "items": [
            {"index": int(match.group(1)), "itemSha256Prefix": match.group(2)}
            for match in _ITEM_SCOPE.finditer(text)
        ],
        "iterations": [int(match.group(1)) for match in _ITERATION_SCOPE.finditer(text)],
    }


def _public_node(raw: object) -> dict[str, object]:
    if not isinstance(raw, Mapping):
        raise ExecutionLedgerError("SDAI-WF2-CLI-004: workflow checkpoint node is invalid")
    required = {"identity", "path", "kind", "status", "outputSha256"}
    if not required.issubset(raw):
        raise ExecutionLedgerError("SDAI-WF2-CLI-004: workflow checkpoint node is incomplete")
    if not all(isinstance(raw[key], str) for key in ("identity", "path", "kind", "status")):
        raise ExecutionLedgerError("SDAI-WF2-CLI-004: workflow checkpoint node identity is invalid")
    if not isinstance(raw["outputSha256"], str) or not _SHA256.fullmatch(raw["outputSha256"]):
        raise ExecutionLedgerError("SDAI-WF2-CLI-004: workflow checkpoint output hash is invalid")
    task_id = raw.get("taskId")
    evidence = raw.get("evidenceReferences") or []
    if task_id is not None and not isinstance(task_id, str):
        raise ExecutionLedgerError("SDAI-WF2-CLI-004: workflow checkpoint task identity is invalid")
    if not isinstance(evidence, list) or not all(isinstance(item, str) for item in evidence):
        raise ExecutionLedgerError("SDAI-WF2-CLI-004: workflow checkpoint evidence is invalid")
    return {
        "identity": raw["identity"],
        "path": raw["path"],
        "kind": raw["kind"],
        "status": raw["status"],
        "outputSha256": raw["outputSha256"],
        "taskId": task_id,
        "evidenceReferences": list(evidence),
        "scope": _execution_scope(raw["identity"]),
    }


@dataclass(frozen=True)
class WorkflowRunStatus:
    body: dict[str, object]

    @property
    def status(self) -> str:
        return str(self.body["status"])

    @property
    def exit_code(self) -> int:
        if self.body.get("checkpointStatus") == "stale":
            return 4
        if self.status == WorkflowExecutionStatus.SUCCEEDED.value:
            return 0
        if self.status in {
            "active",
            WorkflowExecutionStatus.PAUSED.value,
            WorkflowExecutionStatus.BLOCKED.value,
        }:
            return 2
        return 5

    def as_dict(self) -> dict[str, object]:
        payload = dict(self.body)
        payload["statusSha256"] = _hash_json(payload)
        return payload

    def to_json(self) -> str:
        return _canonical_json(self.as_dict()) + "\n"


@dataclass(frozen=True)
class WorkflowResumeResult:
    execution: WorkflowExecutionResult
    run_status: WorkflowRunStatus

    @property
    def exit_code(self) -> int:
        if self.execution.status == WorkflowExecutionStatus.SUCCEEDED:
            return 0
        if self.execution.status in {
            WorkflowExecutionStatus.PAUSED,
            WorkflowExecutionStatus.BLOCKED,
        }:
            return 2
        return 5

    def as_dict(self) -> dict[str, object]:
        execution = self.execution.as_dict()
        execution["nodes"] = [_public_node(item) for item in execution["nodes"]]  # type: ignore[arg-type]
        execution.pop("executionSha256", None)
        body: dict[str, object] = {
            "apiVersion": WORKFLOW_RESUME_API_VERSION,
            "status": self.execution.status.value,
            "execution": execution,
            "run": self.run_status.as_dict(),
        }
        body["resumeSha256"] = _hash_json(body)
        return body

    def to_json(self) -> str:
        return _canonical_json(self.as_dict()) + "\n"


def inspect_workflow_run(
    project_root: Path,
    feature_id: str,
    run_id: str,
) -> WorkflowRunStatus:
    """Return deterministic, output-redacted Workflow Engine 2 ledger status."""

    ledger = load_execution_run(project_root, feature_id, run_id)
    state = ledger.reconstruct()
    events = ledger.load_events()
    registrations = _registration_map(events)
    checkpoint_status = "missing"
    engine: Mapping[str, object] | None = None
    if ledger.checkpoint_path.exists():
        try:
            checkpoint = ledger.load_checkpoint()
        except ExecutionLedgerError as exc:
            if "checkpoint is stale relative to the current event ledger" not in str(exc):
                raise
            checkpoint_status = "stale"
        else:
            checkpoint_status = "current"
            extra = None if checkpoint is None else checkpoint.get("extra")
            candidate = extra.get("workflowEngine2") if isinstance(extra, Mapping) else None
            if candidate is not None and not isinstance(candidate, Mapping):
                raise ExecutionLedgerError(
                    "SDAI-WF2-CLI-004: workflow checkpoint extension is invalid"
                )
            engine = candidate

    task_items: list[dict[str, object]] = []
    next_work: dict[str, object] | None = None
    task_map = state.task_map()
    ordered_ids = [
        event.task_id
        for event in events
        if event.kind == "task.registered" and event.task_id is not None
    ]
    ordered_ids.extend(sorted(set(task_map) - set(ordered_ids)))
    for task_id in ordered_ids:
        task = task_map[task_id]
        registration = registrations.get(task.task_id)
        payload = registration.payload if registration is not None else {}
        item = {
            "taskId": task.task_id,
            "status": task.status,
            "executionIdentity": payload.get("executionIdentity"),
            "nodePath": payload.get("nodePath"),
            "nodeKind": payload.get("nodeKind"),
            "contextSha256": payload.get("contextSha256"),
            "bindings": [binding.as_dict() for binding in task.bindings],
            "scope": _execution_scope(payload.get("executionIdentity")),
        }
        task_items.append(item)
        if next_work is None and task.status != "completed":
            action = "re-evaluate" if state.status == "paused" else "resume"
            if task.status == "registered":
                action = "execute"
            elif task.status == "failed":
                action = "terminal"
            next_work = {
                "action": action,
                "taskId": task.task_id,
                "executionIdentity": payload.get("executionIdentity"),
                "nodePath": payload.get("nodePath"),
                "scope": _execution_scope(payload.get("executionIdentity")),
            }
    if next_work is None and state.status == "active":
        next_work = {
            "action": "plan",
            "taskId": None,
            "executionIdentity": "$root",
            "nodePath": "$root",
            "scope": _execution_scope("$root"),
        }

    nodes: list[dict[str, object]] = []
    graph_sha: object = None
    input_sha: object = None
    plan_sha: object = None
    checkpoint_run_status: object = None
    if engine is not None:
        raw_nodes = engine.get("nodes")
        if not isinstance(raw_nodes, Mapping):
            raise ExecutionLedgerError("SDAI-WF2-CLI-004: workflow checkpoint nodes are invalid")
        nodes = [_public_node(raw_nodes[key]) for key in sorted(raw_nodes)]
        graph_sha = engine.get("graphSha256")
        input_sha = engine.get("inputSha256")
        plan_sha = engine.get("planSha256")
        checkpoint_run_status = engine.get("status")

    normalized_status = {
        "completed": WorkflowExecutionStatus.SUCCEEDED.value,
        "cancelled": WorkflowExecutionStatus.CANCELLED.value,
    }.get(state.status, state.status)
    pause = next(
        (event for event in reversed(events) if event.kind == "run.paused"),
        None,
    )
    if state.status == "paused" and pause is not None:
        reason = pause.payload.get("reason")
        if reason in {
            WorkflowExecutionStatus.PAUSED.value,
            WorkflowExecutionStatus.BLOCKED.value,
        }:
            normalized_status = str(reason)

    body: dict[str, object] = {
        "apiVersion": WORKFLOW_RUN_STATUS_API_VERSION,
        "runId": ledger.manifest.run_id,
        "featureId": ledger.manifest.feature_id,
        "workflow": ledger.manifest.workflow,
        "status": normalized_status,
        "ledgerStatus": state.status,
        "lastSequence": state.last_sequence,
        "lastSha256": state.last_sha256,
        "checkpointStatus": checkpoint_status,
        "checkpointRunStatus": checkpoint_run_status,
        "graphSha256": graph_sha,
        "inputSha256": input_sha,
        "planSha256": plan_sha,
        "nodes": nodes,
        "tasks": task_items,
        "nextWork": next_work,
    }
    return WorkflowRunStatus(body)


def resume_workflow_run(
    project_root: Path,
    feature_id: str,
    run_id: str,
    *,
    input_values: Mapping[str, object] | None = None,
    leaf_executor: WorkflowLeafExecutor | None = None,
) -> WorkflowResumeResult:
    """Resolve the ledger workflow and resume it through Workflow Engine 2."""

    ledger = load_execution_run(project_root, feature_id, run_id)
    resolution = load_workflow_graph(
        project_root,
        ledger.manifest.workflow,
        input_values=input_values,
    )
    execution = execute_workflow_graph(
        resolution,
        ledger,
        leaf_executor=leaf_executor,
    )
    return WorkflowResumeResult(
        execution,
        inspect_workflow_run(project_root, feature_id, run_id),
    )
