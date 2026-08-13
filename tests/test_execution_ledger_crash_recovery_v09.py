from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest

from sdai.execution_ledger import create_execution_run, load_execution_run


FEATURE = "LEDGER-CRASH"
BASELINE = "c" * 40


def _feature(root: Path) -> None:
    feature = root / "specs" / FEATURE
    feature.mkdir(parents=True)
    (feature / "00-intake.md").write_text("# crash recovery café Δ\n", encoding="utf-8")


def test_dead_lock_owner_is_reclaimed_after_process_crash(tmp_path: Path) -> None:
    _feature(tmp_path)
    ledger = create_execution_run(
        tmp_path,
        FEATURE,
        "enterprise",
        BASELINE,
        run_id="run-crash",
    )

    script = """
import os
import sys
from pathlib import Path
from sdai.execution_ledger import load_execution_run
root = Path(sys.argv[1])
ledger = load_execution_run(root, sys.argv[2], sys.argv[3])
lock = ledger._lock()
lock.__enter__()
os.write(1, b'locked\\n')
os.fsync(1)
os._exit(23)
"""
    completed = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path), FEATURE, "run-crash"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        env=dict(os.environ),
    )

    assert completed.returncode == 23
    assert completed.stdout == "locked\n"
    assert ledger.lock_path.exists(), "crashed process must leave the lock artifact for recovery test"

    reloaded = load_execution_run(tmp_path, FEATURE, "run-crash")
    reloaded.append_event("task.registered", task_id="TASK-001")
    state = reloaded.reconstruct()

    assert state.last_sequence == 2
    assert state.task_map()["TASK-001"].status == "registered"
    assert not reloaded.lock_path.exists()


def test_live_lock_owner_is_never_reclaimed(tmp_path: Path) -> None:
    _feature(tmp_path)
    ledger = create_execution_run(
        tmp_path,
        FEATURE,
        "enterprise",
        BASELINE,
        run_id="run-live",
    )

    with ledger._lock():
        with pytest.raises(RuntimeError, match="locked by another process"):
            ledger.append_event("task.registered", task_id="TASK-001")

    ledger.append_event("task.registered", task_id="TASK-001")
    assert ledger.reconstruct().task_map()["TASK-001"].status == "registered"
