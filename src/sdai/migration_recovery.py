from __future__ import annotations

from pathlib import Path, PurePosixPath
import shutil

from sdai import __version__
from sdai.migration import (
    MigrationResult,
    MigrationRollbackResult,
    MigrationSafetyError,
    _atomic_write,
    _load_integrity_json,
    _manifest_body,
    _manifest_changes,
    _preflight_apply,
    _prune_empty_managed_parents,
    _safe_evidence_target,
    _safe_target,
    _sha_bytes,
    _sha_json,
    _validate_project,
    _write_canonical_json,
    plan_migration as _plan_migration,
    rollback_migration as _rollback_migration,
)


MIGRATION_TRANSACTION_PROTOCOL = "sdai.migration-transaction/prepare-commit/v1"
MIGRATION_COMMIT_API_VERSION = "sdai.migration-commit/v1"
MIGRATION_RECOVERY_API_VERSION = "sdai.migration-recovery/v1"


def _record_relative(migration_id: str, name: str) -> str:
    return f".sdai/migrations/{migration_id}/{name}"


def _migration_records(root: Path) -> tuple[Path, ...]:
    evidence_root = root / ".sdai" / "migrations"
    if not evidence_root.exists():
        return ()
    if evidence_root.is_symlink() or not evidence_root.is_dir():
        raise MigrationSafetyError("migration evidence root is unsafe")

    records: list[Path] = []
    for child in sorted(evidence_root.iterdir(), key=lambda item: item.name):
        if child.is_symlink() or not child.is_dir():
            raise MigrationSafetyError(
                f"unexpected migration evidence entry: {child.relative_to(root).as_posix()}"
            )
        if not child.name.isalnum():
            raise MigrationSafetyError(f"unsafe migration record id: {child.name!r}")
        records.append(child)
    return tuple(records)


def _safe_unprepared_record(record: Path) -> bool:
    """Return true only for a pre-manifest record that can never have mutated targets."""

    for path in record.rglob("*"):
        if path.is_symlink():
            return False
        relative = path.relative_to(record)
        if not relative.parts or relative.parts[0] != "backups":
            return False
        if not path.is_dir() and not path.is_file():
            return False
    return True


def _verify_receipt(
    root: Path,
    manifest: dict[str, object],
    *,
    path_value: object,
    api_version: str,
    hash_field: str,
) -> dict[str, object]:
    if not isinstance(path_value, str):
        raise MigrationSafetyError("migration transaction receipt path is invalid")
    receipt_path = _safe_evidence_target(root, path_value)
    receipt = _load_integrity_json(receipt_path, hash_field)
    if receipt.get("apiVersion") != api_version:
        raise MigrationSafetyError("unsupported migration transaction receipt apiVersion")
    if receipt.get("migrationId") != manifest.get("migrationId"):
        raise MigrationSafetyError("migration transaction receipt id mismatch")
    if receipt.get("planSha256") != manifest.get("planSha256"):
        raise MigrationSafetyError("migration transaction receipt plan mismatch")
    if receipt.get("manifestSha256") != manifest.get("manifestSha256"):
        raise MigrationSafetyError("migration transaction receipt manifest mismatch")
    return receipt


