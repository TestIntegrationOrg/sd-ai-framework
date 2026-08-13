from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path, PurePosixPath
import re
from typing import Mapping

from sdai.execution_ledger import ExecutionLedger, ExecutionLedgerError, HashBinding
from sdai.models import validate_feature_id


DEBUG_RECORD_API_VERSION = "sdai.debug-record/v1"
DEBUGGER_ROLE = "debugger"


class DebugRecordError(RuntimeError):
    """Raised when debugger evidence is incomplete, inconsistent, or unsafe."""


_ID = re.compile(r"^[A-Z][A-Z0-9_-]{2,63}$")
_TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_TOP_LEVEL = frozenset(
    {
        "apiVersion",
        "feature_id",
        "run_id",
        "task_id",
        "semantic_role",
        "status",
        "reproduction",
        "observations",
        "hypotheses",
        "experiments",
        "root_cause",
        "fix",
        "regression_evidence",
        "producer",
    }
)
_REPRODUCTION_KEYS = frozenset({"steps", "observed", "expected"})
_OBSERVATION_KEYS = frozenset({"id", "fact", "source"})
_HYPOTHESIS_KEYS = frozenset({"id", "statement", "status", "observation_ids"})
_EXPERIMENT_KEYS = frozenset(
    {"id", "hypothesis_id", "action", "result", "conclusion"}
)
_ROOT_CAUSE_KEYS = frozenset({"statement", "evidence_ids", "confidence"})
_FIX_KEYS = frozenset({"summary", "files"})
_REGRESSION_KEYS = frozenset({"id", "command", "result", "status"})
_PRODUCER_KEYS = frozenset({"agent", "provider", "model"})
_STATUS = frozenset({"investigating", "root-cause-confirmed", "fixed"})
_HYPOTHESIS_STATUS = frozenset({"open", "supported", "rejected"})
_EXPERIMENT_CONCLUSION = frozenset({"supports", "rejects", "inconclusive"})
_ROOT_CONFIDENCE = frozenset({"suspected", "probable", "confirmed"})
_REGRESSION_STATUS = frozenset({"passed", "failed"})


def _fail(code: str, message: str) -> DebugRecordError:
    return DebugRecordError(f"{code}: {message}")


def _mapping(value: object, *, label: str, keys: frozenset[str]) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise _fail("SDAI-DEBUG-001", f"{label} must be a mapping")
    raw = dict(value)
    unknown = sorted(str(key) for key in raw if key not in keys)
    missing = sorted(key for key in keys if key not in raw)
    if unknown:
        raise _fail(
            "SDAI-DEBUG-001",
            f"{label} contains unknown field(s): {', '.join(unknown)}",
        )
    if missing:
        raise _fail(
            "SDAI-DEBUG-001",
            f"{label} is missing field(s): {', '.join(missing)}",
        )
    return raw


