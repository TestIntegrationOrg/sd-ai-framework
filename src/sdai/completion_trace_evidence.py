from __future__ import annotations

from pathlib import Path

from sdai.completion_policy import (
    APPROVAL_CONTRACT,
    QUALITY_CONTRACT,
    SECURITY_CONTRACT,
    TEST_CONTRACT,
    validate_completion_contract,
)
from sdai.execution_ledger import ExecutionLedger, HashBinding
from sdai.trace_evidence import EvidenceKind, EvidenceStatus, load_trace_evidence


class CompletionTraceEvidenceError(RuntimeError):
    pass


_KIND_BY_CONTRACT = {
    TEST_CONTRACT: EvidenceKind.TEST,
    QUALITY_CONTRACT: EvidenceKind.QUALITY,
    SECURITY_CONTRACT: EvidenceKind.SECURITY,
    APPROVAL_CONTRACT: EvidenceKind.APPROVAL,
}


def evidence_binding(
    ledger: ExecutionLedger,
    path: Path,
) -> HashBinding:
    candidate = path if path.is_absolute() else ledger.project_root / path
    return ledger.binding_for_file(candidate, kind="evidence")


def load_completion_trace_evidence(
    ledger: ExecutionLedger,
    contract: str,
    path: Path,
    *,
    expected_subject: str,
):
    selected = validate_completion_contract(contract)
    expected_kind = _KIND_BY_CONTRACT.get(selected)
    if expected_kind is None:
        raise CompletionTraceEvidenceError(
            f"contract {selected!r} is not backed by typed trace evidence"
        )
    candidate = path if path.is_absolute() else ledger.project_root / path
    evidence = load_trace_evidence(ledger.project_root, candidate)
    if evidence.kind is not expected_kind:
        raise CompletionTraceEvidenceError(
            f"contract {selected!r} requires {expected_kind.value} evidence"
        )
    if evidence.status is not EvidenceStatus.PASSED:
        raise CompletionTraceEvidenceError(
            "only passing typed evidence is completion-ready"
        )
    if evidence.subject != expected_subject:
        raise CompletionTraceEvidenceError(
            "typed evidence subject does not match completion subject"
        )
    return selected, evidence