def _transaction_state(
    root: Path,
    record: Path,
) -> tuple[str, dict[str, object] | None]:
    migration_id = record.name
    manifest_path = _safe_evidence_target(
        root,
        _record_relative(migration_id, "manifest.json"),
    )
    if not manifest_path.exists():
        if not _safe_unprepared_record(record):
            raise MigrationSafetyError(
                f"migration record '{migration_id}' has no safe preparation manifest"
            )
        return "unprepared", None

    manifest = _load_integrity_json(manifest_path, "manifestSha256")
    if manifest.get("migrationId") != migration_id:
        raise MigrationSafetyError("migration manifest id does not match evidence directory")

    protocol = manifest.get("transactionProtocol")
    if protocol is None:
        # Manifests produced by 1.0.3 predate the crash-recovery protocol and were
        # written only after target writes completed, so their existence is the
        # historical commit marker.
        return "legacy-committed", manifest
    if protocol != MIGRATION_TRANSACTION_PROTOCOL:
        raise MigrationSafetyError("unsupported migration transaction protocol")

    commit_rel = manifest.get("commitPath")
    recovery_rel = manifest.get("recoveryPath")
    expected_commit = _record_relative(migration_id, "commit.json")
    expected_recovery = _record_relative(migration_id, "recovery.json")
    if commit_rel != expected_commit or recovery_rel != expected_recovery:
        raise MigrationSafetyError("migration transaction receipt paths are not canonical")

    commit_path = _safe_evidence_target(root, expected_commit)
    recovery_path = _safe_evidence_target(root, expected_recovery)
    if commit_path.exists() and recovery_path.exists():
        raise MigrationSafetyError(
            f"migration '{migration_id}' has both commit and recovery receipts"
        )
    if commit_path.exists():
        _verify_receipt(
            root,
            manifest,
            path_value=commit_rel,
            api_version=MIGRATION_COMMIT_API_VERSION,
            hash_field="commitSha256",
        )
        return "committed", manifest
    if recovery_path.exists():
        _verify_receipt(
            root,
            manifest,
            path_value=recovery_rel,
            api_version=MIGRATION_RECOVERY_API_VERSION,
            hash_field="recoverySha256",
        )
        return "recovered", manifest
    return "pending", manifest


def _pending_records(
    root: Path,
) -> tuple[tuple[Path, str, dict[str, object] | None], ...]:
    pending: list[tuple[Path, str, dict[str, object] | None]] = []
    for record in _migration_records(root):
        state, manifest = _transaction_state(root, record)
        if state in {"unprepared", "pending"}:
            pending.append((record, state, manifest))
    return tuple(pending)


def _write_receipt(
    root: Path,
    manifest: dict[str, object],
    *,
    kind: str,
) -> None:
    migration_id = str(manifest["migrationId"])
    if kind == "commit":
        api_version = MIGRATION_COMMIT_API_VERSION
        path_key = "commitPath"
        hash_field = "commitSha256"
        status = "committed"
    elif kind == "recovery":
        api_version = MIGRATION_RECOVERY_API_VERSION
        path_key = "recoveryPath"
        hash_field = "recoverySha256"
        status = "recovered"
    else:
        raise ValueError(f"unknown migration receipt kind: {kind}")

    path_value = manifest.get(path_key)
    if not isinstance(path_value, str):
        raise MigrationSafetyError(f"migration {kind} receipt path is missing")
    receipt_path = _safe_evidence_target(root, path_value)
    body: dict[str, object] = {
        "apiVersion": api_version,
        "frameworkVersion": __version__,
        "migrationId": migration_id,
        "planSha256": manifest["planSha256"],
        "manifestSha256": manifest["manifestSha256"],
        "status": status,
    }
    body[hash_field] = _sha_json(body)
    _write_canonical_json(receipt_path, body)


def _prepare_backups(
    root: Path,
    migration_id: str,
    plan,
) -> dict[str, str]:
    backup_paths: dict[str, str] = {}
    for change in plan.changes:
        if change._before_bytes is None:
            continue
        backup_rel = (
            PurePosixPath(".sdai/migrations")
            / migration_id
            / "backups"
            / PurePosixPath(change.path)
        ).as_posix()
        backup_target = _safe_evidence_target(root, backup_rel)
        _atomic_write(backup_target, change._before_bytes)
        if _sha_bytes(backup_target.read_bytes()) != change.before_sha256:
            raise MigrationSafetyError(
                f"backup integrity verification failed for '{change.path}'"
            )
        backup_paths[change.path] = backup_rel
    return backup_paths


def _build_prepared_manifest(
    migration_id: str,
    plan,
    backup_paths: dict[str, str],
) -> dict[str, object]:
    manifest = _manifest_body(migration_id, plan, backup_paths)
    manifest["transactionProtocol"] = MIGRATION_TRANSACTION_PROTOCOL
    manifest["commitPath"] = _record_relative(migration_id, "commit.json")
    manifest["recoveryPath"] = _record_relative(migration_id, "recovery.json")
    manifest["manifestSha256"] = _sha_json(manifest)
    return manifest


