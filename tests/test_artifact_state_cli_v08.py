from __future__ import annotations

import json
from pathlib import Path

from sdai.entrypoint import main as sdai_main


FEATURE = "STATE-CLI-1"


def _init(root: Path) -> None:
    config = root / ".sdai" / "config.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("version: 1\n", encoding="utf-8")


def test_artifact_status_and_explain_are_read_only(tmp_path: Path, capsys) -> None:
    _init(tmp_path)
    requirements = tmp_path / "specs" / "changes" / FEATURE / "requirements.md"
    requirements.parent.mkdir(parents=True, exist_ok=True)
    requirements.write_text("# Requirements\n\nFR-001: example\n", encoding="utf-8")
    before = requirements.read_bytes()

    assert (
        sdai_main(
            [
                "artifact",
                "status",
                FEATURE,
                "--json",
                "--path",
                str(tmp_path),
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    requirements_state = next(
        item for item in payload["artifacts"] if item["artifact_id"] == "requirements"
    )
    assert requirements_state["freshness"] == "stale"
    assert "no hash-bound artifact state record exists" in requirements_state["reasons"]
    assert requirements.read_bytes() == before

    assert (
        sdai_main(
            [
                "artifact",
                "explain",
                FEATURE,
                "requirements",
                "--json",
                "--path",
                str(tmp_path),
            ]
        )
        == 0
    )
    explained = json.loads(capsys.readouterr().out)
    assert explained["artifact_id"] == "requirements"
    assert explained["freshness"] == "stale"
    assert requirements.read_bytes() == before


def test_artifact_explain_unknown_id_fails_cleanly(tmp_path: Path, capsys) -> None:
    _init(tmp_path)

    assert (
        sdai_main(
            [
                "artifact",
                "explain",
                FEATURE,
                "does-not-exist",
                "--path",
                str(tmp_path),
            ]
        )
        == 1
    )
    assert "not active" in capsys.readouterr().err
