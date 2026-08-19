from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from sdai.audit_contracts import AuditProvenanceError
from sdai.audit_ledger import AuditLedger
from sdai.execution_ledger import create_execution_run, load_execution_run
from sdai.version_entrypoint import main as sdai_main
from sdai.workflow_machine_audit import audited_resume_workflow_run


FEATURE = "WF2-AUDIT-238"
BASELINE = "c" * 40


def _init(root: Path) -> None:
    config = root / ".sdai" / "config.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("version: 1\noperating_mode: individual\n", encoding="utf-8")
    feature = root / "specs" / FEATURE
    feature.mkdir(parents=True, exist_ok=True)
    (feature / "00-intake.md").write_text("# Workflow Engine 2 audit\n", encoding="utf-8")


def _workflow(
    root: Path,
    steps: list[object],
    *,
    inputs: dict[str, object] | None = None,
) -> None:
    path = root / ".sdai" / "workflows" / "engine2-audit.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {
        "version": 9,
        "name": "engine2-audit",
        "validation_mode": "standard",
        "steps": steps,
    }
    if inputs is not None:
        payload["inputs"] = inputs
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def _run(root: Path, run_id: str):
    return create_execution_run(
        root,
        FEATURE,
        "engine2-audit",
        BASELINE,
        run_id=run_id,
    )


def test_versioned_cli_audits_engine2_resume_with_hash_only_sensitive_input(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _init(tmp_path)
    _workflow(
        tmp_path,
        [{"id": "validate", "type": "validate"}],
        inputs={
            "token": {"type": "string", "required": True, "sensitive": True},
        },
    )
    run_id = "engine2-success"
    execution_ledger = _run(tmp_path, run_id)

    assert (
        sdai_main(
            [
                "workflow",
                "resume",
                FEATURE,
                "--run",
                run_id,
                "--input",
                "token=ENGINE2_SECRET_MARKER",
                "--json",
                "--path",
                str(tmp_path),
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "ENGINE2_SECRET_MARKER" not in output

    events = AuditLedger(tmp_path, FEATURE).read()
    assert [event.action.kind for event in events] == [
        "workflow.engine2.resume.started",
        "workflow.engine2.resume.completed",
    ]
    terminal = events[-1]
    sources = {binding.source for binding in terminal.bindings}
    assert "workflow-engine2/resume-start" in sources
    assert "workflow-engine2/graph" in sources
    assert "workflow-engine2/input" in sources
    assert "workflow-engine2/execution" in sources
    assert "workflow-engine2/run-status" in sources
    assert "workflow-engine2/resume-result" in sources
    assert f"specs/{FEATURE}/.sdai/execution/{run_id}/run.json" in sources
    assert f"specs/{FEATURE}/.sdai/execution/{run_id}/events.jsonl" in sources
    assert terminal.execution.run_id == run_id
    assert terminal.execution.workflow == "engine2-audit"
    assert terminal.metadata["ledgerStatus"] == "completed"
    assert b"ENGINE2_SECRET_MARKER" not in AuditLedger(tmp_path, FEATURE).export_jsonl()
    assert execution_ledger.reconstruct().status == "completed"


def test_engine2_paused_resume_has_distinct_terminal_action(tmp_path: Path) -> None:
    _init(tmp_path)
    _workflow(
        tmp_path,
        [{"id": "approval", "type": "approval", "gate": "release"}],
    )
    run_id = "engine2-paused"
    _run(tmp_path, run_id)

    result = audited_resume_workflow_run(tmp_path, FEATURE, run_id)

    assert result.execution.status.value == "paused"
    assert [event.action.kind for event in AuditLedger(tmp_path, FEATURE).read()] == [
        "workflow.engine2.resume.started",
        "workflow.engine2.resume.paused",
    ]


def test_engine2_audit_start_failure_prevents_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init(tmp_path)
    _workflow(tmp_path, [{"id": "validate", "type": "validate"}])
    run_id = "engine2-start-fail"
    _run(tmp_path, run_id)
    original_append = AuditLedger.append

    def fail_start(self, **kwargs):
        raise AuditProvenanceError("SDAI-AUDIT-TEST: forced start failure")

    monkeypatch.setattr(AuditLedger, "append", fail_start)
    with pytest.raises(AuditProvenanceError, match="forced start failure"):
        audited_resume_workflow_run(tmp_path, FEATURE, run_id)
    monkeypatch.setattr(AuditLedger, "append", original_append)

    execution = load_execution_run(tmp_path, FEATURE, run_id)
    ledger_events = execution.load_events()
    assert len(ledger_events) == 1
    assert ledger_events[0].kind == "run.created"
    assert execution.reconstruct().status == "active"


def test_engine2_terminal_audit_failure_never_reexecutes_workflow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init(tmp_path)
    _workflow(tmp_path, [{"id": "validate", "type": "validate"}])
    run_id = "engine2-terminal-fail"
    _run(tmp_path, run_id)
    original_append = AuditLedger.append
    calls = 0

    def fail_terminal(self, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise AuditProvenanceError("SDAI-AUDIT-TEST: forced terminal failure")
        return original_append(self, **kwargs)

    monkeypatch.setattr(AuditLedger, "append", fail_terminal)
    with pytest.raises(AuditProvenanceError, match="forced terminal failure"):
        audited_resume_workflow_run(tmp_path, FEATURE, run_id)
    monkeypatch.setattr(AuditLedger, "append", original_append)

    execution = load_execution_run(tmp_path, FEATURE, run_id)
    ledger_events = execution.load_events()
    assert execution.reconstruct().status == "completed"
    assert sum(event.kind == "run.completed" for event in ledger_events) == 1
    assert sum(event.kind == "task.registered" for event in ledger_events) == 1
    assert sum(event.kind == "task.completed" for event in ledger_events) == 1

    audit_events = AuditLedger(tmp_path, FEATURE).read()
    assert [event.action.kind for event in audit_events] == [
        "workflow.engine2.resume.started",
    ]


def test_engine2_failure_records_only_failure_type_not_exception_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init(tmp_path)
    _workflow(tmp_path, [{"id": "validate", "type": "validate"}])
    run_id = "engine2-runtime-fail"
    _run(tmp_path, run_id)

    from sdai import workflow_machine_audit

    def fail_resume(*args, **kwargs):
        raise RuntimeError("token=ENGINE2_EXCEPTION_SECRET")

    monkeypatch.setattr(workflow_machine_audit, "resume_workflow_run", fail_resume)
    with pytest.raises(RuntimeError, match="ENGINE2_EXCEPTION_SECRET"):
        audited_resume_workflow_run(tmp_path, FEATURE, run_id)

    events = AuditLedger(tmp_path, FEATURE).read()
    assert [event.action.kind for event in events] == [
        "workflow.engine2.resume.started",
        "workflow.engine2.resume.failed",
    ]
    assert events[-1].metadata["failureType"] == "RuntimeError"
    exported = AuditLedger(tmp_path, FEATURE).export_jsonl()
    assert b"ENGINE2_EXCEPTION_SECRET" not in exported
    assert b"token=" not in exported
