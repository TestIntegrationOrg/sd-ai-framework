from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path

import pytest

from sdai.audit_ledger import AuditLedger
from sdai.audit_provenance import AuditAction, AuditActor, AuditBinding, AuditExecution
from sdai.audit_report import AuditReportError, AuditSelectors, build_audit_report


FEATURE = "AUDIT-REPORT-240"


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")
    return path


def _workspace(root: Path, *, legacy: bool = False) -> Path:
    feature = root / "specs" / (FEATURE if legacy else f"changes/{FEATURE}")
    _write(feature / "requirements.md", "# Requirements\n\n- FR-001: Inspect audit evidence.\n")
    return feature


def _append_fixture_events(root: Path) -> tuple[object, object]:
    ledger = AuditLedger(root, FEATURE)
    first = ledger.append(
        category="ai",
        actor=AuditActor(
            "ai",
            "agent:private-agent-subject",
            semantic_role="developer",
            provider="private-provider-marker",
            model="private-model-marker",
        ),
        action=AuditAction(
            "agent.execution.succeeded",
            "feature:private-action-subject",
            reason="private-action-reason-marker",
        ),
        execution=AuditExecution(
            run_id="run-001",
            workflow="standard",
            step_id="implement",
            task_id="TASK-001",
            git_commit="a" * 40,
        ),
        bindings=(
            AuditBinding("input", "agent-invocation/prompt", "sha256:" + "1" * 64),
        ),
        metadata={"status": "succeeded", "detail": "private-metadata-marker"},
        occurred_at="2026-08-19T01:00:00Z",
    )
    second = ledger.append(
        category="workflow",
        actor=AuditActor("workflow", "workflow:standard", semantic_role="orchestrator"),
        action=AuditAction("workflow.step.completed", "step:validate"),
        execution=AuditExecution(
            run_id="run-001",
            workflow="standard",
            step_id="validate",
            task_id="TASK-002",
            git_commit="a" * 40,
        ),
        bindings=(
            AuditBinding("evidence", "workflow-engine2/run-status", "sha256:" + "2" * 64),
        ),
        metadata={"status": "completed"},
        occurred_at="2026-08-19T01:01:00Z",
    )
    return first, second


