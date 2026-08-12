from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

import sdai.cli as cli_module
from sdai.worktree_isolation import (
    WorktreeIsolationError,
    create_worktree_session,
    verify_clean_baseline,
)


def _git_exe() -> str:
    value = shutil.which("git")
    if not value:
        pytest.skip("git is not available")
    return value


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
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
    if check and completed.returncode != 0:
        raise AssertionError(completed.stderr or completed.stdout)
    return completed


def _repo(tmp_path: Path, name: str = "Enterprise Repo Ω") -> Path:
    root = tmp_path / name
    root.mkdir()
    _git(root, "init")
    _git(root, "config", "user.email", "sdai-tests@example.invalid")
    _git(root, "config", "user.name", "SDAI Tests")
    _git(root, "checkout", "-b", "main")
    sdai = root / ".sdai"
    sdai.mkdir()
    (sdai / "config.yaml").write_text("version: 1\n", encoding="utf-8")
    (sdai / "policy.yaml").write_text("version: 1\nprotected: true\n", encoding="utf-8")
    (root / "README.md").write_text("# café Δ\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "baseline")
    return root


def _status(root: Path) -> str:
    return (_git(root, "status", "--porcelain=v1", "--untracked-files=all").stdout or "")


def _branch_exists(root: Path, branch: str) -> bool:
    result = _git(root, "show-ref", "--verify", f"refs/heads/{branch}", check=False)
    return result.returncode == 0


def test_verify_clean_baseline_records_exact_git_identity(tmp_path: Path) -> None:
    root = _repo(tmp_path)

    evidence = verify_clean_baseline(root)

    assert evidence.clean is True
    assert evidence.repository_root == root.resolve()
    assert evidence.branch == "main"
    assert evidence.commit == (_git(root, "rev-parse", "HEAD").stdout or "").strip()
    assert evidence.tree == (_git(root, "rev-parse", "HEAD^{tree}").stdout or "").strip()
    assert evidence.status_sha256.startswith("sha256:")
    assert evidence.repository_identity.startswith("sha256:")
    assert evidence.common_git_dir.is_dir()
    assert _status(root) == ""


def test_create_worktree_preserves_source_and_tracked_security_controls(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    before_policy = (root / ".sdai" / "policy.yaml").read_bytes()
    before_commit = (_git(root, "rev-parse", "HEAD").stdout or "").strip()

    session = create_worktree_session(root, "SIGN-123")

    assert session.worktree_path.is_dir()
    assert not session.worktree_path.is_relative_to(root)
    assert (_git(session.worktree_path, "rev-parse", "HEAD").stdout or "").strip() == before_commit
    assert (session.worktree_path / ".sdai" / "policy.yaml").read_bytes() == before_policy
    assert _status(root) == ""
    assert session.evidence_path.is_file()
    payload = json.loads(session.evidence_path.read_text(encoding="utf-8"))
    assert payload["state"] == "ready"
    assert payload["source"]["clean"] is True
    assert payload["source"]["commit"] == before_commit
    assert payload["worktree"]["branch"] == session.worktree_branch

    cleanup = session.finalize("success", cleanup_requested=True)
    assert cleanup == "removed-clean"
    assert not session.worktree_path.exists()
    assert not _branch_exists(root, session.worktree_branch)
    assert _status(root) == ""


def test_dirty_tracked_or_untracked_baseline_is_refused(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (root / "README.md").write_text("changed\n", encoding="utf-8")

    with pytest.raises(WorktreeIsolationError, match="baseline is dirty"):
        verify_clean_baseline(root)

    _git(root, "restore", "README.md")
    (root / "untracked.txt").write_text("no\n", encoding="utf-8")
    with pytest.raises(WorktreeIsolationError, match="baseline is dirty"):
        create_worktree_session(root, "SIGN-124")


def test_detached_head_is_refused(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    commit = (_git(root, "rev-parse", "HEAD").stdout or "").strip()
    _git(root, "checkout", "--detach", commit)

    with pytest.raises(WorktreeIsolationError, match="detached HEAD"):
        verify_clean_baseline(root)


def test_worktree_root_must_be_absolute_and_outside_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _repo(tmp_path)
    monkeypatch.setenv("SDAI_WORKTREE_ROOT", "relative/worktrees")
    with pytest.raises(WorktreeIsolationError, match="must be an absolute path"):
        create_worktree_session(root, "SIGN-125")

    monkeypatch.setenv("SDAI_WORKTREE_ROOT", str(root / ".sdai" / "worktrees"))
    with pytest.raises(WorktreeIsolationError, match="outside the source workspace"):
        create_worktree_session(root, "SIGN-125")


def test_git_environment_overrides_cannot_redirect_baseline_checks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _repo(tmp_path)
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "attacker.git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(tmp_path / "attacker-tree"))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.hooksPath")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(tmp_path / "hooks"))

    evidence = verify_clean_baseline(root)

    assert evidence.repository_root == root.resolve()
    assert evidence.branch == "main"


def test_failed_clean_execution_auto_cleans_worktree_and_branch(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    session = create_worktree_session(root, "SIGN-126")

    cleanup = session.finalize("failed", error="simulated failure")

    assert cleanup == "removed-clean"
    assert not session.worktree_path.exists()
    assert not _branch_exists(root, session.worktree_branch)
    payload = json.loads(session.evidence_path.read_text(encoding="utf-8"))
    assert payload["outcome"] == "failed"
    assert payload["worktree"]["cleanup"] == "removed-clean"
    assert "simulated failure" in payload["error"]


def test_failed_dirty_execution_is_preserved_instead_of_discarded(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    session = create_worktree_session(root, "SIGN-127")
    changed = session.worktree_path / "implementation.txt"
    changed.write_text("valuable implementation\n", encoding="utf-8")

    cleanup = session.finalize("failed", error="provider failed")

    assert cleanup == "preserved-dirty"
    assert session.worktree_path.is_dir()
    assert changed.read_text(encoding="utf-8") == "valuable implementation\n"
    assert _branch_exists(root, session.worktree_branch)
    payload = json.loads(session.evidence_path.read_text(encoding="utf-8"))
    assert payload["worktree"]["dirty"] is True
    assert payload["worktree"]["cleanup"] == "preserved-dirty"

    _git(root, "worktree", "remove", "--force", str(session.worktree_path))
    _git(root, "branch", "-D", session.worktree_branch)


def test_cleanup_request_refuses_to_discard_dirty_success(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    session = create_worktree_session(root, "SIGN-128")
    (session.worktree_path / "result.txt").write_text("keep me\n", encoding="utf-8")

    cleanup = session.finalize("success", cleanup_requested=True)

    assert cleanup == "preserved-dirty-cleanup-refused"
    assert session.worktree_path.exists()
    assert _branch_exists(root, session.worktree_branch)

    _git(root, "worktree", "remove", "--force", str(session.worktree_path))
    _git(root, "branch", "-D", session.worktree_branch)


def test_cli_worktree_mode_runs_orchestrator_only_in_isolated_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = _repo(tmp_path)
    seen: list[Path] = []

    class FakeOrchestrator:
        def __init__(self, project_root: Path) -> None:
            seen.append(Path(project_root).resolve())

        def run_workflow(self, feature_id: str, workflow: str):
            assert feature_id == "SIGN-129"
            assert workflow == "enterprise"
            return []

    monkeypatch.setattr(cli_module, "Orchestrator", FakeOrchestrator)

    exit_code = cli_module.main(
        [
            "run",
            "SIGN-129",
            "--workflow",
            "enterprise",
            "--isolation",
            "worktree",
            "--cleanup-worktree",
            "--path",
            str(root),
        ]
    )

    assert exit_code == 0
    assert len(seen) == 1
    assert seen[0] != root.resolve()
    assert not seen[0].exists()
    assert _status(root) == ""
    output = capsys.readouterr().out
    assert "Worktree isolation baseline" in output
    assert "cleanup=removed-clean" in output


def test_evidence_and_paths_support_spaces_and_unicode(tmp_path: Path) -> None:
    root = _repo(tmp_path, "Repo With Spaces café Ω")
    session = create_worktree_session(root, "Feature-Δ-130")

    payload = json.loads(session.evidence_path.read_text(encoding="utf-8"))

    assert payload["feature_id"] == "Feature-Δ-130"
    assert payload["source"]["repository_root"] == root.resolve().as_posix()
    assert "Repo With Spaces café Ω" in payload["source"]["repository_root"]
    assert session.worktree_path.exists()

    assert session.finalize("success", cleanup_requested=True) == "removed-clean"
