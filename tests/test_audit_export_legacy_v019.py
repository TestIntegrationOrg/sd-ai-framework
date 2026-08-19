from __future__ import annotations

from pathlib import Path

from sdai.audit_export import build_audit_export_package
from sdai.audit_ledger import AuditLedger
from sdai.audit_provenance import AuditAction, AuditActor
from sdai.audit_sinks import LocalFilesystemAuditSink, handoff_audit_export


FEATURE = "AUDIT-LEGACY-241"


def test_legacy_feature_workspace_exports_and_handoffs_without_migration(tmp_path: Path) -> None:
    feature = tmp_path / "specs" / FEATURE
    feature.mkdir(parents=True)
    (feature / "requirements.md").write_text(
        "# Requirements\n\n- FR-001: Preserve legacy audit export compatibility.\n",
        encoding="utf-8",
    )
    ledger = AuditLedger(tmp_path, FEATURE)
    ledger.append(
        category="system",
        actor=AuditActor("system", "legacy-export-test"),
        action=AuditAction("legacy.export.recorded", f"feature:{FEATURE}"),
        metadata={"status": "recorded"},
        occurred_at="2026-08-19T07:30:00Z",
    )
    before = ledger.export_jsonl()

    package = build_audit_export_package(tmp_path, FEATURE)
    sink = LocalFilesystemAuditSink(tmp_path / "retention")
    receipt = handoff_audit_export(tmp_path, FEATURE, sink, package=package)

    assert package.manifest.feature_id == FEATURE
    assert package.manifest.event_count == 1
    assert receipt.status == "accepted"
    assert receipt.export_id == package.manifest.export_id
    assert AuditLedger(tmp_path, FEATURE).export_jsonl() == before
    assert not (tmp_path / "specs" / "changes" / FEATURE).exists()
