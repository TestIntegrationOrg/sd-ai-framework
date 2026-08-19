from __future__ import annotations

import json
from pathlib import Path

import pytest

from sdai.audit_ledger import AuditLedger
from sdai.audit_provenance import AuditAction, AuditActor
from sdai.version_entrypoint import main as sdai_main


FEATURE = "AUDIT-EXPORT-CLI-241"


def _feature(root: Path) -> Path:
    feature = root / "specs" / "changes" / FEATURE
    feature.mkdir(parents=True)
    (feature / "requirements.md").write_text(
        "# Requirements\n\n- FR-001: Export audit through public CLI.\n",
        encoding="utf-8",
    )
    return feature


def _event(root: Path) -> None:
    AuditLedger(root, FEATURE).append(
        category="system",
        actor=AuditActor("system", "PRIVATE_EXPORT_ACTOR_MARKER"),
        action=AuditAction(
            "export.cli.recorded",
            "feature:PRIVATE_EXPORT_SUBJECT_MARKER",
            reason="PRIVATE_EXPORT_REASON_MARKER",
        ),
        metadata={"status": "recorded", "detail": "PRIVATE_EXPORT_METADATA_MARKER"},
        occurred_at="2026-08-19T06:00:00Z",
    )


def test_versioned_export_cli_is_deterministic_and_manifest_receipt_are_privacy_bounded(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _feature(tmp_path)
    _event(tmp_path)
    sink = tmp_path / "retention"
    args = [
        "audit",
        "export",
        FEATURE,
        "--destination",
        str(sink),
        "--json",
        "--path",
        str(tmp_path),
    ]

    assert sdai_main(args) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["apiVersion"] == "sdai.audit-export-result/v1"
    assert first["status"] == "accepted"
    assert first["eventCount"] == 1
    assert first["chunkCount"] == 1
    assert first["resultSha256"].startswith("sha256:")

    assert sdai_main(args) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["status"] == "already-present"
    for key in (
        "exportId",
        "ledgerHeadSha256",
        "exportSha256",
        "manifestSha256",
        "receiptId",
    ):
        assert second[key] == first[key]

    manifest = (sink / first["exportId"] / "manifest.json").read_text(encoding="utf-8")
    receipt_json = json.dumps(second, sort_keys=True)
    for forbidden in (
        "PRIVATE_EXPORT_ACTOR_MARKER",
        "PRIVATE_EXPORT_SUBJECT_MARKER",
        "PRIVATE_EXPORT_REASON_MARKER",
        "PRIVATE_EXPORT_METADATA_MARKER",
    ):
        assert forbidden not in manifest
        assert forbidden not in receipt_json

    chunk = next((sink / first["exportId"]).glob("chunk-*.bin"))
    chunk_bytes = chunk.read_bytes()
    assert b"PRIVATE_EXPORT_ACTOR_MARKER" in chunk_bytes
    # Chunks are the canonical immutable audit truth; privacy bounding applies to
    # manifest/result/receipt metadata, not to rewriting source ledger bytes.


def test_export_cli_invalid_chunk_size_returns_stable_integrity_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _feature(tmp_path)
    _event(tmp_path)
    code = sdai_main(
        [
            "audit",
            "export",
            FEATURE,
            "--destination",
            str(tmp_path / "sink"),
            "--chunk-bytes",
            "1",
            "--json",
            "--path",
            str(tmp_path),
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 5
    assert payload["apiVersion"] == "sdai.audit-export-error/v1"
    assert payload["error"]["code"] == "SDAI-AUDIT-EXPORT-002"


def test_export_cli_unknown_argument_returns_input_exit_four(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _feature(tmp_path)
    code = sdai_main(
        [
            "audit",
            "export",
            FEATURE,
            "--destination",
            str(tmp_path / "sink"),
            "--unknown",
            "x",
            "--json",
            "--path",
            str(tmp_path),
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 4
    assert payload["error"]["code"] == "SDAI-AUDIT-EXPORT-CLI-001"


def test_export_cli_does_not_change_existing_audit_query_routing(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _feature(tmp_path)
    _event(tmp_path)

    assert sdai_main(["audit", FEATURE, "--json", "--path", str(tmp_path)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["apiVersion"] == "sdai.audit-report/v1"
    assert report["eventCount"] == 1
