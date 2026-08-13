from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from sdai.entrypoint import main as entrypoint_main
from sdai.execution_ledger import ExecutionLedgerError, create_execution_run
from sdai.execution_resume import build_resume_plan, resume_execution


FEATURE = "RESUME-100"
RUN_ID = "run-resume"


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    return (completed.stdout or "").strip()


def _repo(tmp_path: Path):
    root = tmp_path / "resume repo Ω"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "SDAI Test")
    _git(root, "config", "user.email", "sdai@example.test")
    (root / ".sdai").mkdir()
    (root / ".sdai" / "config.yaml").write_text("project: resume-test\n", encoding="utf-8")
    feature = root / "specs" / FEATURE
    feature.mkdir(parents=True)
    (feature / "00-intake.md").write_text("# Resume feature café Δ\n", encoding="utf-8")
    (root / "tracked.txt").write_text("clean\n", encoding="utf-8")
    _git(root, "add", ".sdai/config.yaml", f"specs/{FEATURE}/00-intake.md", "tracked.txt")
    _git(root, "commit", "-m", "baseline")
    baseline = _git(root, "rev-parse", "HEAD")
    ledger = create_execution_run(root, FEATURE, "enterprise", baseline, run_id=RUN_ID)
    return root, ledger


def _register(ledger, *task_ids: str) -> None:
    for task_id in task_ids:
        ledger.append_event("task.registered", task_id=task_id, payload={"title": task_id})


def _complete(root: Path, ledger, task_id: str) -> tuple[Path, str]:
    state = ledger.reconstruct().task_map()[task_id]
    if state.status == "registered":
        ledger.append_event("task.started", task_id=task_id)
    artifact = root / "specs" / FEATURE / f"{task_id}.txt"
    artifact.write_text(f"completed {task_id} Ω\n", encoding="utf-8")
    _git(root, "add", artifact.relative_to(root).as_posix())
    _git(root, "commit", "-m", f"complete {task_id}")
    commit = _git(root, "rev-parse", "HEAD")
    artifact_binding = ledger.binding_for_file(artifact, kind="artifact")
    evidence = ledger.write_task_record(
        task_id,
        "evidence",
        {"tests": "passed", "task": task_id},
    )
    ledger.append_event(
        "task.completed",
        task_id=task_id,
        git_commit=commit,
        bindings=(artifact_binding, evidence),
    )
    return artifact, commit


def test_resume_uses_registration_order_and_skips_only_verified_completed_tasks(tmp_path: Path) -> None:
    root, ledger = _repo(tmp_path)
    _register(ledger, "TASK-020", "TASK-010", "TASK-030")
    _complete(root, ledger, "TASK-020")

    plan = build_resume_plan(root, FEATURE, RUN_ID)

    assert plan.task_order == ("TASK-020", "TASK-010", "TASK-030")
    assert plan.tasks[0].action == "skip"
    assert plan.tasks[0].skip_verified is True
    assert plan.resume_task_id == "TASK-010"
    assert plan.resume_action == "dispatch"
    assert plan.repository_clean is True


def test_completion_commit_may_be_an_ancestor_of_later_head(tmp_path: Path) -> None:
    root, ledger = _repo(tmp_path)
    _register(ledger, "TASK-001", "TASK-002")
    _, completed_commit = _complete(root, ledger, "TASK-001")
    extra = root / "later.txt"
    extra.write_text("later task work\n", encoding="utf-8")
    _git(root, "add", "later.txt")
    _git(root, "commit", "-m", "later commit")

    plan = build_resume_plan(root, FEATURE, RUN_ID)

    assert plan.current_head != completed_commit
    assert plan.tasks[0].git_reachable is True
    assert plan.tasks[0].action == "skip"
    assert plan.resume_task_id == "TASK-002"