def _preflight_recovery(
    root: Path,
    migration_id: str,
    manifest: dict[str, object],
) -> tuple[
    tuple[dict[str, object], ...],
    dict[str, bytes],
    dict[str, str],
]:
    changes = _manifest_changes(manifest)
    backups: dict[str, bytes] = {}
    states: dict[str, str] = {}

    for item in changes:
        path = str(item["path"])
        target = _safe_target(root, path)
        action = str(item["action"])
        before_sha = item.get("beforeSha256")
        after_sha = str(item["afterSha256"])

        if action == "replace-stock":
            expected_backup = (
                PurePosixPath(".sdai/migrations")
                / migration_id
                / "backups"
                / PurePosixPath(path)
            ).as_posix()
            if item.get("backupPath") != expected_backup:
                raise MigrationSafetyError(
                    f"cannot recover '{path}': backup path is not canonical"
                )
            if item.get("backupSha256") != before_sha:
                raise MigrationSafetyError(
                    f"cannot recover '{path}': backup hash does not match pre-migration bytes"
                )
            backup_path = _safe_evidence_target(root, expected_backup)
            if not backup_path.is_file() or backup_path.is_symlink():
                raise MigrationSafetyError(f"cannot recover '{path}': backup is missing")
            backup_bytes = backup_path.read_bytes()
            if _sha_bytes(backup_bytes) != before_sha:
                raise MigrationSafetyError(
                    f"cannot recover '{path}': backup integrity mismatch"
                )
            backups[path] = backup_bytes
        elif item.get("backupPath") is not None or item.get("backupSha256") is not None:
            raise MigrationSafetyError(
                f"cannot recover '{path}': create change has unexpected backup evidence"
            )

        if not target.exists():
            if action == "create":
                states[path] = "before"
                continue
            raise MigrationSafetyError(
                f"cannot recover '{path}': stock target is missing"
            )
        if not target.is_file() or target.is_symlink():
            raise MigrationSafetyError(
                f"cannot recover '{path}': target is not a safe regular file"
            )

        current_sha = _sha_bytes(target.read_bytes())
        if current_sha == after_sha:
            states[path] = "after"
        elif action == "replace-stock" and current_sha == before_sha:
            states[path] = "before"
        else:
            raise MigrationSafetyError(
                f"cannot recover '{path}': target contains bytes outside the recorded transaction"
            )

    return changes, backups, states


def _recover_record(
    root: Path,
    record: Path,
    manifest: dict[str, object],
) -> None:
    migration_id = record.name
    changes, backups, states = _preflight_recovery(root, migration_id, manifest)

    # Recovery is itself restart-safe. A second interruption may leave a mixture of
    # pre-migration and post-migration bytes; the next pass accepts only those two
    # exact recorded states and continues restoring toward the pre-migration state.
    for item in reversed(changes):
        path = str(item["path"])
        if states[path] != "after":
            continue
        target = _safe_target(root, path)
        if item["action"] == "create":
            target.unlink()
            _prune_empty_managed_parents(root, target)
        else:
            _atomic_write(target, backups[path])

    for item in changes:
        path = str(item["path"])
        target = _safe_target(root, path)
        if item["action"] == "create":
            if target.exists():
                raise MigrationSafetyError(
                    f"recovery verification failed for created target '{path}'"
                )
            continue
        if not target.is_file() or _sha_bytes(target.read_bytes()) != item["beforeSha256"]:
            raise MigrationSafetyError(
                f"recovery verification failed for stock target '{path}'"
            )

    _write_receipt(root, manifest, kind="recovery")


