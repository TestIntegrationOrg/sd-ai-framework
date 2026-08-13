from __future__ import annotations

from typing import Mapping, Sequence

from sdai.completion_policy import CompletionRisk, CompletionScope
from sdai.completion_policy_layers import resolve_layered_completion_policy
from sdai.execution_ledger import ExecutionLedger, LedgerEvent


class CompletionLedgerError(RuntimeError):
    """Raised when ledger completion requirements are inconsistent."""


def task_registration(
    ledger: ExecutionLedger,
    task_id: str,
) -> LedgerEvent | None:
    return next(
        (
            event
            for event in ledger.load_events()
            if event.kind == "task.registered" and event.task_id == task_id
        ),
        None,
    )


def current_task_attempt(
    ledger: ExecutionLedger,
    task_id: str,
) -> tuple[int, int]:
    attempt = 0
    boundary = 0
    for event in ledger.load_events():
        if event.task_id != task_id:
            continue
        if event.kind == "task.registered":
            attempt = 1
            boundary = event.sequence
        elif event.kind == "task.reopened":
            attempt += 1
            boundary = event.sequence
    if attempt == 0:
        raise CompletionLedgerError(
            f"SDAI-COMPLETION-LEDGER-001: task {task_id!r} is not registered"
        )
    return attempt, boundary


def declared_completion_contracts(
    ledger: ExecutionLedger,
    task_id: str,
) -> tuple[str, ...]:
    event = task_registration(ledger, task_id)
    if event is None:
        return ()
    raw = event.payload.get("required_completion_evidence", [])
    if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
        raise CompletionLedgerError(
            "SDAI-COMPLETION-LEDGER-002: malformed required completion evidence declaration"
        )
    return tuple(sorted(raw))


def register_completion_requirements(
    ledger: ExecutionLedger,
    task_id: str,
    risk: CompletionRisk | str,
    scope: CompletionScope | str,
    *,
    payload: Mapping[str, object] | None = None,
    organization_required: Sequence[str] = (),
    repository_required: Sequence[str] = (),
    user_required: Sequence[str] = (),
    additional_required: Sequence[str] = (),
):
    resolution = resolve_layered_completion_policy(
        risk,
        scope,
        organization_required=organization_required,
        repository_required=repository_required,
        user_required=user_required,
        additional_required=additional_required,
    )
    existing = task_registration(ledger, task_id)
    if existing is not None:
        declared = set(declared_completion_contracts(ledger, task_id))
        missing = sorted(set(resolution.required_contracts) - declared)
        if missing:
            raise CompletionLedgerError(
                "SDAI-COMPLETION-LEDGER-003: existing registration weakens completion policy: "
                + ", ".join(missing)
            )
        return resolution

    event_payload = dict(payload or {})
    if resolution.required_contracts:
        event_payload["required_completion_evidence"] = list(
            resolution.required_contracts
        )
    ledger.append_event(
        "task.registered",
        task_id=task_id,
        payload=event_payload,
    )
    return resolution


__all__ = [
    "CompletionLedgerError",
    "current_task_attempt",
    "declared_completion_contracts",
    "register_completion_requirements",
    "task_registration",
]
