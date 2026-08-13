from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Mapping

from sdai.completion_policy import CompletionDimension
from sdai.completion_report import CompletionFinding
from sdai.completion_review_checks import current_ledger_attempt
from sdai.execution_ledger import ExecutionLedger
from sdai.isolated_execution import validate_isolated_context_current
from sdai.isolated_tasks import (
    IsolatedStage,
    IsolatedStageStatus,
    latest_stage_result,
    load_persisted_contract,
    task_review_chain,
)
from sdai.verify_engine import verify_feature


_FINAL_TASK_ID = "FINAL-CHANGE-REVIEW"
_ATTEMPT = re.compile(r"^attempt-([1-9][0-9]*)$")


def _canonical_hash(value: Mapping[str, object]) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + sha256(payload).hexdigest()


def latest_final_review_attempt(root: Path, feature_id: str) -> int | None:
    base = root / ".sdai" / "isolated" / feature_id / _FINAL_TASK_ID
    if not base.exists():
        return None
    if base.is_symlink() or not base.is_dir():
        raise RuntimeError("SDAI-COMPLETE-CHANGE-001: final review attempt root is unsafe")
    attempts: list[int] = []
    for child in base.iterdir():
        match = _ATTEMPT.fullmatch(child.name)
        if match is None:
            continue
        if child.is_symlink() or not child.is_dir():
            raise RuntimeError("SDAI-COMPLETE-CHANGE-001: final review attempt path is unsafe")
        attempts.append(int(match.group(1)))
    return max(attempts) if attempts else None


def _current_task_aggregate(
    root: Path,
    ledger: ExecutionLedger,
    *,
    head: str,
) -> tuple[str | None, tuple[str, ...], str | None, str | None]:
    state = ledger.reconstruct()
    if not state.tasks:
        return None, (), None, "final review has no current ledger task set"

    aggregate: list[dict[str, object]] = []
    predecessor_ids: list[str] = []
    first_worker: str | None = None
    for task_state in sorted(state.tasks, key=lambda item: item.task_id):
        try:
            attempt = current_ledger_attempt(ledger, task_state.task_id)
        except Exception as exc:
            return None, (), None, f"cannot resolve current attempt for {task_state.task_id}: {exc}"
        chain = task_review_chain(root, ledger.manifest.feature_id, task_state.task_id, attempt)
        by_stage = {item.invocation.stage: item for item in chain}
        worker = by_stage.get(IsolatedStage.IMPLEMENT)
        spec = by_stage.get(IsolatedStage.SPEC_COMPLIANCE_REVIEW)
        quality = by_stage.get(IsolatedStage.CODE_QUALITY_REVIEW)
        if worker is None or spec is None or quality is None:
            return None, (), None, f"current task review chain is incomplete for {task_state.task_id} attempt {attempt}"
        if any(item.status is not IsolatedStageStatus.PASSED for item in (worker, spec, quality)):
            return None, (), None, f"current task review chain is not passing for {task_state.task_id} attempt {attempt}"
        if any(item.git_commit != head for item in (worker, spec, quality)):
            return None, (), None, f"current task review chain is stale for {task_state.task_id} attempt {attempt}"
        if first_worker is None:
            first_worker = worker.invocation.invocation_id
        predecessor_ids.extend([spec.invocation.invocation_id, quality.invocation.invocation_id])
        aggregate.append(
            {
                "task_id": task_state.task_id,
                "worker_result_sha256": worker.sha256,
                "spec_review_result_sha256": spec.sha256,
                "code_review_result_sha256": quality.sha256,
            }
        )
    return _canonical_hash({"accepted_tasks": aggregate}), tuple(predecessor_ids), first_worker, None


def final_review_finding(
    root: Path,
    ledger: ExecutionLedger,
    *,
    head: str,
    final_attempt: int | None = None,
) -> CompletionFinding:
    feature_id = ledger.manifest.feature_id
    latest = latest_final_review_attempt(root, feature_id)
    if latest is None:
        return CompletionFinding(CompletionDimension.FINAL_REVIEW, "missing", "final whole-change review result is missing")
    if final_attempt is not None and final_attempt != latest:
        return CompletionFinding(
            CompletionDimension.FINAL_REVIEW,
            "wrong-attempt",
            f"requested final review attempt {final_attempt} is superseded by attempt {latest}",
        )
    attempt = latest
    result = latest_stage_result(root, feature_id, _FINAL_TASK_ID, attempt, IsolatedStage.FINAL_CHANGE_REVIEW)
    if result is None:
        return CompletionFinding(CompletionDimension.FINAL_REVIEW, "missing", f"final review attempt {attempt} has no result")
    source = (
        root
        / ".sdai"
        / "isolated"
        / feature_id
        / _FINAL_TASK_ID
        / f"attempt-{attempt}"
        / IsolatedStage.FINAL_CHANGE_REVIEW.value
        / f"{result.invocation.invocation_id}.result.json"
    ).relative_to(root).as_posix()
    contract = load_persisted_contract(root, feature_id, _FINAL_TASK_ID, attempt, IsolatedStage.FINAL_CHANGE_REVIEW)
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

    baseline_item = next((item for item in contract.context if item.source.endswith("final-baseline.txt")), None)
    if baseline_item is None:
        return CompletionFinding(CompletionDimension.FINAL_REVIEW, "blocked", "final review is missing its baseline binding", source)
    task_hash, predecessors, first_worker, error = _current_task_aggregate(root, ledger, head=head)
    if error is not None or task_hash is None or first_worker is None:
        return CompletionFinding(CompletionDimension.FINAL_REVIEW, "blocked", error or "current task aggregate is unavailable", source)
    baseline = baseline_item.text.strip().casefold()
    state = ledger.reconstruct()
    aggregate: list[dict[str, object]] = []
    for task_state in sorted(state.tasks, key=lambda item: item.task_id):
        attempt_for_task = current_ledger_attempt(ledger, task_state.task_id)
        chain = task_review_chain(root, feature_id, task_state.task_id, attempt_for_task)
        by_stage = {item.invocation.stage: item for item in chain}
        aggregate.append(
            {
                "task_id": task_state.task_id,
                "worker_result_sha256": by_stage[IsolatedStage.IMPLEMENT].sha256,
                "spec_review_result_sha256": by_stage[IsolatedStage.SPEC_COMPLIANCE_REVIEW].sha256,
                "code_review_result_sha256": by_stage[IsolatedStage.CODE_QUALITY_REVIEW].sha256,
            }
        )
    expected = _canonical_hash({"accepted_tasks": aggregate, "baseline_commit": baseline, "head": head})
    if contract.remediation_task_sha256 != expected:
        return CompletionFinding(CompletionDimension.FINAL_REVIEW, "wrong-subject", "final review does not cover the current ledger task/attempt set", source)
    if tuple(contract.predecessor_invocation_ids) != predecessors or contract.worker_invocation_id != first_worker:
        return CompletionFinding(CompletionDimension.FINAL_REVIEW, "wrong-subject", "final review predecessor bindings do not match the current task review chains", source)
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


__all__ = ["final_review_finding", "latest_final_review_attempt", "verification_finding"]
