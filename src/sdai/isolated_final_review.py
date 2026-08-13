from __future__ import annotations

from pathlib import Path
from typing import Mapping, Sequence

from sdai.isolated_context_state import materialize_generated_context
from sdai.isolated_tasks import (
    IsolatedStageResult,
    IsolatedTaskContract,
    build_final_change_review_contract,
)


def prepare_final_change_review_contract(
    project_root: Path,
    feature_id: str,
    task_chains: Mapping[str, Sequence[IsolatedStageResult]],
    *,
    baseline_commit: str,
    attempt: int = 1,
) -> IsolatedTaskContract:
    contract = build_final_change_review_contract(
        project_root,
        feature_id,
        task_chains,
        baseline_commit=baseline_commit,
        attempt=attempt,
    )
    materialize_generated_context(project_root, contract)
    return contract


__all__ = ["prepare_final_change_review_contract"]
