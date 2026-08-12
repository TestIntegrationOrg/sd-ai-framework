from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from typing import Mapping


class WorktreeIsolationError(RuntimeError):
    """Raised when SDAI cannot establish or preserve a safe worktree boundary."""


_FEATURE_SAFE = re.compile(r"[^A-Za-z0-9._-]+")
_DANGEROUS_GIT_ENV = {
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_CONFIG",
    "GIT_CONFIG_GLOBAL",
    "GIT_CONFIG_SYSTEM",
}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _git_executable() -> str:
    candidate = shutil.which("git")
    if not candidate:
        raise WorktreeIsolationError("SDAI-WORKTREE-001: Git executable is not available")
    resolved = Path(candidate).resolve()
    if not resolved.is_file():
        raise WorktreeIsolationError(
            f"SDAI-WORKTREE-001: resolved Git executable is not a file: {resolved}"
        )
    return str(resolved)


def _git_env(source: Mapping[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ if source is None else source)
    for key in list(env):
        upper = key.upper()
        if (
            upper in _DANGEROUS_GIT_ENV
            or upper.startswith("GIT_CONFIG_KEY_")
            or upper.startswith("GIT_CONFIG_VALUE_")
        ):
            env.pop(key, None)
    for key in list(env):
        if key.upper() == "GIT_CONFIG_COUNT":
            env.pop(key, None)
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _git(
    cwd: Path,
    *args: str,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        [_git_executable(), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
        env=_git_env(),
        check=False,
    )
    if check and completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "git command failed").strip()
        raise WorktreeIsolationError(
            f"SDAI-WORKTREE-002: git {' '.join(args)} failed: {detail}"
        )
    return completed


def _output(cwd: Path, *args: str) -> str:
    return (_git(cwd, *args).stdout or "").strip()


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _portable(path: Path) -> str:
    return path.as_posix()


def _safe_feature(feature_id: str) -> str:
    value = _FEATURE_SAFE.sub("-", feature_id.strip()).strip(".-_")
    if not value:
        raise WorktreeIsolationError("SDAI-WORKTREE-003: feature id cannot form a safe worktree name")
    return value[:80]


def _atomic_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="\n",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    )
    temp = Path(handle.name)
    try:
        with handle:
            json.dump(
                payload,
                handle,
                indent=2,
                sort_keys=True,
                ensure_ascii=False,
                allow_nan=False,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


@dataclass(frozen=True)
class GitBaselineEvidence:
    repository_root: Path
    repository_identity: str
    origin_url: str | None
    branch: str
    commit: str
    tree: str
    status_sha256: str
    clean: bool
    common_git_dir: Path

    def as_dict(self) -> dict[str, object]:
        return {
            "repository_root": _portable(self.repository_root),
            "repository_identity": self.repository_identity,
            "origin_url": self.origin_url,
            "branch": self.branch,
            "commit": self.commit,
            "tree": self.tree,
            "status_sha256": self.status_sha256,
            "clean": self.clean,
            "common_git_dir": _portable(self.common_git_dir),
        }


@dataclass(frozen=True)
class _WorktreeSnapshot:
    exists: bool
    status: str
    commit: str | None
    tree: str | None

    @property
    def dirty(self) -> bool:
        return bool(self.status)


@dataclass(frozen=True)
class WorktreeSession:
    source_root: Path
    feature_id: str
    run_id: str
    worktree_path: Path
    worktree_branch: str
    evidence_path: Path
    baseline: GitBaselineEvidence
    created_at: str

    def _snapshot(self) -> _WorktreeSnapshot:
        if not self.worktree_path.exists():
            return _WorktreeSnapshot(False, "", None, None)
        status = (
            _git(
                self.worktree_path,
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
            ).stdout
            or ""
        )
        return _WorktreeSnapshot(
            True,
            status,
            _output(self.worktree_path, "rev-parse", "HEAD"),
            _output(self.worktree_path, "rev-parse", "HEAD^{tree}"),
        )

    def _write_evidence(
        self,
        *,
        state: str,
        outcome: str | None = None,
        error: str | None = None,
        cleanup: str = "preserved",
        snapshot: _WorktreeSnapshot | None = None,
        exists_after: bool | None = None,
    ) -> None:
        current = snapshot or self._snapshot()
        exists = current.exists if exists_after is None else exists_after
        payload: dict[str, object] = {
            "version": 1,
            "kind": "sdai.worktree-evidence/v1",
            "feature_id": self.feature_id,
            "run_id": self.run_id,
            "created_at": self.created_at,
            "updated_at": _utc_now().isoformat(),
            "state": state,
            "source": self.baseline.as_dict(),
            "worktree": {
                "path": _portable(self.worktree_path),
                "branch": self.worktree_branch,
                "exists": exists,
                "dirty": current.dirty,
                "status_sha256": "sha256:"
                + sha256(current.status.encode("utf-8")).hexdigest(),
                "head_commit": current.commit,
                "head_tree": current.tree,
                "cleanup": cleanup,
            },
        }
        if outcome is not None:
            payload["outcome"] = outcome
        if error:
            payload["error"] = error
        _atomic_json(self.evidence_path, payload)

    def finalize(
        self,
        outcome: str,
        *,
        error: str | None = None,
        cleanup_requested: bool = False,
    ) -> str:
        if outcome not in {"success", "failed", "paused", "cancelled"}:
            raise ValueError(f"Unsupported worktree outcome: {outcome}")
        snapshot = self._snapshot()
        should_cleanup = (
            outcome in {"failed", "cancelled"} or cleanup_requested
        ) and not snapshot.dirty
        cleanup = "preserved-dirty" if snapshot.dirty else "preserved-clean"
        cleanup_error: str | None = None
        if should_cleanup and snapshot.exists:
            try:
                _git(
                    self.source_root,
                    "worktree",
                    "remove",
                    "--force",
                    str(self.worktree_path),
                )
                branch_delete = _git(
                    self.source_root,
                    "branch",
                    "-D",
                    self.worktree_branch,
                    check=False,
                )
                if branch_delete.returncode != 0:
                    cleanup_error = (
                        branch_delete.stderr
                        or branch_delete.stdout
                        or "branch cleanup failed"
                    ).strip()
                    cleanup = "worktree-removed-branch-preserved"
                else:
                    cleanup = "removed-clean"
            except WorktreeIsolationError as exc:
                cleanup_error = str(exc)
                cleanup = "cleanup-failed"
        elif cleanup_requested and snapshot.dirty:
            cleanup = "preserved-dirty-cleanup-refused"
        combined_error = error
        if cleanup_error:
            combined_error = (
                f"{error}; cleanup: {cleanup_error}"
                if error
                else f"cleanup: {cleanup_error}"
            )
        self._write_evidence(
            state="completed",
            outcome=outcome,
            error=combined_error,
            cleanup=cleanup,
            snapshot=snapshot,
            exists_after=self.worktree_path.exists(),
        )
        return cleanup


def verify_clean_baseline(project_root: Path) -> GitBaselineEvidence:
    root = project_root.resolve()
    if not (root / ".git").exists():
        raise WorktreeIsolationError(
            "SDAI-WORKTREE-003: worktree isolation requires a Git working tree"
        )
    top = Path(_output(root, "rev-parse", "--show-toplevel")).resolve()
    if top != root:
        raise WorktreeIsolationError(
            f"SDAI-WORKTREE-003: --path must be the repository root; Git root is {top}"
        )
    branch = _output(root, "branch", "--show-current")
    if not branch:
        raise WorktreeIsolationError(
            "SDAI-WORKTREE-004: detached HEAD is not an acceptable worktree baseline"
        )
    status = (
        _git(root, "status", "--porcelain=v1", "--untracked-files=all").stdout
        or ""
    )
    if status:
        preview = ", ".join(line[:160] for line in status.splitlines()[:5])
        raise WorktreeIsolationError(
            "SDAI-WORKTREE-004: source baseline is dirty; commit/stash changes before isolated execution"
            + (f" ({preview})" if preview else "")
        )
    commit = _output(root, "rev-parse", "HEAD")
    tree = _output(root, "rev-parse", "HEAD^{tree}")
    common_raw = _output(root, "rev-parse", "--git-common-dir")
    common_dir = Path(common_raw)
    if not common_dir.is_absolute():
        common_dir = (root / common_dir).resolve()
    else:
        common_dir = common_dir.resolve()
    origin = _git(root, "config", "--get", "remote.origin.url", check=False)
    origin_url = (origin.stdout or "").strip() or None
    identity_source = f"{origin_url or _portable(root)}\n{_portable(common_dir)}"
    identity = "sha256:" + sha256(identity_source.encode("utf-8")).hexdigest()
    return GitBaselineEvidence(
        repository_root=root,
        repository_identity=identity,
        origin_url=origin_url,
        branch=branch,
        commit=commit,
        tree=tree,
        status_sha256="sha256:" + sha256(status.encode("utf-8")).hexdigest(),
        clean=True,
        common_git_dir=common_dir,
    )


def _worktree_base(source_root: Path) -> Path:
    configured = os.getenv("SDAI_WORKTREE_ROOT", "").strip()
    if configured:
        candidate = Path(configured)
        if not candidate.is_absolute():
            raise WorktreeIsolationError(
                "SDAI-WORKTREE-005: SDAI_WORKTREE_ROOT must be an absolute path"
            )
        base = candidate.resolve()
    else:
        base = (
            source_root.parent / f".{source_root.name}.sdai-worktrees"
        ).resolve()
    root = source_root.resolve()
    if base == root or _is_within(base, root):
        raise WorktreeIsolationError(
            "SDAI-WORKTREE-005: worktree root must be outside the source workspace"
        )
    return base


def create_worktree_session(project_root: Path, feature_id: str) -> WorktreeSession:
    baseline = verify_clean_baseline(project_root)
    safe_feature = _safe_feature(feature_id)
    created = _utc_now()
    run_id = f"{created.strftime('%Y%m%dT%H%M%S%fZ')}-{baseline.commit[:10]}"
    base = _worktree_base(baseline.repository_root)
    base.mkdir(parents=True, exist_ok=True)
    worktree_path = (base / safe_feature / run_id).resolve()
    if not _is_within(worktree_path, base):
        raise WorktreeIsolationError(
            "SDAI-WORKTREE-005: allocated worktree path escaped its root"
        )
    if worktree_path.exists():
        raise WorktreeIsolationError(
            f"SDAI-WORKTREE-005: allocated worktree already exists: {worktree_path}"
        )
    branch = f"sdai/{safe_feature}/{run_id}"
    _git(baseline.repository_root, "check-ref-format", "--branch", branch)
    evidence_dir = (
        baseline.common_git_dir / "sdai" / "worktree-evidence" / safe_feature
    )
    evidence_path = evidence_dir / f"{run_id}.json"
    session = WorktreeSession(
        source_root=baseline.repository_root,
        feature_id=feature_id,
        run_id=run_id,
        worktree_path=worktree_path,
        worktree_branch=branch,
        evidence_path=evidence_path,
        baseline=baseline,
        created_at=created.isoformat(),
    )
    session._write_evidence(state="preparing", cleanup="not-created")
    try:
        worktree_path.parent.mkdir(parents=True, exist_ok=True)
        _git(
            baseline.repository_root,
            "worktree",
            "add",
            "-b",
            branch,
            str(worktree_path),
            baseline.commit,
        )
        worktree_commit = _output(worktree_path, "rev-parse", "HEAD")
        worktree_tree = _output(worktree_path, "rev-parse", "HEAD^{tree}")
        status = (
            _git(
                worktree_path,
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
            ).stdout
            or ""
        )
        if (
            worktree_commit != baseline.commit
            or worktree_tree != baseline.tree
            or status
        ):
            raise WorktreeIsolationError(
                "SDAI-WORKTREE-006: created worktree does not match the verified clean baseline"
            )
        if not (worktree_path / ".sdai" / "config.yaml").is_file():
            raise WorktreeIsolationError(
                "SDAI-WORKTREE-006: isolated worktree is missing tracked SDAI configuration"
            )
        session._write_evidence(state="ready", cleanup="preserved-clean")
        return session
    except Exception as exc:
        if worktree_path.exists():
            _git(
                baseline.repository_root,
                "worktree",
                "remove",
                "--force",
                str(worktree_path),
                check=False,
            )
        _git(
            baseline.repository_root,
            "branch",
            "-D",
            branch,
            check=False,
        )
        session._write_evidence(
            state="failed-to-create",
            outcome="failed",
            error=str(exc),
            cleanup="creation-rolled-back",
        )
        if isinstance(exc, WorktreeIsolationError):
            raise
        raise WorktreeIsolationError(
            f"SDAI-WORKTREE-006: unable to create isolated worktree: {exc}"
        ) from exc