def test_stale_artifact_reopens_task_and_second_resume_reuses_dispatch(tmp_path: Path) -> None:
    root, ledger = _repo(tmp_path)
    _register(ledger, "TASK-001", "TASK-002")
    artifact, _ = _complete(root, ledger, "TASK-001")
    artifact.write_text("changed after completion\n", encoding="utf-8")
    _git(root, "add", artifact.relative_to(root).as_posix())
    _git(root, "commit", "-m", "change completed artifact")

    before = build_resume_plan(root, FEATURE, RUN_ID)
    assert before.resume_task_id == "TASK-001"
    assert before.resume_action == "reopen"
    assert "binding_hash_mismatch" in before.tasks[0].reasons

    first = resume_execution(root, FEATURE, RUN_ID)
    assert first.status == "ready"
    assert first.dispatch_id is not None
    assert first.dispatch_reused is False
    assert ledger.reconstruct().task_map()["TASK-001"].status == "registered"
    first_count = len(ledger.load_events())

    second = resume_execution(root, FEATURE, RUN_ID)
    assert second.dispatch_id == first.dispatch_id
    assert second.dispatch_reused is True
    assert len(ledger.load_events()) == first_count


def test_rewritten_completion_commit_invalidates_skip_even_when_file_matches(tmp_path: Path) -> None:
    root, ledger = _repo(tmp_path)
    _register(ledger, "TASK-001", "TASK-002")
    artifact, old_commit = _complete(root, ledger, "TASK-001")
    assert artifact.exists()
    _git(root, "commit", "--amend", "--no-edit")
    assert _git(root, "rev-parse", "HEAD") != old_commit

    plan = build_resume_plan(root, FEATURE, RUN_ID)

    assert plan.resume_task_id == "TASK-001"
    assert plan.tasks[0].action == "reopen"
    assert plan.tasks[0].git_reachable is False
    assert any(reason.startswith("recorded_commit_") for reason in plan.tasks[0].reasons)


def test_dirty_engineering_workspace_blocks_resume_without_ledger_mutation(tmp_path: Path) -> None:
    root, ledger = _repo(tmp_path)
    _register(ledger, "TASK-001")
    (root / "tracked.txt").write_text("dirty\n", encoding="utf-8")
    before = ledger.events_path.read_bytes()

    plan = build_resume_plan(root, FEATURE, RUN_ID)
    result = resume_execution(root, FEATURE, RUN_ID)

    assert plan.repository_clean is False
    assert plan.blocked_reason == "repository_dirty_outside_execution_state"
    assert result.status == "blocked"
    assert result.dispatch_id is None
    assert ledger.events_path.read_bytes() == before


def test_failed_task_is_reopened_for_a_new_attempt(tmp_path: Path) -> None:
    root, ledger = _repo(tmp_path)
    _register(ledger, "TASK-001")
    ledger.append_event("task.started", task_id="TASK-001")
    ledger.append_event("task.failed", task_id="TASK-001", payload={"reason": "provider interrupted"})

    result = resume_execution(root, FEATURE, RUN_ID)

    assert result.status == "ready"
    assert result.dispatch_id is not None
    assert result.plan.resume_task_id == "TASK-001"
    task = ledger.reconstruct().task_map()["TASK-001"]
    assert task.status == "registered"
    reopened = [event for event in ledger.load_events() if event.kind == "task.reopened"]
    assert len(reopened) == 1
    assert reopened[0].payload["reason"] == "retry_failed_task"


def test_started_task_reuses_same_dispatch_token_after_interruption(tmp_path: Path) -> None:
    root, ledger = _repo(tmp_path)
    _register(ledger, "TASK-001")

    reserved = resume_execution(root, FEATURE, RUN_ID)
    assert reserved.dispatch_id is not None
    ledger.append_event("task.started", task_id="TASK-001")
    count = len(ledger.load_events())

    resumed = resume_execution(root, FEATURE, RUN_ID)

    assert resumed.status == "ready"
    assert resumed.dispatch_id == reserved.dispatch_id
    assert resumed.dispatch_reused is True
    assert len(ledger.load_events()) == count


