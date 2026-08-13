from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest

from sdai.artifact_state import record_artifact_state
from sdai.debug_records import complete_debugger_task, register_debugger_task
from sdai.entrypoint import main as entrypoint_main
from sdai.execution_ledger import ExecutionLedgerError, create_execution_run, load_execution_run
from sdai.execution_resume import build_resume_plan, resume_execution


ANALYZE_FEATURE = "V09-ANALYZE"
EXEC_FEATURE = "V09-EXEC"
DEBUG_FEATURE = "V09-DEBUG"
REQUIRED_ANALYSIS_CODES = {
    "ORPHAN_REQUIREMENT",
    "ORPHAN_TASK",
    "MISSING_NFR",
    "ARCHITECTURE_CONFLICT",
    "CONTRACT_CONFLICT",
    "UNRESOLVED_ADR",
    "UNTESTED_SCENARIO",
    "UNAPPROVED_BREAKING_CHANGE",
    "UNMITIGATED_THREAT",
    "STALE_ARTIFACT",
}


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")
    return path


def _snapshot_files(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def _broken_analysis_workspace(root: Path) -> None:
    _write(root / ".sdai" / "config.yaml", "project: v09-release-gate\n")
    feature = root / "specs" / "changes" / ANALYZE_FEATURE
    requirements = _write(
        feature / "requirements.md",
        """# Requirements

- FR-001: Sign a script without an implementation task.
- AC-001: Valid request returns a signature.
""",
    )
    _write(feature / "tasks.md", "# Tasks\n\n- TASK-001: Implement an unrelated migration.\n")
    _write(feature / "adr" / "one.md", "# ADR-001: Use AWS KMS\nstatus: proposed\n")
    _write(feature / "adr" / "two.md", "# ADR-001: Store key locally\nstatus: accepted\n")
    _write(
        feature / "contracts" / "one.yaml",
        "id: CONTRACT-001\nstatus: breaking\nreferences: [APPROVAL-001]\n",
    )
    _write(feature / "contracts" / "two.yaml", "id: CONTRACT-001\nstatus: proposed\n")
    _write(
        feature / "approvals" / "contract.yaml",
        "approval_id: APPROVAL-001\nstatus: pending\nreferences: [CONTRACT-001]\n",
    )
    _write(
        feature / "security" / "threats.yaml",
        """threat_id: THREAT-001
status: open
references: [MITIGATION-001]

mitigation_id: MITIGATION-001
status: planned
references: [THREAT-001]
""",
    )
    record_artifact_state(
        root,
        ANALYZE_FEATURE,
        "requirements",
        risk="standard",
        environ={},
    )
    requirements.write_text(
        requirements.read_text(encoding="utf-8") + "\nChanged after validation café Δ.\n",
        encoding="utf-8",
        newline="\n",
    )


def _git_executable() -> str:
    executable = shutil.which("git")
    if not executable:
        pytest.skip("git is required for the 0.9 release gate")
    return executable


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        [_git_executable(), *args],
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


def _git_output(root: Path, *args: str) -> str:
    return (_git(root, *args).stdout or "").strip()


def _execution_repo(tmp_path: Path, feature: str = EXEC_FEATURE, run_id: str = "run-v09"):
    root = tmp_path / "0.9 Release Ω workspace"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "SDAI 0.9 Release Gate")
    _git(root, "config", "user.email", "sdai-v09@example.invalid")
    _git(root, "config", "core.autocrlf", "false")
    _write(root / ".sdai" / "config.yaml", "project: v09-release-gate\n")
    _write(root / "specs" / feature / "00-intake.md", f"# {feature} café Δ\n")
    _write(root / "README.md", "# 0.9 release gate Ω\n")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "0.9 verified baseline")
    baseline = _git_output(root, "rev-parse", "HEAD")
    ledger = create_execution_run(
        root,
        feature,
        "enterprise",
        baseline,
        run_id=run_id,
    )
    return root, ledger


def _register_tasks(ledger, *task_ids: str) -> None:
    for task_id in task_ids:
        ledger.append_event("task.registered", task_id=task_id, payload={"title": task_id})