def _recover_interrupted_state(root: Path) -> tuple[str, ...]:
    pending = _pending_records(root)
    if len(pending) > 1:
        ids = ", ".join(record.name for record, _, _ in pending)
        raise MigrationSafetyError(
            f"multiple interrupted migration transactions require operator review: {ids}"
        )

    recovered: list[str] = []
    for record, state, manifest in pending:
        if state == "unprepared":
            # The protocol never mutates project targets before the canonical manifest
            # is durably written. A record containing only backups is therefore safe
            # to discard and rebuild.
            shutil.rmtree(record)
            recovered.append(record.name)
            continue
        assert manifest is not None
        _recover_record(root, record, manifest)
        recovered.append(record.name)
    return tuple(recovered)


def plan_migration(project_root: Path):
    """Plan without hiding a prior interrupted transaction behind a new delta."""

    root = _validate_project(project_root)
    pending = _pending_records(root)
    if pending:
        migration_id = pending[0][0].name
        raise MigrationSafetyError(
            f"interrupted migration '{migration_id}' requires recovery; "
            "run `sdai migrate apply` or `sdai upgrade` to recover it before planning"
        )
    return _plan_migration(root)


def apply_migration(project_root: Path) -> MigrationResult:
    """Apply a migration with durable prepare/commit evidence and restart recovery."""

    root = _validate_project(project_root)
    _recover_interrupted_state(root)
    plan = _plan_migration(root)
    if plan.current:
        return MigrationResult("current", plan.sha256, None, None, ())

    _preflight_apply(root, plan)
    import uuid

    migration_id = uuid.uuid4().hex
    manifest_rel = _record_relative(migration_id, "manifest.json")
    manifest_path = _safe_evidence_target(root, manifest_rel)
    record_root = manifest_path.parent
    record_root.mkdir(parents=True, exist_ok=False)

    manifest_written = False
    manifest: dict[str, object] | None = None
    try:
        backup_paths = _prepare_backups(root, migration_id, plan)
        manifest = _build_prepared_manifest(migration_id, plan, backup_paths)
        _write_canonical_json(manifest_path, manifest)
        manifest_written = True

        for change in plan.changes:
            target = _safe_target(root, change.path)
            _atomic_write(target, change._after_bytes)
            if _sha_bytes(target.read_bytes()) != change.after_sha256:
                raise MigrationSafetyError(
                    f"post-write integrity verification failed for '{change.path}'"
                )

        _write_receipt(root, manifest, kind="commit")
    except Exception as exc:
        if manifest_written and manifest is not None:
            try:
                _recover_record(root, record_root, manifest)
            except Exception as recovery_exc:
                raise MigrationSafetyError(
                    "migration apply failed and automatic recovery could not complete; "
                    "retry `sdai migrate apply` or `sdai upgrade` after resolving the reported safety error"
                ) from recovery_exc
        else:
            shutil.rmtree(record_root, ignore_errors=True)
        raise exc

    return MigrationResult(
        status="applied",
        plan_sha256=plan.sha256,
        migration_id=migration_id,
        manifest_path=manifest_path.relative_to(root).as_posix(),
        changes=plan.changes,
    )


def rollback_migration(
    project_root: Path,
    migration_id: str,
) -> MigrationRollbackResult:
    """Rollback only committed migrations; interrupted applies recover before retry."""

    root = _validate_project(project_root)
    if not migration_id or not migration_id.isalnum():
        raise MigrationSafetyError("migration id must be a non-empty alphanumeric value")
    record = root / ".sdai" / "migrations" / migration_id
    if not record.exists() or record.is_symlink() or not record.is_dir():
        return _rollback_migration(root, migration_id)

    state, _ = _transaction_state(root, record)
    if state in {"unprepared", "pending"}:
        raise MigrationSafetyError(
            f"migration '{migration_id}' was interrupted before commit; "
            "run `sdai migrate apply` or `sdai upgrade` to recover and retry"
        )
    if state == "recovered":
        raise MigrationSafetyError(
            f"migration '{migration_id}' was recovered before commit and has nothing to rollback"
        )
    return _rollback_migration(root, migration_id)


__all__ = [
    "MIGRATION_COMMIT_API_VERSION",
    "MIGRATION_RECOVERY_API_VERSION",
    "MIGRATION_TRANSACTION_PROTOCOL",
    "apply_migration",
    "plan_migration",
    "rollback_migration",
]
