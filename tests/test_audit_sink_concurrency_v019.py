from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from sdai.audit_export import build_audit_export_package
from sdai.audit_ledger import AuditLedger
from sdai.audit_provenance import AuditAction, AuditActor
from sdai.audit_sinks import AuditSinkError, LocalFilesystemAuditSink, handoff_audit_export


FEATURE = "AUDIT-SINK-RACE-241"


def _setup(root: Path) -> None:
    feature = root / "specs" / "changes" / FEATURE
    feature.mkdir(parents=True)
    (feature / "requirements.md").write_text(
        "# Requirements\n\n- FR-001: Publish immutable audit evidence concurrently.\n",
        encoding="utf-8",
    )
    for index in range(6):
        AuditLedger(root, FEATURE).append(
            category="system",
            actor=AuditActor("system", "race-test"),
            action=AuditAction("sink.race.recorded", f"item:{index}"),
            metadata={"status": "recorded"},
            occurred_at=f"2026-08-19T07:{index:02d}:00Z",
        )


def test_two_simultaneous_handoffs_publish_one_identical_export(tmp_path: Path) -> None:
    _setup(tmp_path)
    package = build_audit_export_package(tmp_path, FEATURE, chunk_size=1024)
    sink = LocalFilesystemAuditSink(tmp_path / "retention")

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(handoff_audit_export, tmp_path, FEATURE, sink, package=package)
            for _ in range(2)
        ]
        receipts = [future.result() for future in futures]

    assert sorted(receipt.status for receipt in receipts) == ["accepted", "already-present"]
    assert len({receipt.receipt_id for receipt in receipts}) == 1
    assert len({receipt.export_id for receipt in receipts}) == 1
    final = sink.destination / package.manifest.export_id
    assert (final / "manifest.json").read_bytes() == package.manifest.to_json().encode("utf-8")
    for descriptor, content in package.iter_chunks():
        assert (final / descriptor.name).read_bytes() == content
    assert not any(path.name.startswith(".partial-") for path in sink.destination.iterdir())


def test_semantically_equivalent_but_noncanonical_existing_manifest_is_rejected(tmp_path: Path) -> None:
    _setup(tmp_path)
    package = build_audit_export_package(tmp_path, FEATURE)
    sink = LocalFilesystemAuditSink(tmp_path / "retention")
    handoff_audit_export(tmp_path, FEATURE, sink, package=package)
    manifest_path = sink.destination / package.manifest.export_id / "manifest.json"
    parsed = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_path.write_text(json.dumps(parsed, indent=2) + "\n", encoding="utf-8")

    with pytest.raises(AuditSinkError, match="not canonical JSON"):
        handoff_audit_export(tmp_path, FEATURE, sink, package=package)
