from __future__ import annotations

import json
from pathlib import Path

import pytest

from sdai.entrypoint import main
from sdai.integration_cli import (
    INTEGRATION_CLI_ERROR_API_VERSION,
    INTEGRATION_INFO_API_VERSION,
    INTEGRATION_LIFECYCLE_RESULT_API_VERSION,
    INTEGRATION_SEARCH_API_VERSION,
    INTEGRATION_SELECTION_API_VERSION,
    INTEGRATION_STATUS_COMMAND_API_VERSION,
)


def _project(root: Path) -> Path:
    (root / ".sdai").mkdir(parents=True, exist_ok=True)
    (root / ".sdai" / "config.yaml").write_text("operating_mode: individual\n", encoding="utf-8")
    (root / ".sdai" / "agents.yaml").write_text("profiles: {}\n", encoding="utf-8")
    (root / ".sdai" / "policy.yaml").write_text("version: 1\nproviders: {}\n", encoding="utf-8")
    (root / "empty-user-integrations").mkdir()
    return root


def _manifest_text(version: str, source: str, description: str) -> str:
    return f"""apiVersion: sdai.integration-manifest/v1
id: test-tool
version: {version}
displayName: Test Tool café Δ
description: {description}
capabilities:
  - skills
projections:
  - kind: skill
    source: {source}
    target: .tool/skills
execution: null
security:
  requiresNetwork: false
  requiresWorkspaceWrite: false
  environment: []
"""


def _catalog(root: Path) -> None:
    manifests = root / ".sdai" / "integrations" / "manifests"
    manifests.mkdir(parents=True, exist_ok=True)
    (manifests / "test-tool-v1.integration.yaml").write_text(
        _manifest_text("1.0.0", "canonical/v1/skills", "Version one café Δ"),
        encoding="utf-8",
        newline="\n",
    )
    (manifests / "test-tool-v2.integration.yaml").write_text(
        _manifest_text("2.0.0", "canonical/v2/skills", "Version two café Δ"),
        encoding="utf-8",
        newline="\n",
    )
    for version, text in (("v1", "skill v1 café Δ\n"), ("v2", "skill v2 café Δ\n")):
        source = root / "canonical" / version / "skills"
        source.mkdir(parents=True, exist_ok=True)
        (source / "review.md").write_text(text, encoding="utf-8", newline="\n")


def _registry_args(root: Path) -> list[str]:
    return ["--user-source", str(root / "empty-user-integrations")]


def _run_json(root: Path, capsys: pytest.CaptureFixture[str], *args: str) -> tuple[int, dict[str, object]]:
    code = main(["integration", *args, "--json", "--path", str(root)])
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.endswith("\n")
    assert captured.out.count("\n") == 1
    return code, json.loads(captured.out)


