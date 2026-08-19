from __future__ import annotations

from pathlib import Path

import pytest

from sdai.audit_export import AuditExportPackage, build_audit_export_package
from sdai.audit_ledger import AuditLedger
from sdai.audit_provenance import AuditAction, AuditActor
from sdai.audit_sinks import (
    AuditExportReceipt,
    AuditExportSinkRegistry,
    AuditSinkError,
    LocalFilesystemAuditSink,
    handoff_audit_export,
)


FEATURE = "AUDIT-SINK-241"


def _feature(root: Path) -> Path:
    feature = root / "specs" / "changes" / FEATURE
    feature.mkdir(parents=True)
    (feature / "requirements.md").write_text(
        "# Requirements\n\n- FR-001: Hand off immutable audit evidence.\n",
        encoding="utf-8",
    )
    return feature


def _append(root: Path, index: int) -> None:
    AuditLedger(root, FEATURE).append(
        category="system",
        actor=AuditActor("system", "sink-test"),
        action=AuditAction("sink.test.recorded", f"item:{index}"),
        metadata={"status": "recorded"},
        occurred_at=f"2026-08-19T05:{index:02d}:00Z",
    )


def _ledger_bytes(root: Path) -> bytes:
    return (
        root
        / "specs"
        / "changes"
        / FEATURE
        / ".sdai"
        / "audit"
        / "events.jsonl"
    ).read_bytes()


def test_local_reference_sink_publishes_verified_package_and_replays_idempotently(tmp_path: Path) -> None:
    _feature(tmp_path)
    for index in range(5):
        _append(tmp_path, index)
    before = _ledger_bytes(tmp_path)
    sink_root = tmp_path / "sink"
    sink = LocalFilesystemAuditSink(sink_root)

    first = handoff_audit_export(tmp_path, FEATURE, sink, chunk_size=1024)
    second = handoff_audit_export(tmp_path, FEATURE, sink, chunk_size=1024)

    assert first.status == "accepted"
    assert second.status == "already-present"
    assert first.receipt_id == second.receipt_id
    assert first.export_id == second.export_id
    assert first.manifest_sha256 == second.manifest_sha256
    assert first.chunk_sha256 == second.chunk_sha256
    assert first.receipt_sha256 != second.receipt_sha256
    final = sink_root / first.export_id
    assert (final / "manifest.json").is_file()
    assert sorted(path.name for path in final.iterdir()) == [
        *(f"chunk-{index:06d}.bin" for index in range(len(first.chunk_sha256))),
        "manifest.json",
    ]
    assert _ledger_bytes(tmp_path) == before
    assert not any(path.name.startswith(".partial-") for path in sink_root.iterdir())


def test_existing_tampered_sink_package_fails_closed_instead_of_overwriting(tmp_path: Path) -> None:
    _feature(tmp_path)
    for index in range(5):
        _append(tmp_path, index)
    sink = LocalFilesystemAuditSink(tmp_path / "sink")
    receipt = handoff_audit_export(tmp_path, FEATURE, sink, chunk_size=1024)
    final = sink.destination / receipt.export_id
    chunk = next(path for path in final.iterdir() if path.name.startswith("chunk-"))
    original = chunk.read_bytes()
    chunk.write_bytes(b"X" + original[1:])

    with pytest.raises(AuditSinkError, match="integrity validation"):
        handoff_audit_export(tmp_path, FEATURE, sink, chunk_size=1024)
    assert chunk.read_bytes() != original


def test_registry_is_deterministic_and_rejects_duplicate_or_unknown_sink(tmp_path: Path) -> None:
    registry = AuditExportSinkRegistry()
    first = LocalFilesystemAuditSink(tmp_path / "one", sink_id="local-a")
    second = LocalFilesystemAuditSink(tmp_path / "two", sink_id="local-b")
    registry.register(second)
    registry.register(first)

    assert registry.ids() == ("local-a", "local-b")
    assert registry.get("local-a") is first
    with pytest.raises(AuditSinkError, match="already registered"):
        registry.register(first)
    with pytest.raises(AuditSinkError, match="not registered"):
        registry.get("missing")


