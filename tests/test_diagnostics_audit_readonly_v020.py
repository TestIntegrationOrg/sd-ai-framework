from __future__ import annotations

from pathlib import Path

import pytest

from sdai.audit_ledger import AuditLedger
from sdai.audit_provenance import AuditAction, AuditActor
from sdai.audit_readonly import ReadOnlyAuditError
from sdai.diagnostics import build_diagnostics_report
from sdai.scaffold import init_project


FEATURE = "DIAGNOSTICS-AUDIT-020"


def _workspace(root: Path) -> Path:
    init_project(root)
    feature = root / "specs" / "changes" / FEATURE
    feature.mkdir(parents=True, exist_ok=True)
    (feature / "requirements.md").write_text(
        "# Requirements\n\n- FR-258-AUDIT: Read audit without mutation.\n",
        encoding="utf-8",
        newline="\n",
    )
    return feature


def _tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_recoverable_incomplete_audit_tail_is_reported_partial_without_truncation(
    tmp_path: Path,
) -> None:
    feature = _workspace(tmp_path)
    AuditLedger(tmp_path, FEATURE).append(
        category="system",
        actor=AuditActor("system", "readonly-test"),
        action=AuditAction("diagnostics.audit.test", "readonly-test"),
        occurred_at="2026-08-19T15:00:00.000000Z",
    )
    ledger = feature / ".sdai" / "audit" / "events.jsonl"
    with ledger.open("ab") as stream:
        stream.write(b'{"incomplete":')
    before = _tree(tmp_path)

    report = build_diagnostics_report(tmp_path, FEATURE)

    assert report.exit_code == 0
    assert report.body["status"] == "partial"
    assert "recoverable-audit-crash-tail" in report.body["partialReasons"]
    assert report.body["audit"]["eventCount"] == 1
    assert report.body["audit"]["recoverableCrashTailBytes"] == len(b'{"incomplete":')
    assert _tree(tmp_path) == before


def test_complete_noncanonical_final_audit_record_is_not_hidden_as_crash_tail(
    tmp_path: Path,
) -> None:
    feature = _workspace(tmp_path)
    AuditLedger(tmp_path, FEATURE).append(
        category="system",
        actor=AuditActor("system", "readonly-test"),
        action=AuditAction("diagnostics.audit.test", "readonly-test"),
        occurred_at="2026-08-19T15:00:00.000000Z",
    )
    ledger = feature / ".sdai" / "audit" / "events.jsonl"
    with ledger.open("ab") as stream:
        stream.write(b'{}')
    before = _tree(tmp_path)

    with pytest.raises(ReadOnlyAuditError, match="complete noncanonical record"):
        build_diagnostics_report(tmp_path, FEATURE)

    assert _tree(tmp_path) == before
