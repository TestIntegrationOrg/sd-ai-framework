from __future__ import annotations

from hashlib import sha256
from pathlib import Path, PurePosixPath

from sdai.isolated_review import build_independent_review_contract
from sdai.isolated_tasks import (
    IsolatedStage,
    IsolatedStageResult,
    IsolatedTaskContract,
    IsolatedTaskError,
)
from sdai.convergence import RemediationTask
from sdai.path_safety import ensure_within_project


def _digest(content: bytes) -> str:
    return "sha256:" + sha256(content).hexdigest()


def materialize_generated_context(
    project_root: Path,
    contract: IsolatedTaskContract,
) -> tuple[Path, ...]:
    """Persist generated `.sdai/isolated/**` context bytes exactly as contracted."""

    root = project_root.resolve()
    paths: list[Path] = []
    prefix = f".sdai/isolated/{contract.feature_id}/"
    for item in contract.context:
        if not item.source.startswith(prefix):
            continue
        path = ensure_within_project(
            root,
            root.joinpath(*PurePosixPath(item.source).parts),
            label="generated isolated context path",
        )
        if path.is_symlink():
            raise IsolatedTaskError(
                f"SDAI-ISOLATED-019: generated context path is a symlink: {item.source}"
            )
        content = item.text.encode("utf-8")
        if _digest(content) != item.source_sha256:
            raise IsolatedTaskError(
                f"SDAI-ISOLATED-019: generated context hash mismatch in contract: {item.source}"
            )
        if path.exists():
            if not path.is_file() or path.read_bytes() != content:
                raise IsolatedTaskError(
                    f"SDAI-ISOLATED-019: generated context already exists with different bytes: {item.source}"
                )
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        paths.append(path)
    return tuple(paths)


def prepare_independent_review_contract(
    project_root: Path,
    task: RemediationTask,
    implementation: IsolatedStageResult,
    stage: IsolatedStage,
    *,
    attempt: int,
    prior_spec_review: IsolatedStageResult | None = None,
) -> IsolatedTaskContract:
    contract = build_independent_review_contract(
        project_root,
        task,
        implementation,
        stage,
        attempt=attempt,
        prior_spec_review=prior_spec_review,
    )
    materialize_generated_context(project_root, contract)
    return contract


__all__ = [
    "materialize_generated_context",
    "prepare_independent_review_contract",
]
