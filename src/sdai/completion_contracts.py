from __future__ import annotations

from pathlib import Path
from typing import Mapping

from sdai.completion_policy import (
    CompletionDimension,
    CompletionStage,
    required_dimensions,
    resolve_completion_risk,
)


COMPLETION_CONTRACTS: Mapping[CompletionDimension, str] = {
    CompletionDimension.SPEC_REVIEW: "sdai.completion/spec-review/v1",
    CompletionDimension.CODE_QUALITY_REVIEW: "sdai.completion/code-quality-review/v1",
    CompletionDimension.FINAL_REVIEW: "sdai.completion/final-review/v1",
    CompletionDimension.VERIFICATION: "sdai.completion/verification/v1",
    CompletionDimension.TEST: "sdai.completion/test/v1",
    CompletionDimension.QUALITY: "sdai.completion/quality/v1",
    CompletionDimension.SECURITY: "sdai.completion/security/v1",
    CompletionDimension.APPROVAL: "sdai.completion/approval/v1",
}


def task_completion_contracts(
    project_root: Path,
    feature_id: str,
    *,
    risk: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> tuple[str, tuple[str, ...]]:
    selected_risk = resolve_completion_risk(project_root, feature_id, risk)
    required = required_dimensions(
        project_root,
        selected_risk,
        CompletionStage.TASK,
        environ=environ,
    )
    return selected_risk, tuple(COMPLETION_CONTRACTS[item] for item in required)


__all__ = ["COMPLETION_CONTRACTS", "task_completion_contracts"]
