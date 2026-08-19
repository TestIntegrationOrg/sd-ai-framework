from __future__ import annotations

from pathlib import Path

import pytest

from sdai.audit_sinks import AuditSinkError, LocalFilesystemAuditSink


def test_local_sink_rejects_symlinked_destination_component(tmp_path: Path) -> None:
    real = tmp_path / "real-retention"
    real.mkdir()
    link = tmp_path / "retention-link"
    try:
        link.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation unavailable")

    with pytest.raises(AuditSinkError, match="contains a symlink component"):
        LocalFilesystemAuditSink(link / "child")