def _report_hash(body: dict[str, object]) -> str:
    unsigned = dict(body)
    unsigned.pop("reportSha256", None)
    encoded = json.dumps(
        unsigned,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + sha256(encoded).hexdigest()


def test_verified_report_is_deterministic_hash_bound_and_privacy_bounded(tmp_path: Path) -> None:
    _workspace(tmp_path)
    _append_fixture_events(tmp_path)

    first = build_audit_report(tmp_path, FEATURE)
    second = build_audit_report(tmp_path, FEATURE)

    assert first.exit_code == 0
    assert first.to_json() == second.to_json()
    body = first.body
    assert body["apiVersion"] == "sdai.audit-report/v1"
    assert body["eventCount"] == 2
    assert body["selectedCount"] == 2
    assert body["returnedCount"] == 2
    assert body["reportSha256"] == _report_hash(body)
    assert body["relationships"]["linkedReferences"] == 1
    exported = first.to_json()
    for forbidden in (
        "private-agent-subject",
        "private-provider-marker",
        "private-model-marker",
        "private-action-subject",
        "private-action-reason-marker",
        "private-metadata-marker",
    ):
        assert forbidden not in exported
    event = body["events"][0]
    assert event["actorKind"] == "ai"
    assert event["action"] == "agent.execution.succeeded"
    assert event["execution"]["stepId"] == "implement"
    assert event["bindings"][0]["source"] == "agent-invocation/prompt"


def test_selectors_use_and_semantics_and_preserve_sequence_order(tmp_path: Path) -> None:
    _workspace(tmp_path)
    first, _ = _append_fixture_events(tmp_path)

    selected = build_audit_report(
        tmp_path,
        FEATURE,
        selectors=AuditSelectors(
            category="ai",
            actor_kind="ai",
            action="agent.execution.succeeded",
            run_id="run-001",
            workflow="standard",
            step_id="implement",
            task_id="TASK-001",
            binding="input",
            status="succeeded",
        ),
    )
    assert selected.exit_code == 0
    assert selected.body["selectedCount"] == 1
    assert selected.body["events"][0]["eventId"] == first.event_id

    by_source = build_audit_report(
        tmp_path,
        FEATURE,
        selectors=AuditSelectors(binding="workflow-engine2/run-status"),
    )
    assert by_source.body["selectedCount"] == 1
    assert by_source.body["events"][0]["sequence"] == 2

    by_hash = build_audit_report(
        tmp_path,
        FEATURE,
        selectors=AuditSelectors(binding="sha256:" + "2" * 64),
    )
    assert by_hash.body["selectedCount"] == 1

    empty = build_audit_report(
        tmp_path,
        FEATURE,
        selectors=AuditSelectors(category="system", workflow="missing"),
    )
    assert empty.exit_code == 0
    assert empty.body["selectedCount"] == 0
    assert empty.body["events"] == []


def test_report_output_is_bounded_without_hiding_selected_count(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _workspace(tmp_path)
    _append_fixture_events(tmp_path)
    from sdai import audit_report

    monkeypatch.setattr(audit_report, "AUDIT_REPORT_MAX_EVENTS", 1)
    report = build_audit_report(tmp_path, FEATURE)
    assert report.body["selectedCount"] == 2
    assert report.body["returnedCount"] == 1
    assert report.body["truncated"] is True
    assert len(report.body["events"]) == 1


def test_missing_audit_bound_evidence_propagates_existing_trace_gap_and_exit_two(tmp_path: Path) -> None:
    feature = _workspace(tmp_path)
    evidence = _write(feature / "quality" / "result.md", "passed\n")
    digest = "sha256:" + sha256(evidence.read_bytes()).hexdigest()
    AuditLedger(tmp_path, FEATURE).append(
        category="evidence",
        actor=AuditActor("system", "quality-recorder"),
        action=AuditAction("quality.recorded", f"feature:{FEATURE}"),
        bindings=(
            AuditBinding("quality", evidence.relative_to(tmp_path).as_posix(), digest),
        ),
        metadata={"status": "passed"},
        occurred_at="2026-08-19T01:00:00Z",
    )
    evidence.unlink()

    report = build_audit_report(tmp_path, FEATURE)
    assert report.exit_code == 2
    gaps = report.body["relationships"]["gaps"]
    assert len(gaps) == 1
    assert gaps[0]["kind"] == "missing-audit-binding"
    assert gaps[0]["target"].endswith("quality/result.md")


def test_no_events_is_read_only_and_returns_stable_status(tmp_path: Path) -> None:
    feature = _workspace(tmp_path)
    report = build_audit_report(tmp_path, FEATURE)

    assert report.exit_code == 3
    assert report.body["status"] == "no-events"
    assert report.body["eventCount"] == 0
    assert report.body["ledgerHeadSha256"] == "sha256:" + "0" * 64
    assert not (feature / ".sdai" / "audit").exists()


def test_legacy_workspace_is_queryable_without_claiming_typed_trace_linkage(tmp_path: Path) -> None:
    _workspace(tmp_path, legacy=True)
    AuditLedger(tmp_path, FEATURE).append(
        category="system",
        actor=AuditActor("system", "legacy-test"),
        action=AuditAction("system.recorded", f"feature:{FEATURE}"),
        bindings=(
            AuditBinding("evidence", "workflow-engine2/run-status", "sha256:" + "3" * 64),
        ),
        metadata={"status": "recorded"},
        occurred_at="2026-08-19T01:00:00Z",
    )

    report = build_audit_report(tmp_path, FEATURE)
    assert report.exit_code == 0
    assert report.body["auditSource"] == f"specs/{FEATURE}/.sdai/audit/events.jsonl"
    assert report.body["relationships"]["scope"] == "legacy-audit-projection"
    assert report.body["relationships"]["linkedReferences"] == 1


def test_ambiguous_modern_and_legacy_workspaces_fail_closed(tmp_path: Path) -> None:
    _workspace(tmp_path)
    _workspace(tmp_path, legacy=True)
    with pytest.raises(AuditReportError, match="audit authority is ambiguous"):
        build_audit_report(tmp_path, FEATURE)


def test_symlinked_audit_path_is_rejected_before_no_events_response(tmp_path: Path) -> None:
    if not hasattr(Path, "symlink_to"):
        pytest.skip("symlinks unavailable")
    feature = _workspace(tmp_path)
    target = tmp_path / "outside-audit"
    target.mkdir()
    (feature / ".sdai").mkdir(parents=True, exist_ok=True)
    try:
        (feature / ".sdai" / "audit").symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation unavailable")

    with pytest.raises(AuditReportError, match="contains a symlink component"):
        build_audit_report(tmp_path, FEATURE)
