from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
import json
from typing import Callable, Mapping

from sdai.execution_ledger import ExecutionLedger, ExecutionLedgerError, LedgerEvent
from sdai.workflow_graph import (
    WorkflowGraphError,
    WorkflowGraphNode,
    WorkflowGraphResolution,
    WorkflowNodeKind,
    evaluate_workflow_expression,
)


WORKFLOW_EXECUTION_API_VERSION = "sdai.workflow-execution-result/v2"
WORKFLOW_EXECUTION_CHECKPOINT_API_VERSION = "sdai.workflow-execution-checkpoint/v2"


class WorkflowExecutionError(RuntimeError):
    """Raised when a Workflow Engine 2 run cannot execute or resume safely."""


class WorkflowExecutionStatus(StrEnum):
    SUCCEEDED = "succeeded"
    PAUSED = "paused"
    BLOCKED = "blocked"
    FAILED = "failed"
    CANCELLED = "cancelled"


_LEAF_KINDS = frozenset(
    {
        WorkflowNodeKind.DETERMINISTIC.value,
        WorkflowNodeKind.AGENT.value,
        WorkflowNodeKind.APPROVAL.value,
        WorkflowNodeKind.VALIDATE.value,
        WorkflowNodeKind.QUALITY_GATE.value,
        WorkflowNodeKind.PLUGIN.value,
        WorkflowNodeKind.SAFE_COMMAND.value,
    }
)
_CONTROL_KINDS = frozenset(
    {
        WorkflowNodeKind.SEQUENCE.value,
        WorkflowNodeKind.IF.value,
        WorkflowNodeKind.SWITCH.value,
        WorkflowNodeKind.PARALLEL.value,
        WorkflowNodeKind.FAN_OUT.value,
        WorkflowNodeKind.FAN_IN.value,
        WorkflowNodeKind.FOREACH.value,
        WorkflowNodeKind.BOUNDED_WHILE.value,
    }
)


def _fail(code: str, message: str) -> WorkflowExecutionError:
    return WorkflowExecutionError(f"{code}: {message}")


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise _fail("SDAI-WF2-EXEC-001", "workflow execution data must be finite JSON") from exc


def _hash_json(value: object) -> str:
    return "sha256:" + sha256(_canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class WorkflowLeafOutcome:
    status: WorkflowExecutionStatus
    output: object | None = None
    evidence_references: tuple[str, ...] = ()
    error: str | None = None

    def __post_init__(self) -> None:
        status = WorkflowExecutionStatus(self.status)
        if status not in {
            WorkflowExecutionStatus.SUCCEEDED,
            WorkflowExecutionStatus.PAUSED,
            WorkflowExecutionStatus.BLOCKED,
            WorkflowExecutionStatus.FAILED,
            WorkflowExecutionStatus.CANCELLED,
        }:
            raise _fail("SDAI-WF2-EXEC-001", f"unsupported leaf outcome {status}")
        if not all(isinstance(item, str) and item for item in self.evidence_references):
            raise _fail("SDAI-WF2-EXEC-001", "leaf evidence references must be non-empty strings")
        _canonical_json(self.output)
        object.__setattr__(self, "status", status)

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "output": self.output,
            "evidenceReferences": list(self.evidence_references),
            "error": self.error,
        }


@dataclass(frozen=True)
class WorkflowLeafInvocation:
    node: WorkflowGraphNode
    execution_identity: str
    dispatch_id: str
    attempt: int
    context: dict[str, object]
    planning_binding: str | None = None


WorkflowLeafExecutor = Callable[[WorkflowLeafInvocation], WorkflowLeafOutcome]


@dataclass(frozen=True)
class WorkflowNodeExecution:
    identity: str
    path: str
    kind: str
    status: str
    output: object | None
    output_sha256: str
    task_id: str | None = None
    evidence_references: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "identity": self.identity,
            "path": self.path,
            "kind": self.kind,
            "status": self.status,
            "output": self.output,
            "outputSha256": self.output_sha256,
            "taskId": self.task_id,
            "evidenceReferences": list(self.evidence_references),
        }