def _string(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _fail("SDAI-DEBUG-001", f"{label} must be a non-empty string")
    return value.strip()


def _identifier(value: object, *, label: str) -> str:
    text = _string(value, label=label)
    if not _ID.fullmatch(text):
        raise _fail(
            "SDAI-DEBUG-001",
            f"{label} must use 3-64 uppercase letters, numbers, underscore, or hyphen",
        )
    return text


def _string_list(
    value: object,
    *,
    label: str,
    minimum: int = 0,
    identifier: bool = False,
) -> list[str]:
    if not isinstance(value, list):
        raise _fail("SDAI-DEBUG-001", f"{label} must be a list")
    if len(value) < minimum:
        raise _fail(
            "SDAI-DEBUG-001",
            f"{label} must contain at least {minimum} item(s)",
        )
    result: list[str] = []
    for index, item in enumerate(value):
        text = (
            _identifier(item, label=f"{label}[{index}]")
            if identifier
            else _string(item, label=f"{label}[{index}]")
        )
        result.append(text)
    if len(result) != len(set(result)):
        raise _fail("SDAI-DEBUG-001", f"{label} must not contain duplicates")
    return result


def _portable_repo_path(value: object, *, label: str) -> str:
    text = _string(value, label=label)
    if "\\" in text or text.startswith("/") or re.match(r"^[A-Za-z]:", text):
        raise _fail("SDAI-DEBUG-001", f"{label} must be a repository-relative POSIX path")
    path = PurePosixPath(text)
    if any(part in {"", ".", ".."} for part in path.parts):
        raise _fail("SDAI-DEBUG-001", f"{label} contains unsafe path traversal")
    return path.as_posix()


def _canonical_bytes(payload: Mapping[str, object]) -> bytes:
    try:
        return json.dumps(
            payload,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise _fail("SDAI-DEBUG-001", f"debug record is not canonical JSON: {exc}") from exc


def debug_record_sha256(record: Mapping[str, object]) -> str:
    return "sha256:" + sha256(_canonical_bytes(record)).hexdigest()


def _validate_identity(
    raw: dict[str, object],
    *,
    feature_id: str | None,
    run_id: str | None,
    task_id: str | None,
) -> tuple[str, str, str]:
    feature = validate_feature_id(_string(raw["feature_id"], label="feature_id"))
    run = _string(raw["run_id"], label="run_id")
    task = _string(raw["task_id"], label="task_id")
    if not _RUN_ID.fullmatch(run):
        raise _fail("SDAI-DEBUG-001", f"invalid run_id: {run!r}")
    if not _TASK_ID.fullmatch(task):
        raise _fail("SDAI-DEBUG-001", f"invalid task_id: {task!r}")
    if feature_id is not None and feature != validate_feature_id(feature_id):
        raise _fail("SDAI-DEBUG-002", "debug record feature_id does not match execution run")
    if run_id is not None and run != run_id:
        raise _fail("SDAI-DEBUG-002", "debug record run_id does not match execution run")
    if task_id is not None and task != task_id:
        raise _fail("SDAI-DEBUG-002", "debug record task_id does not match execution task")
    return feature, run, task


def validate_debug_record(
    value: object,
    *,
    feature_id: str | None = None,
    run_id: str | None = None,
    task_id: str | None = None,
    require_completion: bool = False,
) -> dict[str, object]:
    raw = _mapping(value, label="debug record", keys=_TOP_LEVEL)
    if raw["apiVersion"] != DEBUG_RECORD_API_VERSION:
        raise _fail(
            "SDAI-DEBUG-001",
            f"apiVersion must be {DEBUG_RECORD_API_VERSION!r}",
        )
    feature, run, task = _validate_identity(
        raw,
        feature_id=feature_id,
        run_id=run_id,
        task_id=task_id,
    )
    if raw["semantic_role"] != DEBUGGER_ROLE:
        raise _fail("SDAI-DEBUG-001", "semantic_role must remain 'debugger'")
    status = _string(raw["status"], label="status")
    if status not in _STATUS:
        raise _fail("SDAI-DEBUG-001", f"unsupported debug status: {status!r}")

    reproduction_raw = _mapping(
        raw["reproduction"],
        label="reproduction",
        keys=_REPRODUCTION_KEYS,
    )
    reproduction = {
        "steps": _string_list(
            reproduction_raw["steps"],
            label="reproduction.steps",
            minimum=1,
        ),
        "observed": _string(reproduction_raw["observed"], label="reproduction.observed"),
        "expected": _string(reproduction_raw["expected"], label="reproduction.expected"),
    }

    observations_raw = raw["observations"]
    if not isinstance(observations_raw, list) or not observations_raw:
        raise _fail("SDAI-DEBUG-001", "observations must contain at least one item")
    observations: list[dict[str, str]] = []
    observation_ids: set[str] = set()
    for index, item in enumerate(observations_raw):
        current = _mapping(item, label=f"observations[{index}]", keys=_OBSERVATION_KEYS)
        current_id = _identifier(current["id"], label=f"observations[{index}].id")
        if current_id in observation_ids:
            raise _fail("SDAI-DEBUG-001", f"duplicate observation id: {current_id}")
        observation_ids.add(current_id)
        observations.append(
            {
                "id": current_id,
                "fact": _string(current["fact"], label=f"observations[{index}].fact"),
                "source": _string(current["source"], label=f"observations[{index}].source"),
            }
        )

    hypotheses_raw = raw["hypotheses"]
    if not isinstance(hypotheses_raw, list) or not hypotheses_raw:
        raise _fail("SDAI-DEBUG-001", "hypotheses must contain at least one item")
    hypotheses: list[dict[str, object]] = []
    hypothesis_ids: set[str] = set()
    for index, item in enumerate(hypotheses_raw):
        current = _mapping(item, label=f"hypotheses[{index}]", keys=_HYPOTHESIS_KEYS)
        current_id = _identifier(current["id"], label=f"hypotheses[{index}].id")
        if current_id in hypothesis_ids:
            raise _fail("SDAI-DEBUG-001", f"duplicate hypothesis id: {current_id}")
        hypothesis_ids.add(current_id)
        current_status = _string(current["status"], label=f"hypotheses[{index}].status")
        if current_status not in _HYPOTHESIS_STATUS:
            raise _fail("SDAI-DEBUG-001", f"unsupported hypothesis status: {current_status!r}")
        refs = _string_list(
            current["observation_ids"],
            label=f"hypotheses[{index}].observation_ids",
            minimum=1,
            identifier=True,
        )
        unknown = sorted(set(refs) - observation_ids)
        if unknown:
            raise _fail(
                "SDAI-DEBUG-003",
                f"hypothesis {current_id} references unknown observation(s): {', '.join(unknown)}",
            )
        hypotheses.append(
            {
                "id": current_id,
                "statement": _string(
                    current["statement"],
                    label=f"hypotheses[{index}].statement",
                ),
                "status": current_status,
                "observation_ids": refs,
            }
        )

    experiments_raw = raw["experiments"]
    if not isinstance(experiments_raw, list) or not experiments_raw:
        raise _fail("SDAI-DEBUG-001", "experiments must contain at least one item")
    experiments: list[dict[str, str]] = []
    experiment_ids: set[str] = set()
    for index, item in enumerate(experiments_raw):
        current = _mapping(item, label=f"experiments[{index}]", keys=_EXPERIMENT_KEYS)
        current_id = _identifier(current["id"], label=f"experiments[{index}].id")
        if current_id in experiment_ids:
            raise _fail("SDAI-DEBUG-001", f"duplicate experiment id: {current_id}")
        experiment_ids.add(current_id)
        hypothesis = _identifier(
            current["hypothesis_id"],
            label=f"experiments[{index}].hypothesis_id",
        )
        if hypothesis not in hypothesis_ids:
            raise _fail(
                "SDAI-DEBUG-003",
                f"experiment {current_id} references unknown hypothesis {hypothesis}",
            )
        conclusion = _string(
            current["conclusion"],
            label=f"experiments[{index}].conclusion",
        )
        if conclusion not in _EXPERIMENT_CONCLUSION:
            raise _fail("SDAI-DEBUG-001", f"unsupported experiment conclusion: {conclusion!r}")
        experiments.append(
            {
                "id": current_id,
                "hypothesis_id": hypothesis,
                "action": _string(current["action"], label=f"experiments[{index}].action"),
                "result": _string(current["result"], label=f"experiments[{index}].result"),
                "conclusion": conclusion,
            }
        )

    root_cause: dict[str, object] | None
    if raw["root_cause"] is None:
        root_cause = None
    else:
        current = _mapping(raw["root_cause"], label="root_cause", keys=_ROOT_CAUSE_KEYS)
        refs = _string_list(
            current["evidence_ids"],
            label="root_cause.evidence_ids",
            minimum=1,
            identifier=True,
        )
        evidence_ids = observation_ids | experiment_ids
        unknown = sorted(set(refs) - evidence_ids)
        if unknown:
            raise _fail(
                "SDAI-DEBUG-003",
                f"root_cause references unknown evidence id(s): {', '.join(unknown)}",
            )
        confidence = _string(current["confidence"], label="root_cause.confidence")
        if confidence not in _ROOT_CONFIDENCE:
            raise _fail("SDAI-DEBUG-001", f"unsupported root-cause confidence: {confidence!r}")
        root_cause = {
            "statement": _string(current["statement"], label="root_cause.statement"),
            "evidence_ids": refs,
            "confidence": confidence,
        }

    fix: dict[str, object] | None
    if raw["fix"] is None:
        fix = None
    else:
        current = _mapping(raw["fix"], label="fix", keys=_FIX_KEYS)
        files_raw = current["files"]
        if not isinstance(files_raw, list) or not files_raw:
            raise _fail("SDAI-DEBUG-001", "fix.files must contain at least one path")
        files = [
            _portable_repo_path(item, label=f"fix.files[{index}]")
            for index, item in enumerate(files_raw)
        ]
        if len(files) != len(set(files)):
            raise _fail("SDAI-DEBUG-001", "fix.files must not contain duplicates")
        fix = {
            "summary": _string(current["summary"], label="fix.summary"),
            "files": files,
        }

    regressions_raw = raw["regression_evidence"]
    if not isinstance(regressions_raw, list):
        raise _fail("SDAI-DEBUG-001", "regression_evidence must be a list")
    regressions: list[dict[str, str]] = []
    regression_ids: set[str] = set()
    for index, item in enumerate(regressions_raw):
        current = _mapping(
            item,
            label=f"regression_evidence[{index}]",
            keys=_REGRESSION_KEYS,
        )
        current_id = _identifier(current["id"], label=f"regression_evidence[{index}].id")
        if current_id in regression_ids:
            raise _fail("SDAI-DEBUG-001", f"duplicate regression evidence id: {current_id}")
        regression_ids.add(current_id)
        regression_status = _string(
            current["status"],
            label=f"regression_evidence[{index}].status",
        )
        if regression_status not in _REGRESSION_STATUS:
            raise _fail("SDAI-DEBUG-001", f"unsupported regression status: {regression_status!r}")
        regressions.append(
            {
                "id": current_id,
                "command": _string(current["command"], label=f"regression_evidence[{index}].command"),
                "result": _string(current["result"], label=f"regression_evidence[{index}].result"),
                "status": regression_status,
            }
        )

    producer_raw = _mapping(raw["producer"], label="producer", keys=_PRODUCER_KEYS)
    producer_agent = _string(producer_raw["agent"], label="producer.agent")
    if producer_agent != DEBUGGER_ROLE:
        raise _fail("SDAI-DEBUG-001", "producer.agent must remain 'debugger'")
    producer_model = producer_raw["model"]
    if producer_model is not None and (
        not isinstance(producer_model, str) or not producer_model.strip()
    ):
        raise _fail("SDAI-DEBUG-001", "producer.model must be null or a non-empty string")
    producer = {
        "agent": DEBUGGER_ROLE,
        "provider": _string(producer_raw["provider"], label="producer.provider"),
        "model": producer_model.strip() if isinstance(producer_model, str) else None,
    }

    normalized: dict[str, object] = {
        "apiVersion": DEBUG_RECORD_API_VERSION,
        "feature_id": feature,
        "run_id": run,
        "task_id": task,
        "semantic_role": DEBUGGER_ROLE,
        "status": status,
        "reproduction": reproduction,
        "observations": observations,
        "hypotheses": hypotheses,
        "experiments": experiments,
        "root_cause": root_cause,
        "fix": fix,
        "regression_evidence": regressions,
        "producer": producer,
    }

    if require_completion:
        reasons: list[str] = []
        if status != "fixed":
            reasons.append("status must be 'fixed'")
        if root_cause is None:
            reasons.append("root_cause is required")
        elif root_cause["confidence"] != "confirmed":
            reasons.append("root_cause.confidence must be 'confirmed'")
        if not any(item["status"] == "supported" for item in hypotheses):
            reasons.append("at least one hypothesis must be supported")
        if not any(item["conclusion"] == "supports" for item in experiments):
            reasons.append("at least one experiment must support a hypothesis")
        if fix is None:
            reasons.append("fix is required")
        if not regressions:
            reasons.append("regression_evidence is required")
        elif any(item["status"] != "passed" for item in regressions):
            reasons.append("all regression evidence must pass")
        if reasons:
            raise _fail(
                "SDAI-DEBUG-004",
                "debug record is not completion-ready: " + "; ".join(reasons),
            )
    return normalized


@dataclass(frozen=True)
class PersistedDebugEvidence:
    record: dict[str, object]
    record_sha256: str
    binding: HashBinding
    completion_ready: bool


def register_debugger_task(
    ledger: ExecutionLedger,
    task_id: str,
    *,
    title: str | None = None,
) -> None:
    payload: dict[str, object] = {
        "semantic_role": DEBUGGER_ROLE,
        "required_completion_evidence": [DEBUG_RECORD_API_VERSION],
    }
    if title is not None:
        payload["title"] = _string(title, label="title")
    ledger.append_event("task.registered", task_id=task_id, payload=payload)


def persist_debug_record(
    ledger: ExecutionLedger,
    task_id: str,
    value: object,
    *,
    require_completion: bool = False,
) -> PersistedDebugEvidence:
    state = ledger.reconstruct().task_map().get(task_id)
    if state is None:
        raise _fail("SDAI-DEBUG-005", f"task '{task_id}' is not registered")
    if state.status != "started":
        raise _fail(
            "SDAI-DEBUG-005",
            f"debug evidence requires started task '{task_id}', current status is {state.status}",
        )
    record = validate_debug_record(
        value,
        feature_id=ledger.manifest.feature_id,
        run_id=ledger.manifest.run_id,
        task_id=task_id,
        require_completion=require_completion,
    )
    completion_ready = True
    if not require_completion:
        try:
            validate_debug_record(
                record,
                feature_id=ledger.manifest.feature_id,
                run_id=ledger.manifest.run_id,
                task_id=task_id,
                require_completion=True,
            )
        except DebugRecordError:
            completion_ready = False
    record_digest = debug_record_sha256(record)
    binding = ledger.write_task_record(
        task_id,
        "evidence",
        {
            "evidence_contract": DEBUG_RECORD_API_VERSION,
            "semantic_role": DEBUGGER_ROLE,
            "record_sha256": record_digest,
            "debug_record": record,
        },
    )
    ledger.append_event(
        "task.evidence",
        task_id=task_id,
        bindings=(binding,),
        payload={
            "evidence_contract": DEBUG_RECORD_API_VERSION,
            "semantic_role": DEBUGGER_ROLE,
            "record_sha256": record_digest,
            "completion_ready": completion_ready,
        },
    )
    return PersistedDebugEvidence(record, record_digest, binding, completion_ready)


def complete_debugger_task(
    ledger: ExecutionLedger,
    task_id: str,
    git_commit: str,
    value: object,
    *,
    artifact_bindings: tuple[HashBinding, ...] = (),
) -> PersistedDebugEvidence:
    evidence = persist_debug_record(
        ledger,
        task_id,
        value,
        require_completion=True,
    )
    try:
        ledger.append_event(
            "task.completed",
            task_id=task_id,
            git_commit=git_commit,
            bindings=(*artifact_bindings, evidence.binding),
            payload={
                "semantic_role": DEBUGGER_ROLE,
                "debug_record_sha256": evidence.record_sha256,
            },
        )
    except ExecutionLedgerError:
        raise
    return evidence