def test_search_info_and_not_found_are_machine_clean_and_provenance_complete(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = _project(tmp_path)
    _catalog(root)

    code, search = _run_json(root, capsys, "search", "café", *_registry_args(root))
    assert code == 0
    assert search["apiVersion"] == INTEGRATION_SEARCH_API_VERSION
    assert search["query"] == "café"
    results = search["results"]
    assert isinstance(results, list) and len(results) == 1
    row = results[0]
    assert row["resolution"]["identity"] == "test-tool@2.0.0"
    assert row["resolution"]["selectedProvenance"] == {
        "layer": "repo",
        "locked": False,
        "manifestSha256": row["resolution"]["manifestSha256"],
        "path": "test-tool-v2.integration.yaml",
        "source": "repository",
    }
    assert row["installed"] is None
    assert row["selected"] is False

    code, info = _run_json(
        root,
        capsys,
        "info",
        "test-tool",
        "--version",
        "1.0.0",
        *_registry_args(root),
    )
    assert code == 0
    assert info["apiVersion"] == INTEGRATION_INFO_API_VERSION
    assert info["resolution"]["identity"] == "test-tool@1.0.0"
    assert info["resolution"]["manifest"]["displayName"] == "Test Tool café Δ"
    assert info["installed"] is None

    code, missing = _run_json(root, capsys, "info", "missing-tool", *_registry_args(root))
    assert code == 3
    assert missing["apiVersion"] == INTEGRATION_INFO_API_VERSION
    assert missing["resolution"] is None


def test_install_status_repair_upgrade_use_remove_full_lifecycle(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = _project(tmp_path)
    _catalog(root)
    agents_before = (root / ".sdai" / "agents.yaml").read_bytes()
    policy_before = (root / ".sdai" / "policy.yaml").read_bytes()

    code, installed = _run_json(
        root,
        capsys,
        "install",
        "test-tool",
        "--version",
        "1.0.0",
        *_registry_args(root),
    )
    assert code == 0
    assert installed["apiVersion"] == INTEGRATION_LIFECYCLE_RESULT_API_VERSION
    assert installed["status"] == "ok"
    assert installed["installed"]["identity"] == "test-tool@1.0.0"
    native = root / ".tool" / "skills" / "review.md"
    assert native.read_text(encoding="utf-8") == "skill v1 café Δ\n"
    state_before = (root / ".sdai" / "integrations" / "install-state.json").read_bytes()

    code, repeated = _run_json(
        root,
        capsys,
        "install",
        "test-tool",
        "--version",
        "1.0.0",
        *_registry_args(root),
    )
    assert code == 0
    assert repeated["installed"]["identity"] == "test-tool@1.0.0"
    assert (root / ".sdai" / "integrations" / "install-state.json").read_bytes() == state_before

    code, blocked_install = _run_json(root, capsys, "install", "test-tool", *_registry_args(root))
    assert code == 2
    assert blocked_install["status"] == "different-version-installed"
    assert blocked_install["desired"]["identity"] == "test-tool@2.0.0"
    assert native.read_text(encoding="utf-8") == "skill v1 café Δ\n"

    code, status = _run_json(root, capsys, "status", "test-tool", *_registry_args(root))
    assert code == 2
    assert status["apiVersion"] == INTEGRATION_STATUS_COMMAND_API_VERSION
    assert status["status"] == "stale"
    assert status["installed"]["identity"] == "test-tool@1.0.0"
    assert status["report"]["desiredIdentity"] == "test-tool@2.0.0"

    native.unlink()
    code, repaired = _run_json(root, capsys, "repair", "test-tool", *_registry_args(root))
    assert code == 0
    assert repaired["installed"]["identity"] == "test-tool@1.0.0"
    assert native.read_text(encoding="utf-8") == "skill v1 café Δ\n"

    code, upgraded = _run_json(root, capsys, "upgrade", "test-tool", *_registry_args(root))
    assert code == 0
    assert upgraded["installed"]["identity"] == "test-tool@2.0.0"
    assert native.read_text(encoding="utf-8") == "skill v2 café Δ\n"
    upgraded_state = (root / ".sdai" / "integrations" / "install-state.json").read_bytes()

    code, repeated_upgrade = _run_json(root, capsys, "upgrade", "test-tool", *_registry_args(root))
    assert code == 0
    assert repeated_upgrade["installed"]["identity"] == "test-tool@2.0.0"
    assert (root / ".sdai" / "integrations" / "install-state.json").read_bytes() == upgraded_state

    code, selected = _run_json(root, capsys, "use", "test-tool")
    assert code == 0
    assert selected["selection"]["identity"] == "test-tool@2.0.0"
    selection = json.loads((root / ".sdai" / "integrations" / "selection.json").read_text(encoding="utf-8"))
    assert selection["apiVersion"] == INTEGRATION_SELECTION_API_VERSION
    assert selection["selection"]["identity"] == "test-tool@2.0.0"
    assert (root / ".sdai" / "agents.yaml").read_bytes() == agents_before
    assert (root / ".sdai" / "policy.yaml").read_bytes() == policy_before

    code, exact = _run_json(root, capsys, "status", "test-tool", *_registry_args(root))
    assert code == 0
    assert exact["status"] == "exact"
    assert exact["selected"] is True

    code, removed = _run_json(root, capsys, "remove", "test-tool")
    assert code == 0
    assert removed["status"] == "ok"
    assert removed["selectionCleared"] is True
    assert not native.exists()
    assert not (root / ".sdai" / "integrations" / "selection.json").exists()

    code, removed_again = _run_json(root, capsys, "remove", "test-tool")
    assert code == 0
    assert removed_again["preservedPaths"] == []


def test_modified_managed_file_fails_closed_with_json_error_and_is_preserved(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = _project(tmp_path)
    _catalog(root)
    code, _ = _run_json(
        root,
        capsys,
        "install",
        "test-tool",
        "--version",
        "1.0.0",
        *_registry_args(root),
    )
    assert code == 0
    native = root / ".tool" / "skills" / "review.md"
    native.write_text("USER EDIT café Δ\n", encoding="utf-8", newline="\n")

    code, error = _run_json(root, capsys, "upgrade", "test-tool", *_registry_args(root))
    assert code == 4
    assert error["apiVersion"] == INTEGRATION_CLI_ERROR_API_VERSION
    assert error["status"] == "error"
    assert error["code"].startswith("SDAI-INTEGRATION-MAT-")
    assert native.read_text(encoding="utf-8") == "USER EDIT café Δ\n"


def test_missing_explicit_registry_source_is_json_error_and_human_error_uses_stderr(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = _project(tmp_path)

    code, error = _run_json(
        root,
        capsys,
        "search",
        "",
        "--repo-source",
        str(root / "missing-source"),
        *_registry_args(root),
    )
    assert code == 4
    assert error["apiVersion"] == INTEGRATION_CLI_ERROR_API_VERSION
    assert error["code"] == "SDAI-INTEGRATION-CLI-002"

    code = main(
        [
            "integration",
            "search",
            "--repo-source",
            str(root / "missing-source"),
            "--path",
            str(root),
        ]
    )
    captured = capsys.readouterr()
    assert code == 4
    assert captured.out == ""
    assert "SDAI-INTEGRATION-CLI-002" in captured.err


def test_top_level_help_lists_integration_lifecycle(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--help"]) == 0
    captured = capsys.readouterr()
    assert "Integration lifecycle commands:" in captured.out
    assert "sdai integration install|status|repair|upgrade" in captured.out