@dataclass(frozen=True)
class WorkflowExecutionResult:
    run_id: str
    workflow: str
    graph_sha256: str
    input_sha256: str
    status: WorkflowExecutionStatus
    nodes: tuple[WorkflowNodeExecution, ...]
    error: str | None = None

    def as_dict(self) -> dict[str, object]:
        payload = {
            "apiVersion": WORKFLOW_EXECUTION_API_VERSION,
            "runId": self.run_id,
            "workflow": self.workflow,
            "graphSha256": self.graph_sha256,
            "inputSha256": self.input_sha256,
            "status": self.status.value,
            "nodes": [item.as_dict() for item in self.nodes],
            "error": self.error,
        }
        payload["executionSha256"] = _hash_json(payload)
        return payload

    def to_json(self) -> str:
        return _canonical_json(self.as_dict())


class _Halt(RuntimeError):
    def __init__(self, status: WorkflowExecutionStatus, error: str | None = None) -> None:
        self.status = status
        self.error = error


def default_workflow_leaf_executor(invocation: WorkflowLeafInvocation) -> WorkflowLeafOutcome:
    node = invocation.node
    if node.kind == WorkflowNodeKind.DETERMINISTIC.value:
        return WorkflowLeafOutcome(
            WorkflowExecutionStatus.SUCCEEDED,
            {"action": node.config.get("action")},
        )
    if node.kind == WorkflowNodeKind.VALIDATE.value:
        return WorkflowLeafOutcome(WorkflowExecutionStatus.SUCCEEDED, {"valid": True})
    if node.kind in {WorkflowNodeKind.APPROVAL.value, WorkflowNodeKind.QUALITY_GATE.value}:
        return WorkflowLeafOutcome(
            WorkflowExecutionStatus.PAUSED,
            error=f"{node.kind} '{node.path}' requires an external decision",
        )
    return WorkflowLeafOutcome(
        WorkflowExecutionStatus.BLOCKED,
        error=f"no executor is configured for {node.kind} leaf '{node.path}'",
    )


