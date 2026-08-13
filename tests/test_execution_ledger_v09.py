from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import sdai.execution_ledger as ledger_module
from sdai.execution_ledger import (
    ExecutionLedgerError,
    HashBinding,
    create_execution_run,
    load_execution_run,
)


FEATURE = "LEDGER-100"
BASELINE = "a" * 40
IMPLEMENTED = "b" * 40


def _feature(root: Path) -> Path:
    feature = root / "specs" / FEATURE
    feature.mkdir(parents=True)
    (feature / "00-intake.md").write_text("# Ledger feature café Δ\n", encoding="utf-8")
    return feature


def _ledger(root: Path, *, run_id: str = "run-test"):
    _feature(root)
    return create_execution_run(root, FEATURE, "enterprise", BASELINE, run_id=run_id)


def _start_task(ledger, task_id: str = "TASK-001") -> None:
    ledger.append_event("task.registered", task_id=task_id, payload={"title": "Implement café Δ"})
    ledger.append_event("task.started", task_id=task_id)


def _complete_task(ledger, root: Path, task_id: str = "TASK-001") -> None:
    output = root / "specs" / FEATURE / f"{task_id}.txt"
    output.write_text(f"completed {task_id} Ω\n", encoding="utf-8")
    artifact = ledger.binding_for_file(output, kind="artifact")
    evidence = ledger.write_task_record(task_id, "evidence", {"tests": "passed", "count": 42})
    ledger.append_event(
        "task.completed",
        task_id=task_id,
        git_commit=IMPLEMENTED,
        bindings=(artifact, evidence),
    )


def test_run_creation_is_provider_independent_and_manifest_bound(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)

    events = ledger.load_events()
    state = ledger.reconstruct()

    assert len(events) == 1
    assert events[0].kind == "run.created"
    assert events[0].sequence == 1
    assert events[0].event_id == "run-test:00000001"
    assert events[0].git_commit == BASELINE
    assert events[0].payload["workflow"] == "enterprise"
    assert events[0].bindings[0].source.endswith("/.sdai/execution/run-test/run.json")
    assert state.status == "active"
    assert state.last_sequence == 1
    assert "provider" not in ledger.manifest_path.read_text(encoding="utf-8").casefold()
    assert "model" not in ledger.manifest_path.read_text(encoding="utf-8").casefold()

    reloaded = load_execution_run(tmp_path, FEATURE, "run-test")
    assert reloaded.reconstruct() == state


