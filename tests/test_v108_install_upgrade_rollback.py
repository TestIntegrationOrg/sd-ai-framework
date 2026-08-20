from __future__ import annotations

from pathlib import Path

import pytest

from sdai.migration import MigrationSafetyError, _atomic_write, _safe_target
from sdai.migration_transaction import (
    _build_prepared_manifest,
    _safe_evidence_target,
    _write_backups,
    _write_canonical_json,
    apply_migration,
    plan_migration,
    rollback_migration,
)
from sdai.scaffold import init_project


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


def _prepare_interrupted_transaction(root: Path, migration_id: str):
    plan = plan_migration(root)
    assert plan.changes
    manifest_rel = f".sdai/migrations/{migration_id}/manifest.json"
    manifest_path = _safe_evidence_target(root, manifest_rel)
    manifest_path.parent.mkdir(parents=True, exist_ok=False)
    manifest = _build_prepared_manifest(migration_id, plan)
    _write_canonical_json(manifest_path, manifest)
    _write_backups(root, migration_id, plan)
    return plan, manifest


def test_interrupted_partial_apply_is_recovered_before_retry(tmp_path: Path) -> None:
    init_project(tmp_path)
    before = _files(tmp_path)
    plan, _ = _prepare_interrupted_transaction(tmp_path, "crash286")

    first = plan.changes[0]
    target = _safe_target(tmp_path, first.path)
    _atomic_write(target, first._after_bytes)
    assert target.read_bytes() == first._after_bytes

    with pytest.raises(MigrationSafetyError, match="requires recovery"):
        plan_migration(tmp_path)

    result = apply_migration(tmp_path)
    assert result.status == "applied"
    assert result.migration_id is not None
    assert (tmp_path / ".sdai/migrations/crash286/recovery.json").is_file()
    assert (
        tmp_path / ".sdai" / "migrations" / result.migration_id / "commit.json"
    ).is_file()
    assert plan_migration(tmp_path).current

    rollback_migration(tmp_path, result.migration_id)
    assert _files(tmp_path) == before


def test_interruption_after_manifest_before_backups_is_recoverable(tmp_path: Path) -> None:
    init_project(tmp_path)
    before = _files(tmp_path)
    plan = plan_migration(tmp_path)
    assert plan.changes
    migration_id = "prepared286"
    manifest_path = _safe_evidence_target(
        tmp_path,
        f".sdai/migrations/{migration_id}/manifest.json",
    )
    manifest_path.parent.mkdir(parents=True, exist_ok=False)
    manifest = _build_prepared_manifest(migration_id, plan)
    _write_canonical_json(manifest_path, manifest)

    result = apply_migration(tmp_path)
    assert result.status == "applied"
    assert (tmp_path / f".sdai/migrations/{migration_id}/recovery.json").is_file()

    assert result.migration_id is not None
    rollback_migration(tmp_path, result.migration_id)
    assert _files(tmp_path) == before


def test_interrupted_apply_with_unknown_bytes_fails_closed_without_repair(
    tmp_path: Path,
) -> None:
    init_project(tmp_path)
    plan, _ = _prepare_interrupted_transaction(tmp_path, "unsafe286")
    first = plan.changes[0]
    target = _safe_target(tmp_path, first.path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"operator-owned bytes after interruption\n")

    with pytest.raises(MigrationSafetyError, match="outside the recorded transaction"):
        apply_migration(tmp_path)

    assert target.read_bytes() == b"operator-owned bytes after interruption\n"
    assert not (tmp_path / ".sdai/migrations/unsafe286/recovery.json").exists()
    assert not (tmp_path / ".sdai/migrations/unsafe286/commit.json").exists()


def test_legacy_backup_only_interruption_requires_operator_review(tmp_path: Path) -> None:
    init_project(tmp_path)
    evidence = tmp_path / ".sdai" / "migrations" / "legacy286" / "backups"
    evidence.mkdir(parents=True)
    marker = evidence / "legacy-stock.bin"
    marker.write_bytes(b"old migration backup evidence\n")

    with pytest.raises(MigrationSafetyError, match="legacy interrupted upgrade"):
        apply_migration(tmp_path)

    assert marker.read_bytes() == b"old migration backup evidence\n"
    assert evidence.parent.is_dir()


def test_committed_recovery_protocol_migration_remains_rollback_safe(
    tmp_path: Path,
) -> None:
    init_project(tmp_path)
    before = _files(tmp_path)

    result = apply_migration(tmp_path)
    assert result.status == "applied"
    assert result.migration_id is not None
    record = tmp_path / ".sdai" / "migrations" / result.migration_id
    assert (record / "manifest.json").is_file()
    assert (record / "commit.json").is_file()
    assert not (record / "recovery.json").exists()

    rolled_back = rollback_migration(tmp_path, result.migration_id)
    assert rolled_back.status == "rolled-back"
    assert _files(tmp_path) == before

    repeated = rollback_migration(tmp_path, result.migration_id)
    assert repeated.status == "already-rolled-back"


def test_upgrade_preserves_brownfield_and_local_owned_content(tmp_path: Path) -> None:
    app = tmp_path / "src" / "service.py"
    app.parent.mkdir(parents=True)
    app.write_text("print('brownfield')\n", encoding="utf-8")
    readme = tmp_path / "README.md"
    readme.write_text("# Existing application\n", encoding="utf-8")

    init_project(tmp_path)
    local = tmp_path / ".sdai" / "local-team.yaml"
    local.write_text("owner: platform-team\n", encoding="utf-8")
    before_app = app.read_bytes()
    before_readme = readme.read_bytes()
    before_local = local.read_bytes()

    result = apply_migration(tmp_path)
    assert result.status == "applied"
    assert app.read_bytes() == before_app
    assert readme.read_bytes() == before_readme
    assert local.read_bytes() == before_local
    assert plan_migration(tmp_path).current
