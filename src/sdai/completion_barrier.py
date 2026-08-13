from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
import tempfile
from typing import Mapping

from sdai.completion_change_checks import final_review_finding, verification_finding
from sdai.completion_policy import CompletionDimension, CompletionStage, required_dimensions
from sdai.completion_report import COMPLETION_BARRIER_API_VERSION, CompletionBarrierReport, CompletionFinding
from sdai.completion_review_checks import code_review_finding, current_ledger_attempt, git_head, review_finding
from sdai.completion_trace_checks import typed_evidence_finding
from sdai.convergence import RemediationTask
from sdai.execution_ledger import ExecutionLedger, HashBinding, LedgerEvent
from sdai.isolated_tasks import IsolatedStage, latest_stage_result
from sdai.path_safety import PathSafetyError, ensure_within_project


class CompletionBarrierError(RuntimeError):
    pass


_CONTRACTS: Mapping[CompletionDimension, str] = {
    CompletionDimension.SPEC_REVIEW: "sdai.completion/spec-review/v1",
    CompletionDimension.CODE_QUALITY_REVIEW: "sdai.completion/code-quality-review/v1",
    CompletionDimension.FINAL_REVIEW: "sdai.completion/final-review/v1",
    CompletionDimension.VERIFICATION: "sdai.completion/verification/v1",
    CompletionDimension.TEST: "sdai.completion/test/v1",
    CompletionDimension.QUALITY: "sdai.completion/quality/v1",
    CompletionDimension.SECURITY: "sdai.completion/security/v1",
    CompletionDimension.APPROVAL: "sdai.completion/approval/v1",
}
_TYPED = frozenset(
    {
        CompletionDimension.TEST,
        CompletionDimension.QUALITY,
        CompletionDimension.SECURITY,
        CompletionDimension.APPROVAL,
    }
)


