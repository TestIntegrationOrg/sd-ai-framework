from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

from sdai.completion_ledger import (
    CompletionLedgerError,
    declared_completion_contracts,
    register_completion_requirements,
)
from sdai.completion_policy import (
    CODE_QUALITY_REVIEW_CONTRACT,
    SPEC_REVIEW_CONTRACT,
    TEST_CONTRACT,
)
from sdai.execution_ledger import create_execution_run


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        shell=False,
    )
    return completed.stdout.strip()


def _ledger(tmp_path: Path):
    root = tmp_path / "completion Ω"
    root.mkdir()
    (root / "README.md").write_text("# completion café\n", encoding="utf-8")
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "SDAI Completion")
    _git(root, "config", "user.email", "sdai@example.test")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "baseline")
    head = _git(root, "rev-parse", "HEAD")
    return create_execution_run(
        root,
        "COMPLETE-122",
        "enterprise",
        head,
        run_id="run-complete-122",
    )


def test_registration_records_policy_and_custom_contracts(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)

    register_completion_requirements(
        ledger,
        "TASK-001",
        "standard",
        "task",
        additional_required=("sdai.debug-record/v1",),
    )

    declared = declared_completion_contracts(ledger, "TASK-001")
    assert SPEC_REVIEW_CONTRACT in declared
    assert CODE_QUALITY_REVIEW_CONTRACT in declared
    assert TEST_CONTRACT in declared
    assert "sdai.debug-record/v1" in declared


def test_existing_registration_cannot_be_weaker_than_new_org_requirement(
    tmp_path: Path,
) -> None:
    ledger = _ledger(tmp_path)
    register_completion_requirements(ledger, "TASK-001", "trivial", "task")

    with pytest.raises(CompletionLedgerError, match="weakens"):
        register_completion_requirements(
            ledger,
            "TASK-001",
            "trivial",
            "task",
            organization_required=("company/security-review/v1",),
        )