def test_sink_failure_does_not_mutate_source_ledger_and_handoff_does_not_retry(tmp_path: Path) -> None:
    _feature(tmp_path)
    _append(tmp_path, 0)
    before = _ledger_bytes(tmp_path)

    class FailingSink:
        sink_id = "failing-sink"

        def __init__(self) -> None:
            self.calls = 0

        def handoff(self, package: AuditExportPackage) -> AuditExportReceipt:
            self.calls += 1
            raise RuntimeError("simulated sink outage")

    sink = FailingSink()
    with pytest.raises(RuntimeError, match="simulated sink outage"):
        handoff_audit_export(tmp_path, FEATURE, sink)
    assert sink.calls == 1
    assert _ledger_bytes(tmp_path) == before


def test_ledger_mutation_during_sink_call_fails_after_exactly_one_delivery(tmp_path: Path) -> None:
    _feature(tmp_path)
    _append(tmp_path, 0)

    class MutatingSink:
        sink_id = "mutating-sink"

        def __init__(self) -> None:
            self.calls = 0

        def handoff(self, package: AuditExportPackage) -> AuditExportReceipt:
            self.calls += 1
            AuditLedger(tmp_path, FEATURE).append(
                category="system",
                actor=AuditActor("system", "mutation-test"),
                action=AuditAction("sink.concurrent.recorded", "item:1"),
                metadata={"status": "recorded"},
                occurred_at="2026-08-19T05:30:00Z",
            )
            return AuditExportReceipt.create(
                sink_id=self.sink_id,
                package=package,
                status="accepted",
            )

    sink = MutatingSink()
    with pytest.raises(AuditSinkError, match="changed during immutable sink handoff"):
        handoff_audit_export(tmp_path, FEATURE, sink)
    assert sink.calls == 1
    assert AuditLedger(tmp_path, FEATURE).verify().event_count == 2


def test_receipt_mismatch_fails_closed_without_retry(tmp_path: Path) -> None:
    _feature(tmp_path)
    _append(tmp_path, 0)

    class WrongReceiptSink:
        sink_id = "expected-sink"

        def __init__(self) -> None:
            self.calls = 0

        def handoff(self, package: AuditExportPackage) -> AuditExportReceipt:
            self.calls += 1
            return AuditExportReceipt.create(
                sink_id="different-sink",
                package=package,
                status="accepted",
            )

    sink = WrongReceiptSink()
    with pytest.raises(AuditSinkError, match="different sink"):
        handoff_audit_export(tmp_path, FEATURE, sink)
    assert sink.calls == 1


def test_stale_prebuilt_package_is_rejected_before_sink_call(tmp_path: Path) -> None:
    _feature(tmp_path)
    _append(tmp_path, 0)
    package = build_audit_export_package(tmp_path, FEATURE)
    _append(tmp_path, 1)

    class CountingSink:
        sink_id = "counting-sink"

        def __init__(self) -> None:
            self.calls = 0

        def handoff(self, package: AuditExportPackage) -> AuditExportReceipt:
            self.calls += 1
            return AuditExportReceipt.create(
                sink_id=self.sink_id,
                package=package,
                status="accepted",
            )

    sink = CountingSink()
    with pytest.raises(AuditSinkError, match="does not match immutable export package"):
        handoff_audit_export(tmp_path, FEATURE, sink, package=package)
    assert sink.calls == 0


def test_local_sink_recovers_from_safe_partial_directory_on_replay(tmp_path: Path) -> None:
    _feature(tmp_path)
    _append(tmp_path, 0)
    package = build_audit_export_package(tmp_path, FEATURE)
    sink = LocalFilesystemAuditSink(tmp_path / "sink")
    partial = sink.destination / f".partial-{package.manifest.export_id}"
    partial.mkdir()
    (partial / "old.tmp").write_bytes(b"partial")

    receipt = handoff_audit_export(tmp_path, FEATURE, sink, package=package)

    assert receipt.status == "accepted"
    assert not partial.exists()
    assert (sink.destination / package.manifest.export_id / "manifest.json").is_file()