def test_task_records_and_completion_reconstruct_after_restart(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    _start_task(ledger)
    brief = ledger.write_task_brief("TASK-001", "Implement the exact change.\r\nVerify it.\r\n")
    implementation = ledger.write_task_record(
        "TASK-001",
        "implementation",
        {"files": ["src/service.py"], "summary": "implemented Ω"},
    )
    review = ledger.write_task_record(
        "TASK-001",
        "review",
        {"status": "approved", "reviewer": "code-reviewer"},
    )
    ledger.append_event("task.implementation", task_id="TASK-001", bindings=(implementation,))
    ledger.append_event("task.review", task_id="TASK-001", bindings=(review,))
    ledger.append_event("task.evidence", task_id="TASK-001", bindings=(brief,))
    _complete_task(ledger, tmp_path)
    ledger.append_event("run.completed", git_commit=IMPLEMENTED)

    reloaded = load_execution_run(tmp_path, FEATURE, "run-test")
    state = reloaded.reconstruct()
    task = state.task_map()["TASK-001"]

    assert state.status == "completed"
    assert task.status == "completed"
    assert task.git_commit == IMPLEMENTED
    assert task.terminal_event_id is not None
    assert {binding.kind for binding in task.bindings} == {"artifact", "evidence"}
    paths = reloaded.task_record_paths("TASK-001")
    assert paths["brief"].read_text(encoding="utf-8") == "Implement the exact change.\nVerify it.\n"
    assert json.loads(paths["implementation"].read_text(encoding="utf-8"))["payload"]["summary"] == "implemented Ω"
    assert json.loads(paths["review"].read_text(encoding="utf-8"))["payload"]["status"] == "approved"
    assert json.loads(paths["evidence"].read_text(encoding="utf-8"))["payload"]["tests"] == "passed"


def test_completion_requires_git_and_persistent_hash_binding(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    _start_task(ledger)

    with pytest.raises(ExecutionLedgerError, match="requires a Git commit"):
        ledger.append_event("task.completed", task_id="TASK-001", bindings=(HashBinding("artifact", "x.txt", "sha256:" + "1" * 64),))
    with pytest.raises(ExecutionLedgerError, match="at least one artifact/evidence"):
        ledger.append_event("task.completed", task_id="TASK-001", git_commit=IMPLEMENTED)

    assert ledger.reconstruct().task_map()["TASK-001"].status == "started"


def test_duplicate_or_conflicting_terminal_task_events_are_rejected(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    _start_task(ledger)
    _complete_task(ledger, tmp_path)
    before = ledger.events_path.read_bytes()

    with pytest.raises(ExecutionLedgerError, match="already terminal"):
        ledger.append_event(
            "task.completed",
            task_id="TASK-001",
            git_commit=IMPLEMENTED,
            bindings=(ledger.binding_for_file(tmp_path / "specs" / FEATURE / "TASK-001.txt"),),
        )
    with pytest.raises(ExecutionLedgerError, match="already terminal"):
        ledger.append_event("task.failed", task_id="TASK-001")

    assert ledger.events_path.read_bytes() == before


def test_run_cannot_complete_with_incomplete_task_and_is_terminal_once_completed(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    _start_task(ledger)

    with pytest.raises(ExecutionLedgerError, match="tasks are incomplete"):
        ledger.append_event("run.completed", git_commit=IMPLEMENTED)

    _complete_task(ledger, tmp_path)
    ledger.append_event("run.completed", git_commit=IMPLEMENTED)
    with pytest.raises(ExecutionLedgerError, match="run is already terminal"):
        ledger.append_event("run.failed")


def test_pause_requires_explicit_resume_before_task_activity(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    ledger.append_event("run.paused", payload={"reason": "human gate"})

    with pytest.raises(ExecutionLedgerError, match="must be resumed"):
        ledger.append_event("task.registered", task_id="TASK-001")

    ledger.append_event("run.resumed")
    ledger.append_event("task.registered", task_id="TASK-001")
    assert ledger.reconstruct().status == "active"


def test_jsonl_append_preserves_exact_prefix_and_monotonic_hash_chain(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    prefix = ledger.events_path.read_bytes()
    ledger.append_event("task.registered", task_id="TASK-001")
    after_registered = ledger.events_path.read_bytes()
    ledger.append_event("task.started", task_id="TASK-001")
    final = ledger.events_path.read_bytes()

    assert after_registered.startswith(prefix)
    assert final.startswith(after_registered)
    events = ledger.load_events()
    assert [event.sequence for event in events] == [1, 2, 3]
    assert events[1].previous_sha256 == events[0].sha256
    assert events[2].previous_sha256 == events[1].sha256


def test_partial_os_writes_are_completed_before_fsync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = _ledger(tmp_path)
    real_write = ledger_module.os.write
    calls = 0

    def partial_write(fd: int, data) -> int:
        nonlocal calls
        calls += 1
        view = memoryview(data)
        size = max(1, len(view) // 3)
        return real_write(fd, view[:size])

    monkeypatch.setattr(ledger_module.os, "write", partial_write)
    ledger.append_event("task.registered", task_id="TASK-001")

    assert calls > 1
    assert [event.sequence for event in ledger.load_events()] == [1, 2]


def test_truncated_final_record_fails_closed_and_never_reconstructs_complete(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    _start_task(ledger)
    _complete_task(ledger, tmp_path)
    ledger.events_path.write_bytes(ledger.events_path.read_bytes() + b'{"apiVersion":"sdai.execution-event/v1"')

    with pytest.raises(ExecutionLedgerError, match="truncated/incomplete"):
        ledger.reconstruct()
    with pytest.raises(ExecutionLedgerError, match="truncated/incomplete"):
        load_execution_run(tmp_path, FEATURE, "run-test")


def test_tampered_event_content_hash_fails_closed(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    ledger.append_event("task.registered", task_id="TASK-001", payload={"title": "original"})
    lines = ledger.events_path.read_text(encoding="utf-8").splitlines()
    second = json.loads(lines[1])
    second["payload"]["title"] = "tampered"
    lines[1] = json.dumps(second, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    ledger.events_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(ExecutionLedgerError, match="content hash mismatch"):
        ledger.reconstruct()


def test_manifest_tampering_is_detected_against_run_created_binding(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    payload = json.loads(ledger.manifest_path.read_text(encoding="utf-8"))
    payload["workflow"] = "standard"
    ledger.manifest_path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")

    with pytest.raises(ExecutionLedgerError, match="run.json byte identity"):
        load_execution_run(tmp_path, FEATURE, "run-test")


def test_checkpoint_is_atomic_self_hashed_and_rejected_when_stale_or_tampered(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    ledger.append_event("task.registered", task_id="TASK-001")
    checkpoint = ledger.write_checkpoint({"cursor": "TASK-001", "note": "café Δ"})

    loaded = ledger.load_checkpoint()
    assert loaded == checkpoint
    assert checkpoint["sha256"].startswith("sha256:")
    assert not list(ledger.run_dir.glob(".checkpoint.json.*.tmp"))

    ledger.append_event("task.started", task_id="TASK-001")
    with pytest.raises(ExecutionLedgerError, match="checkpoint is stale"):
        ledger.load_checkpoint()

    ledger.write_checkpoint({"cursor": "TASK-001"})
    payload = json.loads(ledger.checkpoint_path.read_text(encoding="utf-8"))
    payload["extra"]["cursor"] = "TASK-999"
    ledger.checkpoint_path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    with pytest.raises(ExecutionLedgerError, match="checkpoint content hash mismatch"):
        ledger.load_checkpoint()


def test_preexisting_cross_process_lock_blocks_append_without_modifying_ledger(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    before = ledger.events_path.read_bytes()
    ledger.lock_path.write_text("pid=999999\n", encoding="utf-8")

    with pytest.raises(ExecutionLedgerError, match="locked by another process"):
        ledger.append_event("task.registered", task_id="TASK-001")

    assert ledger.events_path.read_bytes() == before
    ledger.lock_path.unlink()


def test_non_finite_or_non_json_payloads_fail_before_append(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    before = ledger.events_path.read_bytes()

    with pytest.raises(ExecutionLedgerError, match="non-finite"):
        ledger.append_event("run.paused", payload={"value": float("nan")})
    with pytest.raises(ExecutionLedgerError, match="unsupported type"):
        ledger.append_event("run.paused", payload={"value": ("tuple",)})

    assert ledger.events_path.read_bytes() == before


def test_task_record_path_symlink_escape_is_rejected(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    ledger.append_event("task.registered", task_id="TASK-001")
    outside = tmp_path / "outside"
    outside.mkdir()
    task_root = ledger.run_dir / "tasks"
    task_root.mkdir()
    link = task_root / "TASK-001"
    try:
        os.symlink(outside, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is unavailable")

    with pytest.raises((ExecutionLedgerError, RuntimeError), match="symlink|inside the project"):
        ledger.write_task_brief("TASK-001", "must not escape")


def test_run_ids_are_unique_and_paths_remain_portable_with_unicode_feature_content(tmp_path: Path) -> None:
    _feature(tmp_path)
    first = create_execution_run(tmp_path, FEATURE, "enterprise", BASELINE)
    second = create_execution_run(tmp_path, FEATURE, "enterprise", BASELINE)

    assert first.manifest.run_id != second.manifest.run_id
    for ledger in (first, second):
        relative = ledger.run_dir.relative_to(tmp_path).as_posix()
        assert "\\" not in relative
        assert relative.startswith(f"specs/{FEATURE}/.sdai/execution/run-")
