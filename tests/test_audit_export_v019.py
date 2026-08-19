from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from sdai.audit_export import (
    AuditExportError,
    AuditExportManifest,
    AuditExportPackage,
    build_audit_export_package,
    validate_audit_export_package,
)
from sdai.audit_ledger import AuditLedger
from sdai.audit_provenance import AuditAction, AuditActor


FEATURE = "AUDIT-EXPORT-241"


def _feature(root: Path) -> Path:
    feature = root / "specs" / "changes" / FEATURE
    feature.mkdir(parents=True)
    (feature / "requirements.md").write_text(
        "# Requirements\n\n- FR-001: Export immutable audit evidence.\n",
        encoding="utf-8",
    )
    return feature


def _append(root: Path, index: int) -> None:
    AuditLedger(root, FEATURE).append(
        category="system",
        actor=AuditActor("system", "export-test"),
        action=AuditAction("export.test.recorded", f"item:{index}"),
        metadata={"status": "recorded", "index": index},
        occurred_at=f"2026-08-19T04:{index:02d}:00Z",
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


def test_same_verified_ledger_produces_byte_identical_package_and_manifest(tmp_path: Path) -> None:
    _feature(tmp_path)
    for index in range(8):
        _append(tmp_path, index)
    before = _ledger_bytes(tmp_path)

    first = build_audit_export_package(tmp_path, FEATURE, chunk_size=1024)
    second = build_audit_export_package(tmp_path, FEATURE, chunk_size=1024)

    assert first.manifest.to_json() == second.manifest.to_json()
    assert first.chunk_bytes == second.chunk_bytes
    assert first.manifest.export_id == second.manifest.export_id
    assert first.manifest.manifest_sha256 == second.manifest.manifest_sha256
    assert first.manifest.event_count == 8
    assert first.manifest.byte_length == len(before)
    assert b"".join(first.chunk_bytes) == before
    assert _ledger_bytes(tmp_path) == before
    assert len(first.manifest.chunks) >= 2
    for descriptor, content in first.iter_chunks():
        assert descriptor.byte_length == len(content)
        assert descriptor.offset == descriptor.index * 1024


def test_new_ledger_head_creates_new_export_identity_without_changing_prior_package(tmp_path: Path) -> None:
    _feature(tmp_path)
    _append(tmp_path, 0)
    first = build_audit_export_package(tmp_path, FEATURE)
    first_manifest = first.manifest.to_json()
    first_chunks = first.chunk_bytes

    _append(tmp_path, 1)
    second = build_audit_export_package(tmp_path, FEATURE)

    assert second.manifest.export_id != first.manifest.export_id
    assert second.manifest.ledger_head_sha256 != first.manifest.ledger_head_sha256
    assert second.manifest.export_sha256 != first.manifest.export_sha256
    assert first.manifest.to_json() == first_manifest
    assert first.chunk_bytes == first_chunks


def test_mutated_chunk_fails_package_validation(tmp_path: Path) -> None:
    _feature(tmp_path)
    for index in range(4):
        _append(tmp_path, index)
    package = build_audit_export_package(tmp_path, FEATURE, chunk_size=1024)
    chunks = list(package.chunk_bytes)
    chunks[0] = b"X" + chunks[0][1:]

    with pytest.raises(AuditExportError, match="chunk 0 SHA-256 mismatch"):
        AuditExportPackage(package.manifest, tuple(chunks))


def test_missing_or_reordered_chunks_fail_validation(tmp_path: Path) -> None:
    _feature(tmp_path)
    for index in range(8):
        _append(tmp_path, index)
    package = build_audit_export_package(tmp_path, FEATURE, chunk_size=1024)
    assert len(package.chunk_bytes) >= 2

    with pytest.raises(AuditExportError, match="chunk count"):
        AuditExportPackage(package.manifest, package.chunk_bytes[:-1])

    reordered = list(package.chunk_bytes)
    reordered[0], reordered[1] = reordered[1], reordered[0]
    with pytest.raises(AuditExportError, match="chunk 0"):
        AuditExportPackage(package.manifest, tuple(reordered))


def test_manifest_mutation_and_unsupported_version_fail_closed(tmp_path: Path) -> None:
    _feature(tmp_path)
    _append(tmp_path, 0)
    package = build_audit_export_package(tmp_path, FEATURE)
    text = package.manifest.to_json()

    tampered = text.replace(package.manifest.export_id, "audit-export-" + "f" * 64)
    with pytest.raises(AuditExportError, match="exportId does not match"):
        AuditExportManifest.from_json(tampered)

    unsupported = text.replace("sdai.audit-export/v1", "sdai.audit-export/v2")
    with pytest.raises(AuditExportError, match="fields/version"):
        AuditExportManifest.from_json(unsupported)


def test_empty_verified_ledger_has_deterministic_zero_chunk_package(tmp_path: Path) -> None:
    _feature(tmp_path)
    package = build_audit_export_package(tmp_path, FEATURE)

    assert package.manifest.event_count == 0
    assert package.manifest.byte_length == 0
    assert package.manifest.chunks == ()
    assert package.chunk_bytes == ()
    assert package.manifest.export_sha256.startswith("sha256:")
    validate_audit_export_package(package)


def test_packaging_detects_ledger_mutation_between_export_and_final_verify(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _feature(tmp_path)
    _append(tmp_path, 0)
    original = AuditLedger.export_jsonl
    mutated = False

    def mutate_after_export(self):
        nonlocal mutated
        data = original(self)
        if not mutated:
            mutated = True
            self.append(
                category="system",
                actor=AuditActor("system", "race-test"),
                action=AuditAction("export.race.recorded", "item:race"),
                metadata={"status": "recorded"},
                occurred_at="2026-08-19T04:59:00Z",
            )
        return data

    monkeypatch.setattr(AuditLedger, "export_jsonl", mutate_after_export)
    with pytest.raises(AuditExportError, match="changed while immutable export was being packaged"):
        build_audit_export_package(tmp_path, FEATURE)