def evaluate_task_completion(
    project_root: Path,
    ledger: ExecutionLedger,
    task: RemediationTask,
    *,
    attempt: int,
    risk: str = "standard",
    typed_evidence_paths: Mapping[str, Path | str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> CompletionBarrierReport:
    root = project_root.resolve()
    if ledger.project_root != root or ledger.manifest.feature_id != task.feature_id:
        raise CompletionBarrierError("SDAI-COMPLETE-001: ledger/task/project identities do not match")
    head = git_head(root)
    required = required_dimensions(root, risk, CompletionStage.TASK, environ=environ)
    current_attempt = current_ledger_attempt(ledger, task.task_id)
    if attempt != current_attempt:
        findings = tuple(
            CompletionFinding(item, "wrong-attempt", f"requested attempt {attempt} is not current ledger attempt {current_attempt}")
            for item in required
        )
        return CompletionBarrierReport(task.feature_id, CompletionStage.TASK, f"task:{task.task_id}", risk, head, attempt, required, findings)

    implementation = latest_stage_result(root, task.feature_id, task.task_id, attempt, IsolatedStage.IMPLEMENT)
    paths = typed_evidence_paths or {}
    findings: list[CompletionFinding] = []
    for dimension in required:
        if dimension is CompletionDimension.SPEC_REVIEW:
            findings.append(
                review_finding(
                    root,
                    task,
                    attempt,
                    IsolatedStage.SPEC_COMPLIANCE_REVIEW,
                    dimension,
                    implementation,
                    head=head,
                )
            )
        elif dimension is CompletionDimension.CODE_QUALITY_REVIEW:
            findings.append(code_review_finding(root, task, attempt, implementation, head=head))
        elif dimension in _TYPED:
            findings.append(
                typed_evidence_finding(
                    root,
                    dimension,
                    paths.get(dimension.value),
                    head=head,
                    expected_subjects={task.subject, f"task:{task.task_id}"},
                )
            )
        else:
            findings.append(CompletionFinding(dimension, "blocked", "dimension is not valid for task completion"))
    return CompletionBarrierReport(
        task.feature_id,
        CompletionStage.TASK,
        f"task:{task.task_id}",
        risk,
        head,
        attempt,
        required,
        tuple(findings),
    )


def evaluate_change_completion(
    project_root: Path,
    ledger: ExecutionLedger,
    *,
    risk: str = "standard",
    final_attempt: int = 1,
    typed_evidence_paths: Mapping[str, Path | str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> CompletionBarrierReport:
    root = project_root.resolve()
    head = git_head(root)
    required = required_dimensions(root, risk, CompletionStage.CHANGE, environ=environ)
    paths = typed_evidence_paths or {}
    state = ledger.reconstruct()
    incomplete = sorted(item.task_id for item in state.tasks if item.status != "completed")
    findings: list[CompletionFinding] = []
    for dimension in required:
        if dimension is CompletionDimension.FINAL_REVIEW:
            finding = final_review_finding(root, ledger.manifest.feature_id, head=head, final_attempt=final_attempt)
            if incomplete and finding.satisfied:
                finding = CompletionFinding(
                    dimension,
                    "blocked",
                    "final review cannot authorize change completion while ledger tasks are incomplete",
                    finding.source,
                )
            findings.append(finding)
        elif dimension is CompletionDimension.VERIFICATION:
            findings.append(verification_finding(root, ledger, head=head, risk=risk, environ=environ))
        elif dimension in _TYPED:
            findings.append(
                typed_evidence_finding(
                    root,
                    dimension,
                    paths.get(dimension.value),
                    head=head,
                    expected_subjects={f"feature:{ledger.manifest.feature_id}"},
                )
            )
        else:
            findings.append(CompletionFinding(dimension, "blocked", "dimension is not valid for change completion"))
    return CompletionBarrierReport(
        ledger.manifest.feature_id,
        CompletionStage.CHANGE,
        f"feature:{ledger.manifest.feature_id}",
        risk,
        head,
        None,
        required,
        tuple(findings),
    )


def _report_path(root: Path, report: CompletionBarrierReport) -> Path:
    subject = report.subject.replace(":", "-").replace("/", "-")
    attempt = f"attempt-{report.attempt}" if report.attempt is not None else "change"
    raw = root / ".sdai" / "completion" / report.feature_id / subject / attempt / "barrier.json"
    try:
        return ensure_within_project(root, raw, label="completion barrier report")
    except PathSafetyError as exc:
        raise CompletionBarrierError("SDAI-COMPLETE-002: report path escapes project root") from exc


def _persist_report(root: Path, report: CompletionBarrierReport) -> Path:
    path = _report_path(root, report)
    content = report.to_json().encode("utf-8") + b"\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.is_symlink():
        raise CompletionBarrierError("SDAI-COMPLETE-002: report path must not be a symlink")
    if path.exists() and path.read_bytes() == content:
        return path
    handle = tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False)
    temp = Path(handle.name)
    try:
        with handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)
    return path


def _bindings_for_report(root: Path, ledger: ExecutionLedger, report: CompletionBarrierReport) -> tuple[HashBinding, ...]:
    bindings: dict[tuple[str, str], HashBinding] = {}
    report_binding = ledger.binding_for_file(_persist_report(root, report), kind="evidence")
    bindings[(report_binding.kind, report_binding.source)] = report_binding
    for finding in report.findings:
        if not finding.satisfied or finding.source is None:
            continue
        source_path = root.joinpath(*PurePosixPath(finding.source).parts)
        binding = ledger.binding_for_file(source_path, kind="evidence")
        bindings[(binding.kind, binding.source)] = binding
    return tuple(bindings[key] for key in sorted(bindings))


def _append_task_evidence(
    ledger: ExecutionLedger,
    task_id: str,
    attempt: int,
    report: CompletionBarrierReport,
    bindings: tuple[HashBinding, ...],
) -> None:
    existing = ledger.load_events()
    for dimension in report.required:
        contract = _CONTRACTS[dimension]
        if any(
            event.task_id == task_id
            and event.kind == "task.evidence"
            and event.payload.get("evidence_contract") == contract
            and event.payload.get("completion_barrier_sha256") == report.sha256
            for event in existing
        ):
            continue
        ledger.append_event(
            "task.evidence",
            task_id=task_id,
            git_commit=report.git_commit,
            bindings=bindings,
            payload={
                "evidence_contract": contract,
                "completion_ready": True,
                "completion_barrier_sha256": report.sha256,
                "attempt": attempt,
                "risk": report.risk,
                "dimension": dimension.value,
            },
        )


def complete_isolated_task(
    project_root: Path,
    ledger: ExecutionLedger,
    task: RemediationTask,
    *,
    attempt: int,
    risk: str = "standard",
    typed_evidence_paths: Mapping[str, Path | str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> LedgerEvent:
    report = evaluate_task_completion(
        project_root,
        ledger,
        task,
        attempt=attempt,
        risk=risk,
        typed_evidence_paths=typed_evidence_paths,
        environ=environ,
    )
    if not report.passed:
        detail = "; ".join(f"{item.dimension.value}:{item.status}:{item.reason}" for item in report.findings if not item.satisfied)
        raise CompletionBarrierError(f"SDAI-COMPLETE-003: task completion blocked: {detail}")
    root = project_root.resolve()
    bindings = _bindings_for_report(root, ledger, report)
    _append_task_evidence(ledger, task.task_id, attempt, report, bindings)
    return ledger.append_event(
        "task.completed",
        task_id=task.task_id,
        git_commit=report.git_commit,
        bindings=bindings,
        payload={"completion_barrier_sha256": report.sha256, "attempt": attempt, "risk": report.risk},
    )


def complete_isolated_run(
    project_root: Path,
    ledger: ExecutionLedger,
    *,
    risk: str = "standard",
    final_attempt: int = 1,
    typed_evidence_paths: Mapping[str, Path | str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> LedgerEvent:
    report = evaluate_change_completion(
        project_root,
        ledger,
        risk=risk,
        final_attempt=final_attempt,
        typed_evidence_paths=typed_evidence_paths,
        environ=environ,
    )
    if not report.passed:
        detail = "; ".join(f"{item.dimension.value}:{item.status}:{item.reason}" for item in report.findings if not item.satisfied)
        raise CompletionBarrierError(f"SDAI-COMPLETE-004: change completion blocked: {detail}")
    root = project_root.resolve()
    bindings = _bindings_for_report(root, ledger, report)
    return ledger.append_event(
        "run.completed",
        git_commit=report.git_commit,
        bindings=bindings,
        payload={"completion_barrier_sha256": report.sha256, "risk": report.risk},
    )


__all__ = [
    "CompletionBarrierError",
    "complete_isolated_run",
    "complete_isolated_task",
    "evaluate_change_completion",
    "evaluate_task_completion",
]