def _complete_task(root: Path, ledger, task_id: str) -> tuple[Path, str]:
    ledger.append_event("task.started", task_id=task_id)
    artifact = root / "src" / f"{task_id}.txt"
    _write(artifact, f"implementation for {task_id} café Δ\n")
    _git(root, "add", artifact.relative_to(root).as_posix())
    _git(root, "commit", "-m", f"complete {task_id}")
    commit = _git_output(root, "rev-parse", "HEAD")
    artifact_binding = ledger.binding_for_file(artifact, kind="artifact")
    evidence_binding = ledger.write_task_record(
        task_id,
        "evidence",
        {"verification": "passed", "task": task_id},
    )
    ledger.append_event(
        "task.completed",
        task_id=task_id,
        git_commit=commit,
        bindings=(artifact_binding, evidence_binding),
    )
    return artifact, commit


def _debug_record(run_id: str, task_id: str) -> dict[str, object]:
    return {
        "apiVersion": "sdai.debug-record/v1",
        "feature_id": DEBUG_FEATURE,
        "run_id": run_id,
        "task_id": task_id,
        "semantic_role": "debugger",
        "status": "fixed",
        "reproduction": {
            "steps": ["Run the tenant-switch request twice against one cache process."],
            "observed": "Second tenant receives the first tenant cached result.",
            "expected": "Each tenant receives only its own result.",
        },
        "observations": [
            {
                "id": "OBS_CACHE",
                "fact": "Cache key omits tenant id.",
                "source": "cache lookup boundary log",
            }
        ],
        "hypotheses": [
            {
                "id": "HYP_CACHE",
                "statement": "Missing tenant identity causes cross-tenant cache reuse.",
                "status": "supported",
                "observation_ids": ["OBS_CACHE"],
            }
        ],
        "experiments": [
            {
                "id": "EXP_CACHE",
                "hypothesis_id": "HYP_CACHE",
                "action": "Add tenant identity to an instrumented cache key and rerun.",
                "result": "Cross-tenant reuse disappears.",
                "conclusion": "supports",
            }
        ],
        "root_cause": {
            "statement": "The cache key omitted tenant identity.",
            "evidence_ids": ["OBS_CACHE", "EXP_CACHE"],
            "confidence": "confirmed",
        },
        "fix": {
            "summary": "Key cached values by tenant and resource.",
            "files": ["src/cache.py"],
        },
        "regression_evidence": [
            {
                "id": "REG_CACHE",
                "command": "pytest -q tests/test_cache.py::test_tenant_switch",
                "result": "1 passed",
                "status": "passed",
            }
        ],
        "producer": {"agent": "debugger", "provider": "codex", "model": "release-gate"},
    }


