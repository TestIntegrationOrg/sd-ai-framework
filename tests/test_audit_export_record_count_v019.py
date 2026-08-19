from __future__ import annotations

from pathlib import Path

import pytest

from sdai.audit_export import (
    AuditExportError,
    AuditExportManifest,
    AuditExportPackage,
    build_audit_export_package,
)
from sdai.audit_ledger import AuditLedger
from sdai.audit_provenance import AuditAction, AuditActor


FEATURE = "AUDIT-COUNT-241"


def test_manifest_event_count_must_match_canonical_jsonl_record_count(tmp_path: Path) -> None:
    feature = tmp_path / "specs" / "changes" / FEATURE
    feature.mkdir(parents=True)
    (feature / "requirements.md").write_text(
        "# Requirements\n\n- FR-001: Bind event count to canonical export bytes.\n",
        encoding="utf-8",
    )
    AuditLedger(tmp_path, FEATURE).append(
        category="system",
        actor=AuditActor("system", "count-test"),
        action=AuditAction("export.count.recorded", f"feature:{FEATURE}"),
        metadata={"status": "recorded"},
        occurred_at="2026-08-19T06:30:00Z",
    )
    package = build_audit_export_package(tmp_path, FEATURE)
    manifest = package.manifest
    wrong = AuditExportManifest.create(
        feature_id=manifest.feature_id,
        event_count=manifest.event_count + 1,
        ledger_head_sha256=manifest.ledger_head_sha256,
        export_sha256=manifest.export_sha256,
        byte_length=manifest.byte_length,
        chunk_size=manifest.chunk_size,
        chunks=manifest.chunks,
    )

    with pytest.raises(
        AuditExportError,
        match="eventCount does not match canonical JSONL record count",
    ):
        AuditExportPackage(wrong, package.chunk_bytes)
