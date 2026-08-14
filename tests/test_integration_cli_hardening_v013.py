from __future__ import annotations

import json
from pathlib import Path

import pytest

from sdai.entrypoint import main
from sdai.integration_cli import (
    INTEGRATION_CLI_ERROR_API_VERSION,
    INTEGRATION_INFO_API_VERSION,
)


def _init(root: Path) -> None:
    (root / ".sdai").mkdir(parents=True, exist_ok=True)
    (root / ".sdai" / "config.yaml").write_text("operating_mode: individual\n", encoding="utf-8")
    (root / "empty-user").mkdir()


def _manifest() -> str:
    return """apiVersion: sdai.integration-manifest/v1
id: override-tool
version: 1.0.0
displayName: Override Tool
description: Layer override regression
capabilities: [skills]
projections:
  - kind: skill
    source: canonical/skills
    target: .tool/skills
execution: null
security:
  requiresNetwork: false
  requiresWorkspaceWrite: false
  environment: []
"""


def _json_call(root: Path, capsys: pytest.CaptureFixture[str], *args: str) -> tuple[int, dict[str, object]]:
    code = main(["integration", *args, "--json", "--path", str(root)])
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.count("\n") == 1
    return code, json.loads(captured.out)


def test_builtin_source_is_normal_lowest_precedence_unless_explicitly_locked_elsewhere(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _init(tmp_path)
    builtin = tmp_path / "builtin"
    repo = tmp_path / "repo"
    user = tmp_path / "empty-user"
    builtin.mkdir()
    repo.mkdir()
    (builtin / "override.integration.yaml").write_text(_manifest(), encoding="utf-8")
    (repo / "override.integration.yaml").write_text(_manifest(), encoding="utf-8")

    code, info = _json_call(
        tmp_path,
        capsys,
        "info",
        "override-tool",
        "--builtin-source",
        str(builtin),
        "--repo-source",
        str(repo),
        "--user-source",
        str(user),
    )

    assert code == 0
    assert info["apiVersion"] == INTEGRATION_INFO_API_VERSION
    resolution = info["resolution"]
    assert resolution["selectedProvenance"]["layer"] == "repo"
    assert [item["layer"] for item in resolution["provenance"]] == ["builtin", "repo"]


def test_malformed_selection_blocks_remove_before_managed_files_are_deleted(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _init(tmp_path)
    repo = tmp_path / ".sdai" / "integrations" / "manifests"
    repo.mkdir(parents=True)
    (repo / "override.integration.yaml").write_text(_manifest(), encoding="utf-8")
    source = tmp_path / "canonical" / "skills"
    source.mkdir(parents=True)
    (source / "review.md").write_text("managed\n", encoding="utf-8")

    code, _ = _json_call(
        tmp_path,
        capsys,
        "install",
        "override-tool",
        "--user-source",
        str(tmp_path / "empty-user"),
    )
    assert code == 0
    native = tmp_path / ".tool" / "skills" / "review.md"
    state = tmp_path / ".sdai" / "integrations" / "install-state.json"
    state_before = state.read_bytes()
    assert native.exists()

    selection = tmp_path / ".sdai" / "integrations" / "selection.json"
    selection.write_text(
        '{"apiVersion":"sdai.integration-selection/v1","selection":{},"selection":{}}\n',
        encoding="utf-8",
    )

    code, error = _json_call(tmp_path, capsys, "remove", "override-tool")
    assert code == 4
    assert error["apiVersion"] == INTEGRATION_CLI_ERROR_API_VERSION
    assert error["code"] == "SDAI-INTEGRATION-CLI-003"
    assert native.read_text(encoding="utf-8") == "managed\n"
    assert state.read_bytes() == state_before
