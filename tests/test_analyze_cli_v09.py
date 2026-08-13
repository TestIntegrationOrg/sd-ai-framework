from __future__ import annotations

import json
from pathlib import Path

import pytest

from sdai.version_entrypoint import main as sdai_main


FEATURE = "ANALYZE-CLI"


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")
    return path


def _init(root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert sdai_main(["init", "--path", str(root)]) == 0
    capsys.readouterr()


def _linked_base(root: Path) -> Path:
    feature = root / "specs" / "changes" / FEATURE
    _write(
        feature / "requirements.md",
        """- FR-001: Implement behavior with TASK-001.
- NFR-001: Complete quickly with TASK-001.
- AC-001: Valid request succeeds with TEST-001.
""",
    )
    _write(feature / "tasks.md", "- TASK-001: Implement FR-001 and NFR-001.\n")
    _write(feature / "tests.md", "- TEST-001: Verify AC-001.\n")
    return feature


def _warning_feature(root: Path) -> None:
    feature = _linked_base(root)
    _write(
        feature / "adr" / "ADR-001.md",
        "# ADR-001: Pending architecture choice\nstatus: proposed\n",
    )


def _blocking_feature(root: Path) -> None:
    feature = _linked_base(root)
    _write(
        feature / "adr" / "ADR-001.md",
        "# ADR-001: Approved architecture\nstatus: accepted\n",
    )
    _write(
        feature / "contracts" / "api.yaml",
        "id: CONTRACT-001\nstatus: breaking\n",
    )


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def test_warning_only_analysis_returns_zero_and_human_output_has_source_lines(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "Warnings Ω"
    root.mkdir()
    _init(root, capsys)
    _warning_feature(root)
    before = _snapshot(root)

    exit_code = sdai_main(["analyze", FEATURE, "--path", str(root)])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert captured.err == ""
    assert "Analysis feature=ANALYZE-CLI" in captured.out
    assert "blocking=0" in captured.out
    assert "WARNING    UNRESOLVED_ADR entity=ADR-001" in captured.out
    assert f"specs/changes/{FEATURE}/adr/ADR-001.md:1" in captured.out
    assert "\\" not in captured.out
    assert _snapshot(root) == before


def test_blocking_analysis_returns_two_and_json_stdout_is_machine_clean(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "Blocking café Δ"
    root.mkdir()
    _init(root, capsys)
    _blocking_feature(root)
    before = _snapshot(root)

    exit_code = sdai_main(
        ["analyze", FEATURE, "--json", "--risk", "critical", "--path", str(root)]
    )
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code == 2
    assert captured.err == ""
    assert captured.out.lstrip().startswith("{")
    assert payload["apiVersion"] == "sdai.findings/v1"
    assert payload["feature_id"] == FEATURE
    assert any(
        item["code"] == "UNAPPROVED_BREAKING_CHANGE"
        and item["severity"] == "blocking"
        for item in payload["findings"]
    )
    assert _snapshot(root) == before


def test_clean_feature_returns_zero_and_empty_findings_json(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "Clean"
    root.mkdir()
    _init(root, capsys)
    feature = _linked_base(root)
    _write(
        feature / "adr" / "ADR-001.md",
        "# ADR-001: Accepted architecture\nstatus: accepted\n",
    )

    exit_code = sdai_main(["analyze", FEATURE, "--json", "--path", str(root)])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code == 0
    assert payload["findings"] == []
    assert captured.err == ""


def test_missing_feature_returns_one_on_stderr_without_json_noise(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "Missing"
    root.mkdir()
    _init(root, capsys)

    exit_code = sdai_main(["analyze", "MISSING-100", "--json", "--path", str(root)])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert captured.out == ""
    assert "SDAI-ANALYSIS-001" in captured.err
    assert "error:" in captured.err


def test_analyze_requires_initialized_project(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = sdai_main(["analyze", FEATURE, "--path", str(tmp_path)])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert captured.out == ""
    assert "Not an SD-AI project" in captured.err


def test_top_level_help_advertises_analyze_without_removing_legacy_help(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert sdai_main(["--help"]) == 0
    output = capsys.readouterr().out

    assert "sdai analyze <feature>" in output
    assert "Execute or resume a declarative workflow" in output
