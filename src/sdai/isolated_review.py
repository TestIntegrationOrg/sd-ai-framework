from __future__ import annotations

from hashlib import sha256
from pathlib import Path

from sdai.agent_platform.models import Capability, ExecutionMode
from sdai.convergence import RemediationTask
from sdai.isolated_tasks import (
    IsolatedContextSlice,
    IsolatedStage,
    IsolatedStageResult,
    IsolatedStageStatus,
    IsolatedTaskContract,
    IsolatedTaskError,
    context_from_remediation,
    latest_stage_result,
    load_persisted_contract,
    persist_contract,
)
from sdai.isolated_workspace import (
    IsolatedWorkspaceError,
    current_head,
    render_workspace_snapshot,
)


_MAX_REVIEW_CONTEXT_CHARS = 60_000


def _digest(content: bytes) -> str:
    return "sha256:" + sha256(content).hexdigest()


def _generated_slice(source: str, text: str) -> IsolatedContextSlice:
    if len(text) > _MAX_REVIEW_CONTEXT_CHARS:
        raise IsolatedTaskError(
            f"SDAI-ISOLATED-018: generated review context is too large: {source}"
        )
    return IsolatedContextSlice(
        source=source,
        line_start=1,
        line_end=max(1, len(text.splitlines())),
        source_sha256=_digest(text.encode("utf-8")),
        text=text,
    )


def _require_current_spec_review(
    root: Path,
    task: RemediationTask,
    implementation: IsolatedStageResult,
    attempt: int,
    prior_spec_review: IsolatedStageResult | None,
) -> IsolatedStageResult:
    if (
        prior_spec_review is None
        or prior_spec_review.invocation.stage is not IsolatedStage.SPEC_COMPLIANCE_REVIEW
        or prior_spec_review.status is not IsolatedStageStatus.PASSED
    ):
        raise IsolatedTaskError(
            "SDAI-ISOLATED-018: code-quality review requires passing independent spec-compliance review"
        )
    spec_contract = load_persisted_contract(
        root,
        task.feature_id,
        task.task_id,
        attempt,
        IsolatedStage.SPEC_COMPLIANCE_REVIEW,
    )
    if spec_contract is None:
        raise IsolatedTaskError(
            "SDAI-ISOLATED-018: persisted spec-compliance contract is required before code-quality review"
        )
    if (
        prior_spec_review.invocation.contract_sha256 != spec_contract.sha256
        or spec_contract.worker_invocation_id != implementation.invocation.invocation_id
        or spec_contract.remediation_task_sha256 != task.sha256
    ):
        raise IsolatedTaskError(
            "SDAI-ISOLATED-018: spec-compliance review does not belong to the current task/attempt/worker"
        )
    persisted_result = latest_stage_result(
        root,
        task.feature_id,
        task.task_id,
        attempt,
        IsolatedStage.SPEC_COMPLIANCE_REVIEW,
    )
    if persisted_result is None or persisted_result.sha256 != prior_spec_review.sha256:
        raise IsolatedTaskError(
            "SDAI-ISOLATED-018: spec-compliance review is not the persisted current review result"
        )
    return prior_spec_review


