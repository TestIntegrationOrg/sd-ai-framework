from __future__ import annotations

from pathlib import Path

from sdai.completion_trace_evidence import (
    CompletionTraceEvidenceError,
    load_completion_trace_evidence,
)
from sdai.execution_ledger import ExecutionLedger
from sdai.trace_freshness import (
    CommitPolicy,
    ProofFreshness,
    evaluate_trace_evidence_freshness,
)


def load_current_completion_trace_evidence(
    ledger: ExecutionLedger,
    contract: str,
    path: Path,
    *,
    expected_subject: str,
):
    selected, evidence = load_completion_trace_evidence(
        ledger,
        contract,
        path,
        expected_subject=expected_subject,
    )
    freshness = evaluate_trace_evidence_freshness(
        ledger.project_root,
        evidence,
        commit_policy=CommitPolicy.EXACT_HEAD,
    )
    if freshness.freshness is not ProofFreshness.VALID:
        raise CompletionTraceEvidenceError(
            f"typed evidence is not current: {freshness.freshness.value}"
        )
    return selected, evidence, freshness


__all__ = ["load_current_completion_trace_evidence"]
