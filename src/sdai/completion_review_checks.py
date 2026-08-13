from __future__ import annotations
from pathlib import Path
import subprocess
from sdai.completion_policy import CompletionDimension
from sdai.completion_report import CompletionFinding
from sdai.convergence import RemediationTask
from sdai.execution_ledger import ExecutionLedger
from sdai.isolated_execution import validate_isolated_context_current
from sdai.isolated_tasks import IsolatedStage, IsolatedStageResult, IsolatedStageStatus, latest_stage_result, load_persisted_contract

class CompletionReviewCheckError(RuntimeError): pass

def git_head(root: Path) -> str:
    c=subprocess.run(["git","rev-parse","--verify","HEAD"],cwd=root,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding="utf-8",errors="strict",shell=False,check=False)
    if c.returncode:
        raise CompletionReviewCheckError(f"SDAI-COMPLETE-REVIEW-001: unable to resolve Git HEAD: {c.stderr.strip() or c.stdout.strip()}")
    value=c.stdout.strip().casefold()
    if len(value) not in {40,64} or any(ch not in "0123456789abcdef" for ch in value):
        raise CompletionReviewCheckError("SDAI-COMPLETE-REVIEW-001: invalid Git HEAD identity")
    return value

def current_ledger_attempt(ledger: ExecutionLedger, task_id: str) -> int:
    attempt=0; registered=False
    for event in ledger.load_events():
        if event.task_id != task_id: continue
        if event.kind=="task.registered": attempt=1; registered=True
        elif event.kind=="task.reopened": attempt+=1
    if not registered: raise CompletionReviewCheckError(f"SDAI-COMPLETE-REVIEW-002: task {task_id!r} is not registered")
    return attempt

def result_source(root: Path, feature_id: str, task_id: str, attempt: int, result: IsolatedStageResult) -> str:
    return (root/".sdai"/"isolated"/feature_id/task_id/f"attempt-{attempt}"/result.invocation.stage.value/f"{result.invocation.invocation_id}.result.json").relative_to(root).as_posix()

def review_finding(root: Path, task: RemediationTask, attempt: int, stage: IsolatedStage, dimension: CompletionDimension, implementation: IsolatedStageResult|None, *, head: str) -> CompletionFinding:
    result=latest_stage_result(root,task.feature_id,task.task_id,attempt,stage)
    if result is None: return CompletionFinding(dimension,"missing",f"required {stage.value} result is missing")
    source=result_source(root,task.feature_id,task.task_id,attempt,result)
    contract=load_persisted_contract(root,task.feature_id,task.task_id,attempt,stage)
    if contract is None: return CompletionFinding(dimension,"missing",f"persisted {stage.value} contract is missing",source)
    if result.invocation.contract_sha256!=contract.sha256 or result.invocation.stage is not stage: return CompletionFinding(dimension,"wrong-attempt","review result does not match the current persisted contract",source)
    if result.status is not IsolatedStageStatus.PASSED: return CompletionFinding(dimension,"failed",f"review status is {result.status.value}",source)
    if result.git_commit!=head or contract.git_commit!=head: return CompletionFinding(dimension,"stale","review is bound to an older Git commit",source)
    if implementation is None: return CompletionFinding(dimension,"missing","implementation result is missing",source)
    if contract.worker_invocation_id!=implementation.invocation.invocation_id: return CompletionFinding(dimension,"wrong-subject","review is bound to a different implementation worker",source)
    if result.invocation.semantic_agent==implementation.invocation.semantic_agent: return CompletionFinding(dimension,"blocked","implementing worker cannot satisfy independent review",source)
    try: validate_isolated_context_current(root,contract)
    except Exception as exc: return CompletionFinding(dimension,"stale",f"review context is no longer current: {exc}",source)
    return CompletionFinding(dimension,"valid","independent review is passed and current",source)

def code_review_finding(root: Path, task: RemediationTask, attempt: int, implementation: IsolatedStageResult|None, *, head: str) -> CompletionFinding:
    finding=review_finding(root,task,attempt,IsolatedStage.CODE_QUALITY_REVIEW,CompletionDimension.CODE_QUALITY_REVIEW,implementation,head=head)
    if not finding.satisfied: return finding
    spec=latest_stage_result(root,task.feature_id,task.task_id,attempt,IsolatedStage.SPEC_COMPLIANCE_REVIEW)
    contract=load_persisted_contract(root,task.feature_id,task.task_id,attempt,IsolatedStage.CODE_QUALITY_REVIEW)
    if spec is None or contract is None: return CompletionFinding(CompletionDimension.CODE_QUALITY_REVIEW,"missing","review chain is incomplete",finding.source)
    if spec.invocation.invocation_id not in contract.predecessor_invocation_ids: return CompletionFinding(CompletionDimension.CODE_QUALITY_REVIEW,"wrong-subject","code-quality review is not chained to the current spec review",finding.source)
    return finding