def build_independent_review_contract(
    project_root: Path,
    task: RemediationTask,
    implementation: IsolatedStageResult,
    stage: IsolatedStage,
    *,
    attempt: int,
    prior_spec_review: IsolatedStageResult | None = None,
) -> IsolatedTaskContract:
    """Build a fresh review contract from bounded durable worker evidence.

    The reviewer receives only:
    - the original remediation provenance windows;
    - the worker's recorded output;
    - a deterministic tracked + untracked workspace snapshot relative to the
      persisted implementation contract;
    - for code-quality review, the persisted spec-review result for this exact
      task, attempt, and worker invocation.
    """

    if stage not in {
        IsolatedStage.SPEC_COMPLIANCE_REVIEW,
        IsolatedStage.CODE_QUALITY_REVIEW,
    }:
        raise IsolatedTaskError(
            "SDAI-ISOLATED-018: independent task review must be spec-compliance or code-quality"
        )
    if implementation.invocation.stage is not IsolatedStage.IMPLEMENT:
        raise IsolatedTaskError("SDAI-ISOLATED-018: review requires implementation result")
    if implementation.status is not IsolatedStageStatus.PASSED:
        raise IsolatedTaskError(
            "SDAI-ISOLATED-018: review requires implementation stage recorded as passed"
        )
    if implementation.invocation.semantic_agent == "code-reviewer":
        raise IsolatedTaskError(
            "SDAI-ISOLATED-018: worker semantic agent cannot satisfy independent reviewer role"
        )
    if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
        raise IsolatedTaskError("SDAI-ISOLATED-018: review attempt must be positive")

    root = project_root.resolve()
    implementation_contract = load_persisted_contract(
        root,
        task.feature_id,
        task.task_id,
        attempt,
        IsolatedStage.IMPLEMENT,
    )
    if implementation_contract is None:
        raise IsolatedTaskError(
            "SDAI-ISOLATED-018: persisted implementation contract is required for independent review"
        )
    if (
        implementation_contract.sha256 != implementation.invocation.contract_sha256
        or implementation_contract.remediation_task_sha256 != task.sha256
    ):
        raise IsolatedTaskError(
            "SDAI-ISOLATED-018: implementation result does not match persisted current task contract"
        )

    existing = load_persisted_contract(root, task.feature_id, task.task_id, attempt, stage)
    if existing is not None:
        return existing

    current_spec_review: IsolatedStageResult | None = None
    if stage is IsolatedStage.CODE_QUALITY_REVIEW:
        current_spec_review = _require_current_spec_review(
            root,
            task,
            implementation,
            attempt,
            prior_spec_review,
        )

    try:
        snapshot = render_workspace_snapshot(root, implementation_contract.git_commit)
        head = current_head(root)
    except IsolatedWorkspaceError as exc:
        raise IsolatedTaskError(f"SDAI-ISOLATED-018: unable to capture review workspace: {exc}") from exc

    worker_source = (
        f".sdai/isolated/{task.feature_id}/{task.task_id}/attempt-{attempt}/"
        "implement/worker-output.txt"
    )
    diff_source = (
        f".sdai/isolated/{task.feature_id}/{task.task_id}/attempt-{attempt}/"
        f"{stage.value}/workspace.diff"
    )
    context = [
        *context_from_remediation(root, task),
        _generated_slice(worker_source, implementation.output),
        _generated_slice(diff_source, snapshot),
    ]
    predecessors: tuple[str, ...] = ()
    if current_spec_review is not None:
        spec_source = (
            f".sdai/isolated/{task.feature_id}/{task.task_id}/attempt-{attempt}/"
            "spec-compliance-review/reviewer-output.txt"
        )
        context.append(_generated_slice(spec_source, current_spec_review.output))
        predecessors = (current_spec_review.invocation.invocation_id,)

    if sum(len(item.text) for item in context) > _MAX_REVIEW_CONTEXT_CHARS:
        raise IsolatedTaskError(
            "SDAI-ISOLATED-018: combined independent review context exceeds bounded limit"
        )

    contract = IsolatedTaskContract(
        feature_id=task.feature_id,
        task_id=task.task_id,
        remediation_task_sha256=task.sha256,
        round_id=task.round_id,
        attempt=attempt,
        stage=stage,
        git_commit=head,
        dispatch_id=f"{implementation.invocation.invocation_id}:{stage.value}",
        semantic_agent="code-reviewer",
        capability=Capability.REVIEW,
        mode=ExecutionMode.ADVISORY,
        summary=task.summary,
        allowed_roots=task.allowed_roots,
        forbidden_roots=task.forbidden_roots,
        context=tuple(context),
        predecessor_invocation_ids=predecessors,
        worker_invocation_id=implementation.invocation.invocation_id,
    )
    persist_contract(root, contract)
    return contract


__all__ = ["build_independent_review_contract"]
