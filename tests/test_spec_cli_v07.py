from __future__ import annotations

import json
from pathlib import Path

import yaml

from sdai.entrypoint import main as sdai_main
from sdai.spec_changes import load_current_spec
from sdai.spec_validation import parse_current_requirements


def _fixture(root: Path, feature: str = "SIGN-CLI") -> Path:
    config = root / ".sdai" / "config.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("version: 1\n", encoding="utf-8")

    current_path = root / "specs" / "current" / "signing" / "specification.md"
    current_path.parent.mkdir(parents=True, exist_ok=True)
    current_path.write_text(
        """# Signing

## Functional Requirements
- FR-001: The service MUST sign a PowerShell file.

## Acceptance Criteria
- AC-001: A valid request returns a signed file.
""",
        encoding="utf-8",
    )
    current = load_current_spec(root, "signing")
    previous = parse_current_requirements(current).by_id()["FR-001"].sha256

    change_root = root / "specs" / "changes" / feature
    delta_root = change_root / "deltas"
    delta_root.mkdir(parents=True, exist_ok=True)
    (change_root / "change.yaml").write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "feature_id": feature,
                "title": "CLI promotion fixture",
                "status": "proposed",
                "domains": ["signing"],
                "baselines": {"signing": current.sha256},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (delta_root / "signing.yaml").write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "domain": "signing",
                "baseline_spec_sha256": current.sha256,
                "operations": [
                    {
                        "op": "MODIFIED",
                        "requirement_id": "FR-001",
                        "previous_hash": previous,
                        "definition": "The service MUST sign a PowerShell file using an approved key.",
                        "reason": "CLI fixture.",
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return current_path


def test_spec_validate_and_diff_json_are_machine_readable(tmp_path: Path, capsys) -> None:
    _fixture(tmp_path)

    assert sdai_main(["spec", "validate", "SIGN-CLI", "--json", "--path", str(tmp_path)]) == 0
    validation = json.loads(capsys.readouterr().out)
    assert validation["valid"] is True
    assert validation["feature_id"] == "SIGN-CLI"

    assert sdai_main(["spec", "diff", "SIGN-CLI", "--json", "--path", str(tmp_path)]) == 0
    diff = json.loads(capsys.readouterr().out)
    assert diff["feature_id"] == "SIGN-CLI"
    assert diff["domains"][0]["changes"][0]["op"] == "MODIFIED"
    assert "proposed_content" not in diff["domains"][0]

    assert sdai_main(
        [
            "spec",
            "diff",
            "SIGN-CLI",
            "--json",
            "--include-content",
            "--path",
            str(tmp_path),
        ]
    ) == 0
    detailed = json.loads(capsys.readouterr().out)
    assert "approved key" in detailed["domains"][0]["proposed_content"]


def test_spec_promote_dry_run_is_read_only_before_approval(tmp_path: Path, capsys) -> None:
    current = _fixture(tmp_path)
    before = current.read_bytes()

    assert sdai_main(
        ["spec", "promote", "SIGN-CLI", "--dry-run", "--json", "--path", str(tmp_path)]
    ) == 0
    preview = json.loads(capsys.readouterr().out)

    assert preview["eligible"] is False
    assert preview["approval"]["satisfied"] is False
    assert current.read_bytes() == before
    assert (tmp_path / "specs" / "changes" / "SIGN-CLI").is_dir()


def test_spec_approve_then_promote_archives_change(tmp_path: Path, capsys) -> None:
    current_path = _fixture(tmp_path)

    assert sdai_main(
        [
            "spec",
            "approve",
            "SIGN-CLI",
            "--by",
            "architect@example.com",
            "--role",
            "architect",
            "--json",
            "--path",
            str(tmp_path),
        ]
    ) == 0
    approval = json.loads(capsys.readouterr().out)
    assert approval["satisfied"] is True
    assert approval["change_sha256"].startswith("sha256:")

    assert sdai_main(
        ["spec", "promote", "SIGN-CLI", "--json", "--path", str(tmp_path)]
    ) == 0
    result = json.loads(capsys.readouterr().out)

    assert "approved key" in current_path.read_text(encoding="utf-8")
    assert not (tmp_path / "specs" / "changes" / "SIGN-CLI").exists()
    archive = tmp_path / result["archive_path"]
    assert archive.is_dir()
    assert (archive / "promotion.yaml").is_file()


def test_spec_promote_without_approval_returns_cli_error_and_preserves_truth(
    tmp_path: Path,
    capsys,
) -> None:
    current = _fixture(tmp_path)
    before = current.read_bytes()

    assert sdai_main(["spec", "promote", "SIGN-CLI", "--path", str(tmp_path)]) == 1
    captured = capsys.readouterr()

    assert "SDAI-SPECPROMO-004" in captured.err
    assert current.read_bytes() == before
    assert (tmp_path / "specs" / "changes" / "SIGN-CLI").is_dir()
