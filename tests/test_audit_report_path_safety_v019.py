from __future__ import annotations

from pathlib import Path

import pytest

from sdai.audit_report import AuditReportError, build_audit_report


FEATURE = "AUDIT-PATH-240"


def test_existing_audit_path_component_file_is_not_reported_as_no_events(tmp_path: Path) -> None:
    feature = tmp_path / "specs" / "changes" / FEATURE
    feature.mkdir(parents=True)
    state = feature / ".sdai"
    state.mkdir()
    (state / "audit").write_text("not a directory\n", encoding="utf-8")

    with pytest.raises(AuditReportError, match="feature audit directory must be a directory"):
        build_audit_report(tmp_path, FEATURE)