def test_paused_run_is_resumed_before_dispatch_reservation(tmp_path: Path) -> None:
    root, ledger = _repo(tmp_path)
    _register(ledger, "TASK-001")
    ledger.append_event("run.paused", payload={"reason": "process stop"})

    result = resume_execution(root, FEATURE, RUN_ID)
    kinds = [event.kind for event in ledger.load_events()]

    assert result.status == "ready"
    assert "run.resumed" in kinds
    assert kinds.index("run.resumed") < kinds.index("task.dispatch_reserved")
    assert ledger.reconstruct().status == "active"


def test_stale_checkpoint_is_not_trusted_and_is_replaced_after_resume(tmp_path: Path) -> None:
    root, ledger = _repo(tmp_path)
    _register(ledger, "TASK-001")
    ledger.write_checkpoint({"cursor": "before-start"})
    ledger.append_event("task.started", task_id="TASK-001")

    plan = build_resume_plan(root, FEATURE, RUN_ID)
    assert plan.checkpoint_status == "stale"

    result = resume_execution(root, FEATURE, RUN_ID)
    assert result.status == "ready"
    assert result.checkpoint_path is not None
    assert ledger.load_checkpoint() is not None


def test_corrupt_checkpoint_fails_closed(tmp_path: Path) -> None:
    root, ledger = _repo(tmp_path)
    _register(ledger, "TASK-001")
    ledger.write_checkpoint({"cursor": "valid"})
    raw = json.loads(ledger.checkpoint_path.read_text(encoding="utf-8"))
    raw["sha256"] = "sha256:" + "0" * 64
    ledger.checkpoint_path.write_text(json.dumps(raw) + "\n", encoding="utf-8")

    with pytest.raises(ExecutionLedgerError, match="checkpoint content hash mismatch"):
        build_resume_plan(root, FEATURE, RUN_ID)


def test_all_verified_tasks_produce_idempotent_nothing_to_resume(tmp_path: Path) -> None:
    root, ledger = _repo(tmp_path)
    _register(ledger, "TASK-001")
    _complete(root, ledger, "TASK-001")

    first = resume_execution(root, FEATURE, RUN_ID)
    second = resume_execution(root, FEATURE, RUN_ID)

    assert first.status == "nothing-to-resume"
    assert first.dispatch_id is None
    assert second.status == "nothing-to-resume"
    assert ledger.reconstruct().task_map()["TASK-001"].status == "completed"


def test_compare_and_append_rejects_stale_resume_writer(tmp_path: Path) -> None:
    root, ledger = _repo(tmp_path)
    initial = ledger.reconstruct().last_sha256
    ledger.append_event(
        "task.registered",
        task_id="TASK-001",
        expected_last_sha256=initial,
    )

    with pytest.raises(ExecutionLedgerError, match="SDAI-LEDGER-008"):
        ledger.append_event(
            "task.registered",
            task_id="TASK-002",
            expected_last_sha256=initial,
        )


def test_execution_cli_status_and_resume_json_are_machine_clean(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, ledger = _repo(tmp_path)
    _register(ledger, "TASK-001")

    status_code = entrypoint_main(
        ["execution", "status", FEATURE, "--run", RUN_ID, "--path", str(root), "--json"]
    )
    status_capture = capsys.readouterr()
    status_payload = json.loads(status_capture.out)
    assert status_code == 0
    assert status_capture.err == ""
    assert status_payload["apiVersion"] == "sdai.execution-resume-plan/v1"
    assert status_payload["resume_task_id"] == "TASK-001"

    resume_code = entrypoint_main(
        ["execution", "resume", FEATURE, "--run", RUN_ID, "--path", str(root), "--json"]
    )
    resume_capture = capsys.readouterr()
    resume_payload = json.loads(resume_capture.out)
    assert resume_code == 0
    assert resume_capture.err == ""
    assert resume_payload["apiVersion"] == "sdai.execution-resume-result/v1"
    assert resume_payload["status"] == "ready"
    assert resume_payload["dispatch_id"].startswith(f"dispatch:{RUN_ID}:TASK-001:1:")
