from __future__ import annotations

from pathlib import Path
from typing import Mapping

from sdai.completion_policy import CompletionDimension
from sdai.completion_report import CompletionFinding
from sdai.execution_ledger import ExecutionLedger
from sdai.isolated_execution import validate_isolated_context_current
from sdai.isolated_tasks import IsolatedStage, IsolatedStageStatus, latest_stage_result, load_persisted_contract
from sdai.verify_engine import verify_feature


def final_review_finding(root: Path, feature_id: str, *, head: str, final_attempt: int = 1) -> CompletionFinding:
    task_id = "FINAL-CHANGE-REVIEW"
    result = latest_stage_result(root, feature_id, task_id, final_attempt, IsolatedStage.FINAL_CHANGE_REVIEW)
    if result is None:
        return CompletionFinding(CompletionDimension.FINAL_REVIEW, "missing", "final whole-change review result is missing")
    source = (
        root
        / ".sdai"
        / "isolated"
        / feature_id
        / task_id
        / f"attempt-{final_attempt}"
        / IsolatedStage.FINAL_CHANGE_REVIEW.value
        / f"{result.invocation.invocation_id}.result.json"
    ).relative_to(root).as_posix()
    contract = load_persisted_contract(root, feature_id, task_id, final_attempt, IsolatedStage.FINAL_CHANGE_REVIEW)
    if contract is None or result.invocation.contract_sha256 != contract.sha256:
        return CompletionFinding(CompletionDimension.FINAL_REVIEW, "wrong-attempt", "final review does not match its persisted contract", source)
    if result.status is not IsolatedStageStatus.PASSED:
        return CompletionFinding(CompletionDimension.FINAL_REVIEW, "failed", f"final review status is {result.status.value}", source)
    if result.git_commit != head or contract.git_commit != head:
        return CompletionFinding(CompletionDimension.FINAL_REVIEW, "stale", "final review is bound to an older Git commit", source)
    try:
        validate_isolated_context_current(root, contract)
    except Exception as exc:
        return CompletionFinding(CompletionDimension.FINAL_REVIEW, "stale", f"final review context is no longer current: {exc}", source)
    return CompletionFinding(CompletionDimension.FINAL_REVIEW, "valid", "final whole-change review is passed and current", source)


def verification_finding(
    root: Path,
    ledger: ExecutionLedger,
    *,
    head: str,
    risk: str,
    environ: Mapping[str, str] | None = None,
) -> CompletionFinding:
    try:
        report = verify_feature(root, ledger.manifest.feature_id, risk=risk, environ=environ)
    except Exception as exc:
        return CompletionFinding(CompletionDimension.VERIFICATION, "blocked", f"current verification failed to evaluate: {exc}")
    if report.git_commit != head:
        return CompletionFinding(CompletionDimension.VERIFICATION, "stale", "verification report is not bound to current HEAD")
    if not report.passed:
        return CompletionFinding(CompletionDimension.VERIFICATION, "failed", f"verification outcome is {report.outcome.value}")
    return CompletionFinding(CompletionDimension.VERIFICATION, "valid", "deterministic and semantic verification pass for current HEAD")


__all__ = ["final_review_finding", "verification_finding"]
