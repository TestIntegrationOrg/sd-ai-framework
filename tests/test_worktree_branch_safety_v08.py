from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import shutil
import subprocess

import pytest

import sdai.worktree_isolation as worktree_module
from sdai.worktree_isolation import WorktreeIsolationError, create_worktree_session


def _git_exe() -> str:
    value = shutil.which("git")
    if not value:
        pytest.skip("git is not available")
    return value


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        [_git_exe(), *args],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stderr or completed.stdout)
    return (completed.stdout or "").strip()


def _repo(tmp_path: Path) -> tuple[Path, str, str]:
    root = tmp_path / "branch-collision-repo"
    root.mkdir()
    _git(root, "init")
    _git(root, "config", "user.email", "sdai-tests@example.invalid")
    _git(root, "config", "user.name", "SDAI Tests")
    _git(root, "config", "core.autocrlf", "false")
    current = _git(root, "branch", "--show-current")
    if current != "main":
        _git(root, "checkout", "-b", "main")
    (root / ".sdai").mkdir()
    (root / ".sdai" / "config.yaml").write_text("version: 1\n", encoding="utf-8")
    (root / "value.txt").write_text("one\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "first")
    first = _git(root, "rev-parse", "HEAD")
    (root / "value.txt").write_text("two\n", encoding="utf-8")
    _git(root, "add", "value.txt")
    _git(root, "commit", "-m", "second")
    second = _git(root, "rev-parse", "HEAD")
    return root, first, second


def test_preexisting_generated_branch_is_never_deleted_on_creation_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, first, second = _repo(tmp_path)
    fixed = datetime(2026, 8, 12, 23, 59, 58, 123456, tzinfo=timezone.utc)
    monkeypatch.setattr(worktree_module, "_utc_now", lambda: fixed)
    run_id = f"{fixed.strftime('%Y%m%dT%H%M%S%fZ')}-{second[:10]}"
    branch = f"sdai/SIGN-131/{run_id}"
    _git(root, "branch", branch, first)

    with pytest.raises(WorktreeIsolationError, match="already exists"):
        create_worktree_session(root, "SIGN-131")

    assert _git(root, "rev-parse", branch) == first
    worktree_root = root.parent / f".{root.name}.sdai-worktrees"
    allocated = worktree_root / "SIGN-131" / run_id
    assert not allocated.exists()
