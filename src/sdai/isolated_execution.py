from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path, PurePosixPath
import shutil
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


def validate_isolated_context_current(
    project_root: Path,
    contract: IsolatedTaskContract,
) -> None:
    """Fail closed if a persisted file-backed task context no longer matches bytes.

    Generated final-review diff slices live under the isolated framework-state
    namespace and are already embedded/hash-bound in the immutable contract. File
    provenance slices are revalidated immediately before provider execution.
    """

    root = project_root.resolve()
    for item in contract.context:
        if (
            contract.stage is IsolatedStage.FINAL_CHANGE_REVIEW
            and item.source.startswith(f".sdai/isolated/{contract.feature_id}/")
        ):
            continue
        path = _context_file(root, item.source)
        current = _digest(path.read_bytes())
        if current != item.source_sha256:
            raise IsolatedTaskError(
                "SDAI-ISOLATED-016: isolated task context is stale; "
                f"{item.source} changed from {item.source_sha256} to {current}"
            )


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
            # Do not follow symlink directories. A symlink itself is captured and
            # any new/changed symlink is unauthorized unless its path is allowed.
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

    def _restore(self, changed: Iterable[str], after: dict[str, tuple[str, bytes | None]]) -> None:
        # Remove newly-created unauthorized files/symlinks first.
        for relative in changed:
            if relative in self._before:
                continue
            raw = self.project_root / relative
            if raw.is_symlink() or raw.is_file():
                raw.unlink(missing_ok=True)

        # Restore modified/deleted unauthorized files. Existing symlinks are not a
        # supported safe baseline for isolated execution; fail closed rather than
        # following/reconstructing attacker-controlled targets.
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
            self._restore(unauthorized, after)
            preview = ", ".join(unauthorized[:8])
            suffix = " ..." if len(unauthorized) > 8 else ""
            raise IsolatedWriteViolation(
                "SDAI-ISOLATED-017: isolated agent modified paths outside its task allowlist; "
                f"changes were restored: {preview}{suffix}"
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
