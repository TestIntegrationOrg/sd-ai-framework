from __future__ import annotations

import json
from pathlib import Path

import pytest

from sdai.artifacts import write_text
from sdai.migration import (
    MIGRATION_PLAN_API_VERSION,
    MIGRATION_RESULT_API_VERSION,
    MIGRATION_ROLLBACK_API_VERSION,
    MigrationSafetyError,
    apply_migration,
    plan_migration,
    rollback_migration,
)
from sdai.scaffold import init_project
from sdai.v05_scaffold import AGENTS_V054
from sdai.version_entrypoint import main as sdai_main


def _files(root: Path) -> dict[str, bytes]:
    result: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        rel = path.relative_to(root).as_posix()
        if rel.startswith(".sdai/migrations/"):
            continue
        result[rel] = path.read_bytes()
    return result


def _legacy_project(root: Path) -> None:
    init_project(root)


def test_migration_plan_is_deterministic_and_read_only(tmp_path: Path) -> None:
    _legacy_project(tmp_path)
    before = _files(tmp_path)

    first = plan_migration(tmp_path)
    second = plan_migration(tmp_path)

    assert first.to_json() == second.to_json()
    assert first.as_dict()["apiVersion"] == MIGRATION_PLAN_API_VERSION
    assert first.changes
    assert _files(tmp_path) == before
    assert not (tmp_path / ".sdai" / "migrations").exists()


def test_apply_and_rollback_restore_original_managed_bytes(tmp_path: Path) -> None:
    _legacy_project(tmp_path)
    before = _files(tmp_path)

    result = apply_migration(tmp_path)
    assert result.status == "applied"
    assert result.as_dict()["apiVersion"] == MIGRATION_RESULT_API_VERSION
    assert result.migration_id
    assert result.manifest_path
    assert plan_migration(tmp_path).current

    rollback = rollback_migration(tmp_path, result.migration_id)
    assert rollback.status == "rolled-back"
    assert rollback.as_dict()["apiVersion"] == MIGRATION_ROLLBACK_API_VERSION
    assert _files(tmp_path) == before

    repeated = rollback_migration(tmp_path, result.migration_id)
    assert repeated.status == "already-rolled-back"


def test_stock_files_upgrade_but_customized_files_are_preserved(tmp_path: Path) -> None:
    _legacy_project(tmp_path)
    agent_root = tmp_path / ".sdai" / "agents"
    stock = agent_root / "requirements-analyst.agent.md"
    custom = agent_root / "developer.agent.md"
    write_text(stock, AGENTS_V054["requirements-analyst"], overwrite=True)
    custom.parent.mkdir(parents=True, exist_ok=True)
    custom.write_text("# team-owned developer\n", encoding="utf-8", newline="\n")
    stock_before = stock.read_bytes()
    custom_before = custom.read_bytes()

    plan = plan_migration(tmp_path)
    by_path = {item.path: item for item in plan.changes}
    assert by_path[".sdai/agents/requirements-analyst.agent.md"].action == "replace-stock"
    assert ".sdai/agents/developer.agent.md" not in by_path

    result = apply_migration(tmp_path)
    assert stock.read_bytes() != stock_before
    assert custom.read_bytes() == custom_before

    assert result.migration_id is not None
    rollback_migration(tmp_path, result.migration_id)
    assert stock.read_bytes() == stock_before
    assert custom.read_bytes() == custom_before


def test_rollback_refuses_tampered_target_before_mutating_anything(tmp_path: Path) -> None:
    _legacy_project(tmp_path)
    result = apply_migration(tmp_path)
    creates = [item for item in result.changes if item.action == "create"]
    assert len(creates) >= 2
    tampered = tmp_path.joinpath(*Path(creates[0].path).parts)
    untouched = tmp_path.joinpath(*Path(creates[1].path).parts)
    tampered.write_text("team changed this after migration\n", encoding="utf-8")
    untouched_before = untouched.read_bytes()

    assert result.migration_id is not None
    with pytest.raises(MigrationSafetyError, match="changed after migration"):
        rollback_migration(tmp_path, result.migration_id)

    assert tampered.read_text(encoding="utf-8") == "team changed this after migration\n"
    assert untouched.read_bytes() == untouched_before


def test_migrate_json_cli_is_machine_clean(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _legacy_project(tmp_path)

    assert sdai_main(["migrate", "plan", "--json", "--path", str(tmp_path)]) == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["apiVersion"] == MIGRATION_PLAN_API_VERSION
    assert payload["changes"]
    assert captured.err == ""

    assert sdai_main(["migrate", "apply", "--json", "--path", str(tmp_path)]) == 0
    applied_output = capsys.readouterr()
    applied = json.loads(applied_output.out)
    assert applied["apiVersion"] == MIGRATION_RESULT_API_VERSION
    assert applied["status"] == "applied"
    assert applied_output.err == ""

    assert sdai_main(
        [
            "migrate",
            "rollback",
            applied["migrationId"],
            "--json",
            "--path",
            str(tmp_path),
        ]
    ) == 0
    rollback_output = capsys.readouterr()
    rolled_back = json.loads(rollback_output.out)
    assert rolled_back["apiVersion"] == MIGRATION_ROLLBACK_API_VERSION
    assert rolled_back["status"] == "rolled-back"
    assert rollback_output.err == ""


def test_public_upgrade_preserves_historical_grammar_and_is_idempotent(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _legacy_project(tmp_path)

    assert sdai_main(["upgrade", "--path", str(tmp_path)]) == 0
    first = capsys.readouterr()
    assert f"Upgraded SD-AI project at {tmp_path.resolve()}" in first.out
    assert "SD-AI framework version " in first.out
    assert "  + .sdai/framework-version.yaml" in first.out
    assert first.err == ""

    assert sdai_main(["upgrade", "--path", str(tmp_path)]) == 0
    second = capsys.readouterr()
    assert "SD-AI project already has the current scaffold" in second.out
    assert "SD-AI framework version " in second.out
    assert second.err == ""
