from __future__ import annotations

from pathlib import Path

import pytest

from sdai.audit_ledger import AuditLedger
from sdai.audit_provenance import AuditAction, AuditActor
from sdai.audit_report import AuditReportError, build_audit_report


FEATURE = "AUDIT-SNAPSHOT-240"


def test_relationship_time_append_invalidates_report_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    feature = tmp_path / "specs" / "changes" / FEATURE
    feature.mkdir(parents=True)
    (feature / "requirements.md").write_text(
        "# Requirements\n\n- FR-001: Report one audit snapshot.\n",
        encoding="utf-8",
    )
    ledger = AuditLedger(tmp_path, FEATURE)
    ledger.append(
        category="system",
        actor=AuditActor("system", "snapshot-test"),
        action=AuditAction("snapshot.started", f"feature:{FEATURE}"),
        metadata={"status": "recorded"},
        occurred_at="2026-08-19T03:00:00Z",
    )

    from sdai import audit_report

    original = audit_report._relationship_summary

    def mutate_during_relationship(root, workspace, feature_id, selected):
        result = original(root, workspace, feature_id, selected)
        AuditLedger(root, feature_id).append(
            category="system",
            actor=AuditActor("system", "snapshot-test"),
            action=AuditAction("snapshot.changed", f"feature:{feature_id}"),
            metadata={"status": "recorded"},
            occurred_at="2026-08-19T03:01:00Z",
        )
        return result

    monkeypatch.setattr(audit_report, "_relationship_summary", mutate_during_relationship)

    with pytest.raises(AuditReportError, match="SDAI-AUDIT-REPORT-004"):
        build_audit_report(tmp_path, FEATURE)
