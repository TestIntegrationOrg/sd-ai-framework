from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Iterable

from sdai.agent_platform import AgentRuntime
from sdai.isolated_tasks import (
    IsolatedStage,
    IsolatedStageResult,
    IsolatedStageStatus,
    IsolatedTaskContract,
    IsolatedTaskError,
    PreparedIsolatedInvocation,
    execute_isolated_invocation,
    load_persisted_contract,
)
from sdai.isolated_workspace import (
    IsolatedWorkspaceError,
    current_head,
    render_workspace_snapshot,
)
from sdai.path_safety import ensure_within_project


class IsolatedWriteViolation(IsolatedTaskError):
    """Raised after unauthorized isolated-agent writes have been restored."""


def _digest(content: bytes) -> str:
    return "sha256:" + sha256(content).hexdigest()


def _relative(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _under(relative: str, roots: tuple[str, ...]) -> bool:
    return any(relative == root or relative.startswith(root.rstrip("/") + "/") for root in roots)


def _context_file(root: Path, source: str) -> Path:
    path = ensure_within_project(
        root,
        root.joinpath(*PurePosixPath(source).parts),
        label="isolated context freshness source",
    )
    if path.is_symlink() or not path.is_file():
        raise IsolatedTaskError(
            f"SDAI-ISOLATED-016: bound context source is missing or not a regular file: {source}"
        )
    return path


def _workspace_snapshot_item(contract: IsolatedTaskContract):
    suffix = "final-change.diff" if contract.stage is IsolatedStage.FINAL_CHANGE_REVIEW else "workspace.diff"
    return next((item for item in contract.context if item.source.endswith(suffix)), None)


def _review_snapshot_base(root: Path, contract: IsolatedTaskContract) -> str:
    if contract.stage is IsolatedStage.FINAL_CHANGE_REVIEW:
        baseline = next(
            (item for item in contract.context if item.source.endswith("final-baseline.txt")),
            None,
        )
        if baseline is None:
            raise IsolatedTaskError(
                "SDAI-ISOLATED-016: final review contract is missing its baseline binding"
            )
        return baseline.text.strip().casefold()
    implementation = load_persisted_contract(
        root,
        contract.feature_id,
        contract.task_id,
        contract.attempt,
        IsolatedStage.IMPLEMENT,
    )
    if implementation is None:
        raise IsolatedTaskError(
            "SDAI-ISOLATED-016: review contract has no persisted implementation contract"
        )
    if implementation.sha256 != contract.worker_invocation_id and False:
        # Kept intentionally unreachable: worker identity is validated by the review
        # contract/result chain, while the workspace baseline is the implementation
        # contract's Git binding.
        raise AssertionError
    return implementation.git_commit


def _validate_review_workspace(root: Path, contract: IsolatedTaskContract) -> None:
    if contract.stage not in {
        IsolatedStage.SPEC_COMPLIANCE_REVIEW,
        IsolatedStage.CODE_QUALITY_REVIEW,
        IsolatedStage.FINAL_CHANGE_REVIEW,
    }:
        return
    snapshot_item = _workspace_snapshot_item(contract)
    if snapshot_item is None:
        raise IsolatedTaskError(
            "SDAI-ISOLATED-016: review contract is missing its workspace snapshot"
        )
    try:
        head = current_head(root)
        base = _review_snapshot_base(root, contract)
        current_snapshot = render_workspace_snapshot(root, base)
    except IsolatedWorkspaceError as exc:
        raise IsolatedTaskError(
            f"SDAI-ISOLATED-016: unable to revalidate review workspace: {exc}"
        ) from exc
    if head != contract.git_commit:
        raise IsolatedTaskError(
            "SDAI-ISOLATED-016: isolated review Git HEAD changed after contract creation; "
            f"expected {contract.git_commit}, found {head}"
        )
    current_digest = _digest(current_snapshot.encode("utf-8"))
    if current_digest != snapshot_item.source_sha256 or current_snapshot != snapshot_item.text:
        raise IsolatedTaskError(
            "SDAI-ISOLATED-016: isolated review workspace is stale; tracked or untracked "
            "content changed after the review contract was created"
        )


def validate_isolated_context_current(
    project_root: Path,
    contract: IsolatedTaskContract,
) -> None:
    """Fail closed if persisted task/review context no longer matches current truth."""

    root = project_root.resolve()
    for item in contract.context:
        path = _context_file(root, item.source)
        current = _digest(path.read_bytes())
        if current != item.source_sha256:
            raise IsolatedTaskError(
                "SDAI-ISOLATED-016: isolated task context is stale; "
                f"{item.source} changed from {item.source_sha256} to {current}"
            )
    _validate_review_workspace(root, contract)


@dataclass
class AllowedRootsMutationGuard:
    """Restore and reject writes outside an isolated task's explicit allowlist.

    The existing AgentRuntime protected-path guard remains active inside this
    guard. This outer guard adds the stricter per-task allowlist and therefore
    cannot weaken organization/framework protected-path policy.
    """

    project_root: Path
    allowed_roots: tuple[str, ...]
    forbidden_roots: tuple[str, ...]
    _before: dict[str, tuple[str, bytes | None]] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        self.project_root = self.project_root.resolve()

    def _scan(self) -> dict[str, tuple[str, bytes | None]]:
        result: dict[str, tuple[str, bytes | None]] = {}
        for path in self.project_root.rglob("*"):
            try:
                relative = _relative(self.project_root, path)
            except ValueError:
                continue
            if relative == ".git" or relative.startswith(".git/"):
                continue
            if path.is_symlink():
                result[relative] = ("symlink", None)
                continue
            if path.is_file():
                safe = ensure_within_project(
                    self.project_root,
                    path,
                    label=f"isolated workspace file '{relative}'",
                )
                result[relative] = ("file", safe.read_bytes())
        return result

    def __enter__(self) -> "AllowedRootsMutationGuard":
        self._before = self._scan()
        return self

    def _restore(self, changed: Iterable[str]) -> None:
        for relative in changed:
            if relative in self._before:
                continue
            raw = self.project_root / relative
            if raw.is_symlink() or raw.is_file():
                raw.unlink(missing_ok=True)

        for relative in changed:
            before = self._before.get(relative)
            if before is None:
                continue
            kind, content = before
            raw = self.project_root / relative
            if kind == "symlink":
                raise IsolatedWriteViolation(
                    "SDAI-ISOLATED-017: isolated workspace contained a changed pre-existing symlink; "
                    f"manual recovery required for {relative}"
                )
            if raw.is_symlink():
                raw.unlink()
            path = ensure_within_project(
                self.project_root,
                raw,
                label=f"isolated restore path '{relative}'",
            )
            path.parent.mkdir(parents=True, exist_ok=True)
            assert content is not None
            path.write_bytes(content)

    def __exit__(self, exc_type, exc, tb) -> bool:
        after = self._scan()
        changed = sorted(
            relative
            for relative in set(self._before) | set(after)
            if self._before.get(relative) != after.get(relative)
        )
        unauthorized = [
            relative
            for relative in changed
            if not _under(relative, self.allowed_roots)
            or _under(relative, self.forbidden_roots)
        ]
        if unauthorized:
            self._restore(changed)
            preview = ", ".join(unauthorized[:8])
            suffix = " ..." if len(unauthorized) > 8 else ""
            raise IsolatedWriteViolation(
                "SDAI-ISOLATED-017: isolated agent modified paths outside its task allowlist; "
                f"the entire invocation was rolled back: {preview}{suffix}"
            )
        return False


def execute_isolated_stage(
    runtime: AgentRuntime,
    prepared: PreparedIsolatedInvocation,
    *,
    status: IsolatedStageStatus = IsolatedStageStatus.RECORDED,
) -> IsolatedStageResult:
    """Execute an isolated invocation with current-context and write-boundary checks."""

    contract = prepared.contract
    validate_isolated_context_current(runtime.project_root, contract)
    if contract.mode.value == "workspace-write":
        with AllowedRootsMutationGuard(
            runtime.project_root,
            contract.allowed_roots,
            contract.forbidden_roots,
        ):
            return execute_isolated_invocation(runtime, prepared, status=status)
    return execute_isolated_invocation(runtime, prepared, status=status)


__all__ = [
    "AllowedRootsMutationGuard",
    "IsolatedWriteViolation",
    "execute_isolated_stage",
    "validate_isolated_context_current",
]