class _WorkflowExecutor:
    def __init__(
        self,
        resolution: WorkflowGraphResolution,
        ledger: ExecutionLedger,
        leaf_executor: WorkflowLeafExecutor,
        cancelled: Callable[[], bool] | None,
    ) -> None:
        self.resolution = resolution
        self.graph = resolution.graph
        self.ledger = ledger
        self.leaf_executor = leaf_executor
        self.cancelled = cancelled
        self.nodes = {item.path: item for item in self.graph.nodes}
        self.by_id: dict[str, list[str]] = {}
        for node in self.graph.nodes:
            self.by_id.setdefault(node.id, []).append(node.path)
        self.records: dict[str, WorkflowNodeExecution] = {}
        self.outputs: dict[str, object | None] = {}
        self.input_sha256 = _hash_json(resolution.input_values)
        self.plan_sha256 = _hash_json(
            {
                "graphSha256": self.graph.sha256,
                "inputSha256": self.input_sha256,
                "workflow": resolution.name,
            }
        )

    def _context(self) -> dict[str, object]:
        return {"inputs": self.resolution.input_values, "steps": {}}

    def _task_id(self, identity: str, context: Mapping[str, object]) -> tuple[str, str]:
        context_sha = _hash_json(context)
        digest = sha256(
            f"{self.plan_sha256}\n{identity}\n{context_sha}".encode("utf-8")
        ).hexdigest()
        return f"wf-{digest[:32]}", context_sha

    def _registration(self, task_id: str) -> LedgerEvent | None:
        return next(
            (
                event
                for event in self.ledger.load_events()
                if event.kind == "task.registered" and event.task_id == task_id
            ),
            None,
        )

    def _failed_attempts(self, task_id: str) -> int:
        return sum(
            1
            for event in self.ledger.load_events()
            if event.task_id == task_id
            and event.kind == "task.evidence"
            and event.payload.get("workflowEngine2AttemptStatus") == "failed"
        )

    def _read_completed_outcome(self, task_id: str) -> WorkflowLeafOutcome:
        path = self.ledger.task_record_paths(task_id)["evidence"]
        if path.is_symlink() or not path.is_file():
            raise _fail("SDAI-WF2-EXEC-006", f"completed workflow task '{task_id}' has no evidence record")
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            payload = raw["payload"]
            outcome = payload["outcome"]
            result = WorkflowLeafOutcome(
                WorkflowExecutionStatus(str(outcome["status"])),
                outcome.get("output"),
                tuple(outcome.get("evidenceReferences") or ()),
                outcome.get("error"),
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise _fail("SDAI-WF2-EXEC-006", f"workflow task evidence is malformed: {task_id}") from exc
        state = self.ledger.reconstruct().task_map().get(task_id)
        if state is None or state.status != "completed":
            raise _fail("SDAI-WF2-EXEC-006", f"workflow task is not durably completed: {task_id}")
        binding = self.ledger.binding_for_file(path, kind="evidence")
        if binding not in state.bindings:
            raise _fail("SDAI-WF2-EXEC-006", f"workflow task evidence binding changed: {task_id}")
        evidence = tuple(
            dict.fromkeys(
                (*result.evidence_references, *(item.source for item in state.bindings))
            )
        )
        return WorkflowLeafOutcome(
            result.status,
            result.output,
            evidence,
            result.error,
        )

    def _checkpoint_records(
        self,
        engine: Mapping[str, object],
    ) -> tuple[WorkflowNodeExecution, ...]:
        raw_nodes = engine.get("nodes")
        if not isinstance(raw_nodes, Mapping):
            raise _fail("SDAI-WF2-EXEC-007", "completed run checkpoint has invalid node records")
        records: list[WorkflowNodeExecution] = []
        try:
            for identity in sorted(raw_nodes):
                raw = raw_nodes[identity]
                if not isinstance(identity, str) or not isinstance(raw, Mapping):
                    raise TypeError
                output = raw.get("output")
                record = WorkflowNodeExecution(
                    identity=str(raw["identity"]),
                    path=str(raw["path"]),
                    kind=str(raw["kind"]),
                    status=str(raw["status"]),
                    output=output,
                    output_sha256=str(raw["outputSha256"]),
                    task_id=None if raw.get("taskId") is None else str(raw["taskId"]),
                    evidence_references=tuple(str(item) for item in raw.get("evidenceReferences", ())),
                )
                if record.identity != identity or record.output_sha256 != _hash_json(output):
                    raise ValueError
                records.append(record)
        except (KeyError, TypeError, ValueError) as exc:
            raise _fail("SDAI-WF2-EXEC-007", "completed run checkpoint node records are stale") from exc
        return tuple(records)

    def _persist_outcome(
        self,
        task_id: str,
        identity: str,
        node: WorkflowGraphNode,
        outcome: WorkflowLeafOutcome,
    ) -> tuple[str, ...]:
        payload = {
            "apiVersion": "sdai.workflow-leaf-evidence/v2",
            "workflowPlanSha256": self.plan_sha256,
            "graphSha256": self.graph.sha256,
            "executionIdentity": identity,
            "nodePath": node.path,
            "outcome": outcome.as_dict(),
            "outcomeSha256": _hash_json(outcome.as_dict()),
        }
        binding = self.ledger.write_task_record(task_id, "evidence", payload)
        self.ledger.append_event(
            "task.evidence",
            task_id=task_id,
            bindings=(binding,),
            payload={"workflowEngine2": True, "outcomeSha256": payload["outcomeSha256"]},
        )
        self.ledger.append_event(
            "task.completed",
            task_id=task_id,
            git_commit=self.ledger.manifest.baseline_commit,
            bindings=(binding,),
            payload={"workflowEngine2": True, "status": outcome.status.value},
        )
        return tuple((*outcome.evidence_references, binding.source))

    def _retire_conflicting_tasks(self, identity: str, task_id: str) -> None:
        events = self.ledger.load_events()
        state = self.ledger.reconstruct()
        for registration in events:
            if (
                registration.kind != "task.registered"
                or registration.task_id is None
                or registration.task_id == task_id
                or registration.payload.get("workflowEngine2") is not True
                or registration.payload.get("executionIdentity") != identity
            ):
                continue
            stale_id = registration.task_id
            stale = state.task_map().get(stale_id)
            if stale is None or stale.status == "completed":
                continue
            if stale.status == "failed":
                self.ledger.append_event(
                    "task.reopened",
                    task_id=stale_id,
                    payload={"reason": "workflow-input-invalidated"},
                )
                stale = self.ledger.reconstruct().task_map()[stale_id]
            if stale.status == "registered":
                self.ledger.append_event("task.started", task_id=stale_id)
            stale_node_path = str(registration.payload.get("nodePath") or "")
            stale_node = self.nodes.get(stale_node_path)
            if stale_node is None:
                raise _fail("SDAI-WF2-EXEC-007", f"stale task references unknown node: {stale_node_path}")
            self._persist_outcome(
                stale_id,
                identity,
                stale_node,
                WorkflowLeafOutcome(
                    WorkflowExecutionStatus.CANCELLED,
                    error="invalidated by changed upstream input/context",
                ),
            )

    def _retire_stale_plan_tasks(self) -> None:
        events = self.ledger.load_events()
        state = self.ledger.reconstruct()
        for registration in events:
            if (
                registration.kind != "task.registered"
                or registration.task_id is None
                or registration.payload.get("workflowEngine2") is not True
                or registration.payload.get("workflowPlanSha256") == self.plan_sha256
            ):
                continue
            task_id = registration.task_id
            stale = state.task_map().get(task_id)
            if stale is None or stale.status == "completed":
                continue
            if stale.status == "failed":
                self.ledger.append_event(
                    "task.reopened",
                    task_id=task_id,
                    payload={"reason": "workflow-plan-invalidated"},
                )
                stale = self.ledger.reconstruct().task_map()[task_id]
            if stale.status == "registered":
                self.ledger.append_event("task.started", task_id=task_id)
            path = str(registration.payload.get("nodePath") or "stale")
            node = self.nodes.get(path) or WorkflowGraphNode(
                path=path,
                id=path.rsplit("/", 1)[-1],
                kind=str(registration.payload.get("nodeKind") or "unknown"),
                parent=None,
                index=0,
            )
            self._persist_outcome(
                task_id,
                str(registration.payload.get("executionIdentity") or path),
                node,
                WorkflowLeafOutcome(
                    WorkflowExecutionStatus.CANCELLED,
                    error="invalidated by changed workflow graph/input plan",
                ),
            )

    def _record(
        self,
        identity: str,
        node: WorkflowGraphNode,
        status: str,
        output: object | None,
        *,
        task_id: str | None = None,
        evidence: tuple[str, ...] = (),
    ) -> WorkflowNodeExecution:
        record = WorkflowNodeExecution(
            identity,
            node.path,
            node.kind,
            status,
            output,
            _hash_json(output),
            task_id,
            evidence,
        )
        self.records[identity] = record
        self.outputs[identity] = output
        self.outputs[node.path] = output
        self._checkpoint(status)
        return record

    def _checkpoint(self, status: str) -> None:
        self.ledger.write_checkpoint(
            {
                "workflowEngine2": {
                    "apiVersion": WORKFLOW_EXECUTION_CHECKPOINT_API_VERSION,
                    "workflow": self.resolution.name,
                    "graphSha256": self.graph.sha256,
                    "inputSha256": self.input_sha256,
                    "planSha256": self.plan_sha256,
                    "status": status,
                    "nodes": {
                        key: self.records[key].as_dict()
                        for key in sorted(self.records)
                    },
                }
            }
        )

    def _check_cancelled(self) -> None:
        if self.cancelled is not None and self.cancelled():
            if self.ledger.reconstruct().status == "active":
                self.ledger.append_event(
                    "run.cancelled",
                    payload={"reason": "workflow-engine2-cancellation", "planSha256": self.plan_sha256},
                )
            self._checkpoint(WorkflowExecutionStatus.CANCELLED.value)
            raise _Halt(WorkflowExecutionStatus.CANCELLED, "workflow execution was cancelled")

    def _pause(self, status: WorkflowExecutionStatus, message: str | None) -> None:
        state = self.ledger.reconstruct()
        if state.status == "active":
            self.ledger.append_event(
                "run.paused",
                payload={
                    "reason": status.value,
                    "message": message,
                    "planSha256": self.plan_sha256,
                },
            )
        self._checkpoint(status.value)
        raise _Halt(status, message)

    def _execute_leaf(
        self,
        node: WorkflowGraphNode,
        context: dict[str, object],
        identity: str,
    ) -> WorkflowNodeExecution:
        planning_binding: str | None = None
        planner = getattr(self.leaf_executor, "planning_binding", None)
        if callable(planner):
            try:
                candidate = planner(node)
            except Exception as exc:
                raise _fail(
                    "SDAI-WF2-EXEC-009",
                    f"leaf planning failed for '{node.path}': {exc}",
                ) from exc
            if candidate is not None and (not isinstance(candidate, str) or not candidate):
                raise _fail("SDAI-WF2-EXEC-009", "leaf planning binding must be a string or null")
            planning_binding = candidate
        task_context: Mapping[str, object] = context
        if planning_binding is not None:
            task_context = {
                "context": context,
                "leafPlanningBinding": planning_binding,
            }
        task_id, context_sha = self._task_id(identity, task_context)
        state = self.ledger.reconstruct()
        current = state.task_map().get(task_id)
        registration = self._registration(task_id)
        expected_registration = {
            "workflowEngine2": True,
            "workflowPlanSha256": self.plan_sha256,
            "graphSha256": self.graph.sha256,
            "executionIdentity": identity,
            "nodePath": node.path,
            "nodeKind": node.kind,
            "contextSha256": context_sha,
        }
        if planning_binding is not None:
            expected_registration["leafPlanningBinding"] = planning_binding
        if current is None:
            self._retire_conflicting_tasks(identity, task_id)
            self.ledger.append_event(
                "task.registered",
                task_id=task_id,
                payload=expected_registration,
            )
            current = self.ledger.reconstruct().task_map()[task_id]
        elif registration is None or any(
            registration.payload.get(key) != value
            for key, value in expected_registration.items()
        ):
            raise _fail("SDAI-WF2-EXEC-006", f"workflow task identity collision: {task_id}")

        if current.status == "completed":
            outcome = self._read_completed_outcome(task_id)
            evidence = tuple(outcome.evidence_references)
            record = self._record(
                identity,
                node,
                outcome.status.value,
                outcome.output,
                task_id=task_id,
                evidence=evidence,
            )
            return record
        if current.status == "failed":
            self.ledger.append_event("task.reopened", task_id=task_id, payload={"reason": "workflow-retry"})
            current = self.ledger.reconstruct().task_map()[task_id]
        if current.status == "registered":
            self.ledger.append_event("task.started", task_id=task_id)

        retry = node.config.get("retry") or {}
        maximum = int(retry.get("maxAttempts") or 1) if isinstance(retry, Mapping) else 1
        failures = self._failed_attempts(task_id)
        attempt = failures + 1
        while attempt <= maximum:
            self._check_cancelled()
            dispatch_id = "wf-dispatch-" + sha256(f"{task_id}:{attempt}".encode("utf-8")).hexdigest()[:24]
            outcome = self.leaf_executor(
                WorkflowLeafInvocation(
                    node,
                    identity,
                    dispatch_id,
                    attempt,
                    dict(context),
                    planning_binding,
                )
            )
            if not isinstance(outcome, WorkflowLeafOutcome):
                raise _fail("SDAI-WF2-EXEC-004", "leaf executor must return WorkflowLeafOutcome")
            if outcome.status == WorkflowExecutionStatus.FAILED and attempt < maximum:
                self.ledger.append_event(
                    "task.evidence",
                    task_id=task_id,
                    payload={
                        "workflowEngine2AttemptStatus": "failed",
                        "attempt": attempt,
                        "dispatchId": dispatch_id,
                        "outcomeSha256": _hash_json(outcome.as_dict()),
                    },
                )
                attempt += 1
                continue
            if outcome.status in {WorkflowExecutionStatus.PAUSED, WorkflowExecutionStatus.BLOCKED}:
                self._pause(outcome.status, outcome.error)
            evidence = self._persist_outcome(task_id, identity, node, outcome)
            record = self._record(
                identity,
                node,
                outcome.status.value,
                outcome.output,
                task_id=task_id,
                evidence=evidence,
            )
            if outcome.status == WorkflowExecutionStatus.CANCELLED:
                if self.ledger.reconstruct().status == "active":
                    self.ledger.append_event("run.cancelled", payload={"reason": outcome.error or "leaf-cancelled"})
                raise _Halt(WorkflowExecutionStatus.CANCELLED, outcome.error)
            if outcome.status == WorkflowExecutionStatus.FAILED and node.config.get("onFailure") != "continue":
                if self.ledger.reconstruct().status == "active":
                    self.ledger.append_event("run.failed", payload={"reason": outcome.error or "leaf-failed"})
                raise _Halt(WorkflowExecutionStatus.FAILED, outcome.error)
            return record
        raise AssertionError("retry loop exhausted without outcome")

    def _set_context_output(
        self,
        context: dict[str, object],
        node: WorkflowGraphNode,
        record: WorkflowNodeExecution,
    ) -> None:
        raw_steps = context.setdefault("steps", {})
        assert isinstance(raw_steps, dict)
        value = {"status": record.status, "output": record.output, "sha256": record.output_sha256}
        raw_steps[node.id] = value
        raw_steps[node.path] = value

    def _resolve_source(self, source: str) -> str:
        if source in self.nodes:
            return source
        matches = self.by_id.get(source, [])
        if len(matches) != 1:
            raise _fail("SDAI-WF2-EXEC-005", f"fan-in source '{source}' is missing or ambiguous")
        return matches[0]

    def _execute_children(
        self,
        paths: tuple[str, ...],
        context: dict[str, object],
        prefix: str,
    ) -> list[object | None]:
        outputs: list[object | None] = []
        for path in paths:
            record = self._execute_path(path, context, prefix)
            outputs.append(record.output)
        return outputs

    def _execute_path(
        self,
        path: str,
        context: dict[str, object],
        prefix: str = "",
    ) -> WorkflowNodeExecution:
        self._check_cancelled()
        node = self.nodes[path]
        identity = f"{prefix}::{path}" if prefix else path
        if node.kind in _LEAF_KINDS:
            record = self._execute_leaf(node, context, identity)
            self._set_context_output(context, node, record)
            return record
        if node.kind not in _CONTROL_KINDS:
            raise _fail("SDAI-WF2-EXEC-003", f"unsupported graph node kind '{node.kind}'")

        if node.kind in {WorkflowNodeKind.SEQUENCE.value, WorkflowNodeKind.PARALLEL.value}:
            if node.kind == WorkflowNodeKind.PARALLEL.value:
                maximum = node.config.get("maxConcurrency")
                if not isinstance(maximum, int) or maximum < 1 or maximum > 32:
                    raise _fail("SDAI-WF2-EXEC-003", f"parallel node '{path}' has invalid concurrency bound")
                writable = (
                    [child for child in node.children if self._subtree_requires_write(child)]
                    if maximum > 1
                    else []
                )
                if writable:
                    raise _fail(
                        "SDAI-WF2-EXEC-003",
                        f"parallel node '{path}' contains concurrent workspace-write branches",
                    )
            output = self._execute_children(node.children, context, identity)

        elif node.kind == WorkflowNodeKind.IF.value:
            condition = bool(evaluate_workflow_expression(node.config["condition"], context))
            label = "then" if condition else "else"
            branch = next((item for item in node.branches if item.label == label), None)
            output = [] if branch is None else self._execute_children(branch.children, context, f"{identity}:{label}")

        elif node.kind == WorkflowNodeKind.SWITCH.value:
            selected = evaluate_workflow_expression(node.config["value"], context)
            branch = None
            for candidate in node.branches:
                if candidate.when is not None and evaluate_workflow_expression(candidate.when, context) == selected:
                    branch = candidate
                    break
            if branch is None:
                branch = next((item for item in node.branches if item.label == "default"), None)
            output = [] if branch is None else self._execute_children(branch.children, context, f"{identity}:{branch.id}")

        elif node.kind in {WorkflowNodeKind.FAN_OUT.value, WorkflowNodeKind.FOREACH.value}:
            items = evaluate_workflow_expression(node.config["items"], context)
            if not isinstance(items, list):
                raise _fail("SDAI-WF2-EXEC-003", f"{node.kind} node '{path}' items must resolve to a list")
            maximum = node.config.get("maxItems")
            if not isinstance(maximum, int) or maximum < 1 or len(items) > maximum:
                raise _fail("SDAI-WF2-EXEC-003", f"{node.kind} node '{path}' exceeds maxItems")
            if node.kind == WorkflowNodeKind.FAN_OUT.value:
                concurrency = node.config.get("maxConcurrency")
                if not isinstance(concurrency, int) or concurrency < 1 or concurrency > 32:
                    raise _fail("SDAI-WF2-EXEC-003", f"fan-out node '{path}' has invalid concurrency bound")
                if concurrency > 1 and self._subtree_requires_write(node.children[0]):
                    raise _fail(
                        "SDAI-WF2-EXEC-003",
                        f"fan-out node '{path}' cannot concurrently execute workspace-write branches",
                    )
            output = []
            variable = str(node.config.get("as") or "item")
            for index, item in enumerate(items):
                item_context = json.loads(_canonical_json(context))
                item_context["item"] = {variable: item, "index": index}
                item_id = _hash_json(item).removeprefix("sha256:")[:12]
                output.append(
                    self._execute_children(
                        node.children,
                        item_context,
                        f"{identity}:item-{index:04d}-{item_id}",
                    )
                )

        elif node.kind == WorkflowNodeKind.BOUNDED_WHILE.value:
            maximum = node.config.get("maxIterations")
            if not isinstance(maximum, int) or maximum < 1 or maximum > 100:
                raise _fail("SDAI-WF2-EXEC-003", f"bounded-while node '{path}' has invalid iteration bound")
            output = []
            for iteration in range(maximum):
                context["loop"] = {"iteration": iteration, "previous": output[-1] if output else None}
                if not bool(evaluate_workflow_expression(node.config["condition"], context)):
                    break
                output.append(
                    self._execute_children(node.children, context, f"{identity}:iteration-{iteration:04d}")
                )
            else:
                context["loop"] = {"iteration": maximum, "previous": output[-1] if output else None}
                if bool(evaluate_workflow_expression(node.config["condition"], context)):
                    raise _fail(
                        "SDAI-WF2-EXEC-008",
                        f"bounded-while node '{path}' reached maxIterations without termination",
                    )

        elif node.kind == WorkflowNodeKind.FAN_IN.value:
            sources = node.config.get("sources")
            if not isinstance(sources, list):
                raise _fail("SDAI-WF2-EXEC-003", f"fan-in node '{path}' sources are invalid")
            output = []
            for source in sources:
                resolved = self._resolve_source(str(source))
                if resolved not in self.outputs:
                    raise _fail("SDAI-WF2-EXEC-005", f"fan-in source '{resolved}' has no completed output")
                output.append(self.outputs[resolved])

        else:  # pragma: no cover - exhaustive control set
            raise AssertionError(node.kind)

        record = self._record(identity, node, WorkflowExecutionStatus.SUCCEEDED.value, output)
        self._set_context_output(context, node, record)
        return record

    def _subtree_requires_write(self, path: str) -> bool:
        node = self.nodes[path]
        if node.kind == WorkflowNodeKind.AGENT.value and node.config.get("mode") == "workspace-write":
            return True
        if node.kind == WorkflowNodeKind.SAFE_COMMAND.value and node.config.get("requiresWorkspaceWrite") is True:
            return True
        if node.kind == WorkflowNodeKind.PLUGIN.value:
            return True
        return any(self._subtree_requires_write(child) for child in node.children)

    def execute(self) -> WorkflowExecutionResult:
        if self.ledger.manifest.workflow != self.resolution.name:
            raise _fail("SDAI-WF2-EXEC-002", "ledger workflow identity does not match graph resolution")
        state = self.ledger.reconstruct()
        if state.status == "paused":
            self.ledger.append_event(
                "run.resumed",
                payload={"reason": "workflow-engine2-resume", "planSha256": self.plan_sha256},
            )
        elif state.status == "completed":
            checkpoint = self.ledger.load_checkpoint()
            engine = None if checkpoint is None else checkpoint.get("extra", {}).get("workflowEngine2")
            if not isinstance(engine, Mapping) or engine.get("planSha256") != self.plan_sha256:
                raise _fail("SDAI-WF2-EXEC-007", "completed run checkpoint is stale for this graph/input")
            return WorkflowExecutionResult(
                self.ledger.manifest.run_id,
                self.resolution.name,
                self.graph.sha256,
                self.input_sha256,
                WorkflowExecutionStatus.SUCCEEDED,
                self._checkpoint_records(engine),
            )
        elif state.status in {"failed", "cancelled"}:
            raise _fail("SDAI-WF2-EXEC-007", f"terminal run cannot resume from {state.status}")

        self._retire_stale_plan_tasks()

        try:
            context = self._context()
            self._execute_path(self.graph.root, context)
            if self.ledger.reconstruct().status == "active":
                self.ledger.append_event(
                    "run.completed",
                    git_commit=self.ledger.manifest.baseline_commit,
                    payload={"workflowPlanSha256": self.plan_sha256},
                )
            self._checkpoint(WorkflowExecutionStatus.SUCCEEDED.value)
            status = WorkflowExecutionStatus.SUCCEEDED
            error = None
        except _Halt as halt:
            status = halt.status
            error = halt.error
        except (WorkflowExecutionError, WorkflowGraphError) as exc:
            if self.ledger.reconstruct().status == "active":
                self.ledger.append_event(
                    "run.failed",
                    payload={"reason": str(exc), "planSha256": self.plan_sha256},
                )
            self._checkpoint(WorkflowExecutionStatus.FAILED.value)
            status = WorkflowExecutionStatus.FAILED
            error = str(exc)
        return WorkflowExecutionResult(
            self.ledger.manifest.run_id,
            self.resolution.name,
            self.graph.sha256,
            self.input_sha256,
            status,
            tuple(self.records[key] for key in sorted(self.records)),
            error,
        )


def execute_workflow_graph(
    resolution: WorkflowGraphResolution,
    ledger: ExecutionLedger,
    *,
    leaf_executor: WorkflowLeafExecutor | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> WorkflowExecutionResult:
    """Execute or resume a bounded Workflow Engine 2 graph on the durable ledger."""

    try:
        return _WorkflowExecutor(
            resolution,
            ledger,
            leaf_executor or default_workflow_leaf_executor,
            cancelled,
        ).execute()
    except ExecutionLedgerError as exc:
        raise _fail("SDAI-WF2-EXEC-006", str(exc)) from exc
