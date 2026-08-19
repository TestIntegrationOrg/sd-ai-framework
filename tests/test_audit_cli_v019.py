from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path

import pytest

from sdai.audit_ledger import AuditLedger
from sdai.audit_provenance import AuditAction, AuditActor, AuditBinding, AuditExecution
from sdai.version_entrypoint import main as sdai_main


FEATURE = "AUDIT-CLI-240"


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")
    return path


def _feature(root: Path) -> Path:
    feature = root / "specs" / "changes" / FEATURE
    _write(feature / "requirements.md", "# Requirements\n\n- FR-001: Query audit safely.\n")
    return feature


def _event(root: Path) -> object:
    return AuditLedger(root, FEATURE).append(
        category="ai",
        actor=AuditActor(
            "ai",
            "agent:CLI_PRIVATE_SUBJECT",
            semantic_role="developer",
            provider="CLI_PRIVATE_PROVIDER",
            model="CLI_PRIVATE_MODEL",
        ),
        action=AuditAction(
            "agent.execution.succeeded",
            "feature:CLI_PRIVATE_ACTION_SUBJECT",
            reason="CLI_PRIVATE_REASON",
        ),
        execution=AuditExecution(
            run_id="run-cli",
            workflow="standard",
            step_id="implement",
            task_id="TASK-CLI",
            git_commit="b" * 40,
        ),
        bindings=(
            AuditBinding("output", "agent-invocation/output", "sha256:" + "4" * 64),
        ),
        metadata={"status": "succeeded", "detail": "CLI_PRIVATE_METADATA"},
        occurred_at="2026-08-19T02:00:00Z",
    )


def test_versioned_audit_json_is_stable_bounded_and_private_content_free(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _feature(tmp_path)
    event = _event(tmp_path)
    args = [
        "audit",
        FEATURE,
        "--json",
        "--action",
        "agent.execution.succeeded",
        "--run",
        "run-cli",
        "--step",
        "implement",
        "--binding",
        "output",
        "--status",
        "succeeded",
        "--path",
        str(tmp_path),
    ]

    assert sdai_main(args) == 0
    first = capsys.readouterr().out
    assert sdai_main(args) == 0
    second = capsys.readouterr().out
    assert first == second
    payload = json.loads(first)
    assert payload["apiVersion"] == "sdai.audit-report/v1"
    assert payload["selectedCount"] == 1
    assert payload["events"][0]["eventId"] == event.event_id
    assert payload["events"][0]["actorKind"] == "ai"
    for forbidden in (
        "CLI_PRIVATE_SUBJECT",
        "CLI_PRIVATE_PROVIDER",
        "CLI_PRIVATE_MODEL",
        "CLI_PRIVATE_ACTION_SUBJECT",
        "CLI_PRIVATE_REASON",
        "CLI_PRIVATE_METADATA",
    ):
        assert forbidden not in first


def test_invalid_selector_returns_versioned_json_input_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _feature(tmp_path)
    code = sdai_main(
        ["audit", FEATURE, "--json", "--category", "not-a-category", "--path", str(tmp_path)]
    )
    output = capsys.readouterr().out
    assert code == 4
    payload = json.loads(output)
    assert payload["apiVersion"] == "sdai.audit-error/v1"
    assert payload["category"] == "input"
    assert payload["error"]["code"] == "SDAI-AUDIT-CLI-002"
    assert payload["errorSha256"].startswith("sha256:")


def test_unknown_argument_uses_stable_input_exit_not_argparse_system_exit(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _feature(tmp_path)
    code = sdai_main(["audit", FEATURE, "--json", "--unknown", "x", "--path", str(tmp_path)])
    payload = json.loads(capsys.readouterr().out)
    assert code == 4
    assert payload["error"]["code"] == "SDAI-AUDIT-CLI-001"


def test_tampered_ledger_returns_integrity_exit_without_partial_event_output(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    feature = _feature(tmp_path)
    _event(tmp_path)
    events = feature / ".sdai" / "audit" / "events.jsonl"
    content = events.read_text(encoding="utf-8")
    events.write_text(
        content.replace("agent.execution.succeeded", "agent.execution.succeedex", 1),
        encoding="utf-8",
        newline="\n",
    )

    code = sdai_main(["audit", FEATURE, "--json", "--path", str(tmp_path)])
    output = capsys.readouterr().out
    assert code == 5
    payload = json.loads(output)
    assert payload["apiVersion"] == "sdai.audit-error/v1"
    assert payload["category"] == "integrity"
    assert payload["error"]["code"].startswith("SDAI-AUDIT-")
    assert "eventId" not in output
    assert "agent.execution.succeedex" not in output


def test_no_events_returns_exit_three_without_creating_audit_directory(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    feature = _feature(tmp_path)
    code = sdai_main(["audit", FEATURE, "--json", "--path", str(tmp_path)])
    payload = json.loads(capsys.readouterr().out)
    assert code == 3
    assert payload["status"] == "no-events"
    assert payload["eventCount"] == 0
    assert not (feature / ".sdai" / "audit").exists()


def test_stale_or_missing_audit_binding_returns_exit_two_with_trace_gap(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    feature = _feature(tmp_path)
    report = _write(feature / "quality" / "result.md", "passed\n")
    digest = "sha256:" + sha256(report.read_bytes()).hexdigest()
    AuditLedger(tmp_path, FEATURE).append(
        category="evidence",
        actor=AuditActor("system", "quality-recorder"),
        action=AuditAction("quality.recorded", f"feature:{FEATURE}"),
        bindings=(
            AuditBinding("quality", report.relative_to(tmp_path).as_posix(), digest),
        ),
        metadata={"status": "passed"},
        occurred_at="2026-08-19T02:00:00Z",
    )
    report.unlink()

    code = sdai_main(["audit", FEATURE, "--json", "--path", str(tmp_path)])
    payload = json.loads(capsys.readouterr().out)
    assert code == 2
    assert payload["status"] == "verified"
    assert payload["relationships"]["gaps"][0]["kind"] == "missing-audit-binding"


def test_human_output_is_concise_and_omits_private_content(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _feature(tmp_path)
    _event(tmp_path)
    assert sdai_main(["audit", FEATURE, "--path", str(tmp_path)]) == 0
    output = capsys.readouterr().out
    assert f"Audit {FEATURE} status=verified" in output
    assert "action=agent.execution.succeeded" in output
    assert "actor=ai" in output
    assert "CLI_PRIVATE_PROVIDER" not in output
    assert "CLI_PRIVATE_SUBJECT" not in output


def test_ledger_change_during_query_maps_to_integrity_exit(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _feature(tmp_path)
    _event(tmp_path)
    original_read = AuditLedger.read

    def inconsistent_read(self):
        events = original_read(self)
        return events[:-1]

    monkeypatch.setattr(AuditLedger, "read", inconsistent_read)
    code = sdai_main(["audit", FEATURE, "--json", "--path", str(tmp_path)])
    payload = json.loads(capsys.readouterr().out)
    assert code == 5
    assert payload["error"]["code"] == "SDAI-AUDIT-REPORT-004"