def test_v09_analyze_cli_is_read_only_and_surfaces_all_required_finding_families(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "Analyze Release Ω"
    _broken_analysis_workspace(root)
    before = _snapshot_files(root)

    code = entrypoint_main(
        ["analyze", ANALYZE_FEATURE, "--path", str(root), "--json"]
    )
    capture = capsys.readouterr()
    payload = json.loads(capture.out)

    assert code == 2
    assert capture.err == ""
    assert payload["apiVersion"] == "sdai.findings/v1"
    assert REQUIRED_ANALYSIS_CODES <= {item["code"] for item in payload["findings"]}
    assert _snapshot_files(root) == before
    assert all(item["evidence"] for item in payload["findings"])


def test_v09_interrupted_multitask_run_resumes_exact_task_without_duplicate_dispatch(
    tmp_path: Path,
) -> None:
    root, ledger = _execution_repo(tmp_path)
    _register_tasks(ledger, "TASK-020", "TASK-010", "TASK-030")
    _complete_task(root, ledger, "TASK-020")

    first = resume_execution(root, EXEC_FEATURE, "run-v09")
    assert first.status == "ready"
    assert first.plan.resume_task_id == "TASK-010"
    assert first.dispatch_id is not None
    ledger.append_event("task.started", task_id="TASK-010")

    resumed = resume_execution(root, EXEC_FEATURE, "run-v09")
    events = ledger.load_events()
    reservations = [item for item in events if item.kind == "task.dispatch_reserved"]

    assert resumed.plan.tasks[0].action == "skip"
    assert resumed.plan.resume_task_id == "TASK-010"
    assert resumed.dispatch_id == first.dispatch_id
    assert resumed.dispatch_reused is True
    assert len([item for item in reservations if item.task_id == "TASK-010"]) == 1
    assert not any(item.task_id == "TASK-030" for item in reservations)


def test_v09_completed_task_becomes_non_skippable_when_bound_artifact_changes(
    tmp_path: Path,
) -> None:
    root, ledger = _execution_repo(tmp_path, run_id="run-stale")
    _register_tasks(ledger, "TASK-001", "TASK-002")
    artifact, completed_commit = _complete_task(root, ledger, "TASK-001")

    initial = build_resume_plan(root, EXEC_FEATURE, "run-stale")
    assert initial.tasks[0].action == "skip"
    assert initial.tasks[0].git_reachable is True
    assert initial.current_head == completed_commit
    assert initial.resume_task_id == "TASK-002"

    artifact.write_text("implementation changed after completion Ω\n", encoding="utf-8", newline="\n")
    _git(root, "add", artifact.relative_to(root).as_posix())
    _git(root, "commit", "-m", "change completed task artifact")

    changed = build_resume_plan(root, EXEC_FEATURE, "run-stale")
    assert changed.tasks[0].action == "reopen"
    assert changed.tasks[0].skip_verified is False
    assert changed.tasks[0].git_reachable is True
    assert "binding_hash_mismatch" in changed.tasks[0].reasons
    assert changed.resume_task_id == "TASK-001"


def test_v09_truncated_ledger_fails_closed_instead_of_reconstructing_completion(
    tmp_path: Path,
) -> None:
    root, ledger = _execution_repo(tmp_path, run_id="run-corrupt")
    _register_tasks(ledger, "TASK-001")
    _complete_task(root, ledger, "TASK-001")
    assert ledger.reconstruct().task_map()["TASK-001"].status == "completed"

    raw = ledger.events_path.read_bytes()
    assert raw.endswith(b"\n")
    ledger.events_path.write_bytes(raw[:-1])

    with pytest.raises(ExecutionLedgerError, match="truncated/incomplete"):
        load_execution_run(root, EXEC_FEATURE, "run-corrupt")
    with pytest.raises(ExecutionLedgerError, match="truncated/incomplete"):
        ledger.reconstruct()


def test_v09_debugger_requires_confirmed_root_cause_and_regression_evidence(
    tmp_path: Path,
) -> None:
    root, ledger = _execution_repo(tmp_path, feature=DEBUG_FEATURE, run_id="run-debug")
    register_debugger_task(ledger, "TASK-DEBUG", title="Diagnose tenant cache defect")
    ledger.append_event("task.started", task_id="TASK-DEBUG")
    artifact = _write(root / "src" / "cache.py", "cache key = tenant + resource\n")
    _git(root, "add", artifact.relative_to(root).as_posix())
    _git(root, "commit", "-m", "fix cache key")
    commit = _git_output(root, "rev-parse", "HEAD")
    artifact_binding = ledger.binding_for_file(artifact, kind="artifact")

    with pytest.raises(ExecutionLedgerError, match="SDAI-LEDGER-009"):
        ledger.append_event(
            "task.completed",
            task_id="TASK-DEBUG",
            git_commit=commit,
            bindings=(artifact_binding,),
        )

    evidence = complete_debugger_task(
        ledger,
        "TASK-DEBUG",
        commit,
        _debug_record("run-debug", "TASK-DEBUG"),
        artifact_bindings=(artifact_binding,),
    )
    events = ledger.load_events()
    state = ledger.reconstruct().task_map()["TASK-DEBUG"]

    assert evidence.completion_ready is True
    assert evidence.record["root_cause"]["confidence"] == "confirmed"  # type: ignore[index]
    assert evidence.record["regression_evidence"][0]["status"] == "passed"  # type: ignore[index]
    assert state.status == "completed"
    assert [item.kind for item in events].index("task.evidence") < [item.kind for item in events].index("task.completed")
    assert evidence.binding in state.bindings


def test_v09_keeps_all_previous_release_compatibility_gates_enabled() -> None:
    tests = Path(__file__).resolve().parent
    for name in (
        "test_v06_release_compatibility.py",
        "test_v07_release_compatibility.py",
        "test_v08_release_compatibility.py",
    ):
        path = tests / name
        assert path.is_file(), f"previous compatibility gate was removed: {name}"
        assert path.read_text(encoding="utf-8").strip(), f"previous compatibility gate is empty: {name}"
