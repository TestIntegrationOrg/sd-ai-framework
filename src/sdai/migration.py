from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import tempfile
import uuid

from sdai import __version__
from sdai.enterprise_scaffold import install_v04_scaffold
from sdai.path_safety import PathSafetyError, ensure_within_project
from sdai.scaffold import upgrade_project
from sdai.v05_scaffold import install_v05_scaffold
from sdai.versioning import write_framework_metadata
from sdai.workflow_templates import install_current_workflows


MIGRATION_PLAN_API_VERSION = "sdai.migration-plan/v1"
MIGRATION_RESULT_API_VERSION = "sdai.migration-result/v1"
MIGRATION_MANIFEST_API_VERSION = "sdai.migration-manifest/v1"
MIGRATION_ROLLBACK_API_VERSION = "sdai.migration-rollback/v1"

_MANAGED_ROOTS = (".sdai", ".agents")
_MIGRATION_ROOT = PurePosixPath(".sdai/migrations")
_SYMLINK_SENTINEL = b"SDAI-MIGRATION-SYMLINK-SENTINEL\n"


class MigrationError(RuntimeError):
    """Base class for migration planning/apply/rollback failures."""


class MigrationSafetyError(MigrationError):
    """Raised when migration cannot prove a write/rollback is safe."""


@dataclass(frozen=True, slots=True)
class MigrationChange:
    path: str
    action: str
    before_sha256: str | None
    after_sha256: str
    _before_bytes: bytes | None = field(default=None, repr=False, compare=False)
    _after_bytes: bytes = field(default=b"", repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.action not in {"create", "replace-stock"}:
            raise ValueError(f"unsupported migration action: {self.action!r}")
        _validate_managed_relative(self.path)
        if self.action == "create" and self.before_sha256 is not None:
            raise ValueError("create migration changes cannot have before_sha256")
        if self.action == "replace-stock" and self.before_sha256 is None:
            raise ValueError("replace-stock migration changes require before_sha256")

    def as_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "action": self.action,
            "beforeSha256": self.before_sha256,
            "afterSha256": self.after_sha256,
        }


@dataclass(frozen=True, slots=True)
class MigrationPlan:
    changes: tuple[MigrationChange, ...]

    def __post_init__(self) -> None:
        paths = [item.path for item in self.changes]
        if paths != sorted(paths):
            raise ValueError("migration changes must be sorted by path")
        if len(paths) != len(set(paths)):
            raise ValueError("migration change paths must be unique")

    def _body(self) -> dict[str, object]:
        return {
            "apiVersion": MIGRATION_PLAN_API_VERSION,
            "frameworkVersion": __version__,
            "strategy": "additive-safe-stock-upgrade",
            "changes": [item.as_dict() for item in self.changes],
        }

    @property
    def sha256(self) -> str:
        return _sha_json(self._body())

    @property
    def current(self) -> bool:
        return not self.changes

    def as_dict(self) -> dict[str, object]:
        payload = self._body()
        payload["planSha256"] = self.sha256
        return payload

    def to_json(self) -> str:
        return _canonical_json(self.as_dict()) + "\n"


@dataclass(frozen=True, slots=True)
class MigrationResult:
    status: str
    plan_sha256: str
    migration_id: str | None
    manifest_path: str | None
    changes: tuple[MigrationChange, ...]

    def _body(self) -> dict[str, object]:
        return {
            "apiVersion": MIGRATION_RESULT_API_VERSION,
            "frameworkVersion": __version__,
            "status": self.status,
            "migrationId": self.migration_id,
            "planSha256": self.plan_sha256,
            "manifestPath": self.manifest_path,
            "changes": [item.as_dict() for item in self.changes],
        }

    @property
    def sha256(self) -> str:
        return _sha_json(self._body())

    def as_dict(self) -> dict[str, object]:
        payload = self._body()
        payload["resultSha256"] = self.sha256
        return payload

    def to_json(self) -> str:
        return _canonical_json(self.as_dict()) + "\n"


@dataclass(frozen=True, slots=True)
class MigrationRollbackResult:
    status: str
    migration_id: str
    plan_sha256: str
    changes: tuple[dict[str, object], ...]

    def _body(self) -> dict[str, object]:
        return {
            "apiVersion": MIGRATION_ROLLBACK_API_VERSION,
            "frameworkVersion": __version__,
            "status": self.status,
            "migrationId": self.migration_id,
            "planSha256": self.plan_sha256,
            "changes": list(self.changes),
        }

    @property
    def sha256(self) -> str:
        return _sha_json(self._body())

    def as_dict(self) -> dict[str, object]:
        payload = self._body()
        payload["rollbackSha256"] = self.sha256
        return payload

    def to_json(self) -> str:
        return _canonical_json(self.as_dict()) + "\n"


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _sha_bytes(data: bytes) -> str:
    return "sha256:" + sha256(data).hexdigest()


def _sha_json(value: object) -> str:
    return _sha_bytes(_canonical_json(value).encode("utf-8"))


def _validate_project(root: Path) -> Path:
    resolved = root.resolve()
    config = ensure_within_project(
        resolved,
        resolved / ".sdai" / "config.yaml",
        label="SD-AI project config",
    )
    if not config.is_file():
        raise FileNotFoundError("Not an SD-AI project. Run `sdai init` first.")
    return resolved


def _validate_managed_relative(value: str) -> PurePosixPath:
    rel = PurePosixPath(value)
    if rel.is_absolute() or not rel.parts or ".." in rel.parts or "." in rel.parts:
        raise MigrationSafetyError(f"unsafe migration path: {value!r}")
    if rel.parts[0] not in _MANAGED_ROOTS:
        raise MigrationSafetyError(
            f"migration path '{value}' is outside managed SDAI roots"
        )
    if rel == _MIGRATION_ROOT or _MIGRATION_ROOT in rel.parents:
        raise MigrationSafetyError("migration plans cannot mutate migration evidence")
    return rel


def _safe_target(root: Path, value: str) -> Path:
    rel = _validate_managed_relative(value)
    candidate = root.joinpath(*rel.parts)
    try:
        ensure_within_project(root, candidate, label=f"migration target '{value}'")
    except PathSafetyError as exc:
        raise MigrationSafetyError(str(exc)) from exc

    current = root
    for part in rel.parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise MigrationSafetyError(
                f"migration target '{value}' traverses symlink '{current.relative_to(root).as_posix()}'"
            )
    if candidate.is_symlink():
        raise MigrationSafetyError(
            f"migration target '{value}' is a symlink and cannot be modified safely"
        )
    return candidate


def _managed_snapshot(root: Path) -> tuple[dict[str, bytes], tuple[str, ...]]:
    files: dict[str, bytes] = {}
    symlinks: set[str] = set()
    for root_name in _MANAGED_ROOTS:
        base = root / root_name
        if not base.exists() and not base.is_symlink():
            continue
        if base.is_symlink():
            symlinks.add(root_name)
            continue
        if not base.is_dir():
            raise MigrationSafetyError(f"managed SDAI root '{root_name}' must be a directory")
        for directory, dirnames, filenames in os.walk(base, followlinks=False):
            directory_path = Path(directory)
            rel_directory = directory_path.relative_to(root).as_posix()
            if rel_directory == ".sdai" and "migrations" in dirnames:
                dirnames.remove("migrations")

            retained_dirs: list[str] = []
            for name in dirnames:
                child = directory_path / name
                rel = child.relative_to(root).as_posix()
                if child.is_symlink():
                    symlinks.add(rel)
                else:
                    retained_dirs.append(name)
            dirnames[:] = retained_dirs

            for name in filenames:
                child = directory_path / name
                rel = child.relative_to(root).as_posix()
                if child.is_symlink():
                    symlinks.add(rel)
                    files[rel] = _SYMLINK_SENTINEL
                    continue
                if not child.is_file():
                    raise MigrationSafetyError(
                        f"managed SDAI path '{rel}' is not a regular file"
                    )
                files[rel] = child.read_bytes()
    return files, tuple(sorted(symlinks))


def _copy_managed_snapshot(source: Path, destination: Path) -> tuple[str, ...]:
    files, symlinks = _managed_snapshot(source)
    for rel, data in files.items():
        target = destination.joinpath(*PurePosixPath(rel).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    for rel in symlinks:
        path = PurePosixPath(rel)
        target = destination.joinpath(*path.parts)
        if rel in files:
            continue
        target.mkdir(parents=True, exist_ok=True)
    return symlinks


def _install_current_scaffold(root: Path) -> None:
    upgrade_project(root)
    install_v04_scaffold(root)
    install_v05_scaffold(root)
    install_current_workflows(root)
    write_framework_metadata(root)


def _blocked_by_symlink(path: str, symlinks: tuple[str, ...]) -> str | None:
    for item in symlinks:
        if path == item or path.startswith(item + "/"):
            return item
    return None


def plan_migration(project_root: Path) -> MigrationPlan:
    """Build the exact current-scaffold delta without mutating the project."""

    root = _validate_project(project_root)
    before, _ = _managed_snapshot(root)
    with tempfile.TemporaryDirectory(prefix="sdai-migration-plan-") as temp_dir:
        mirror = Path(temp_dir) / "project"
        mirror.mkdir(parents=True)
        symlinks = _copy_managed_snapshot(root, mirror)
        _install_current_scaffold(mirror)
        after, _ = _managed_snapshot(mirror)

    changes: list[MigrationChange] = []
    for path in sorted(set(before) | set(after)):
        before_bytes = before.get(path)
        after_bytes = after.get(path)
        if before_bytes == after_bytes:
            continue
        if after_bytes is None:
            raise MigrationSafetyError(
                f"current scaffold unexpectedly deletes managed path '{path}'"
            )
        blocked = _blocked_by_symlink(path, symlinks)
        if blocked is not None:
            raise MigrationSafetyError(
                f"migration target '{path}' is blocked by managed symlink '{blocked}'"
            )
        _safe_target(root, path)
        action = "create" if path not in before else "replace-stock"
        changes.append(
            MigrationChange(
                path=path,
                action=action,
                before_sha256=(
                    None if before_bytes is None else _sha_bytes(before_bytes)
                ),
                after_sha256=_sha_bytes(after_bytes),
                _before_bytes=before_bytes,
                _after_bytes=after_bytes,
            )
        )
    return MigrationPlan(tuple(changes))


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing_mode: int | None = None
    if path.exists():
        existing_mode = stat.S_IMODE(path.stat().st_mode)
    temporary = path.with_name(f".{path.name}.sdai-migration-{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if existing_mode is not None:
            os.chmod(temporary, existing_mode)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _preflight_apply(root: Path, plan: MigrationPlan) -> None:
    for change in plan.changes:
        target = _safe_target(root, change.path)
        if change.action == "create":
            if target.exists():
                raise MigrationSafetyError(
                    f"migration target '{change.path}' appeared after planning; re-plan before applying"
                )
            continue
        if not target.is_file():
            raise MigrationSafetyError(
                f"migration target '{change.path}' disappeared after planning; re-plan before applying"
            )
        current = _sha_bytes(target.read_bytes())
        if current != change.before_sha256:
            raise MigrationSafetyError(
                f"migration target '{change.path}' changed after planning; re-plan before applying"
            )


def _manifest_body(
    migration_id: str,
    plan: MigrationPlan,
    backup_paths: dict[str, str],
) -> dict[str, object]:
    changes: list[dict[str, object]] = []
    for change in plan.changes:
        item = change.as_dict()
        backup = backup_paths.get(change.path)
        if backup is not None:
            item["backupPath"] = backup
            item["backupSha256"] = change.before_sha256
        changes.append(item)
    return {
        "apiVersion": MIGRATION_MANIFEST_API_VERSION,
        "frameworkVersion": __version__,
        "migrationId": migration_id,
        "planSha256": plan.sha256,
        "changes": changes,
    }


def _write_canonical_json(path: Path, payload: dict[str, object]) -> None:
    _atomic_write(path, (_canonical_json(payload) + "\n").encode("utf-8"))


def apply_migration(project_root: Path) -> MigrationResult:
    """Apply the exact safe scaffold delta and record reversible integrity evidence."""

    root = _validate_project(project_root)
    plan = plan_migration(root)
    if plan.current:
        return MigrationResult("current", plan.sha256, None, None, ())

    _preflight_apply(root, plan)
    migration_id = uuid.uuid4().hex
    record_root = _safe_target(root, f".sdai/migrations/{migration_id}/manifest.json").parent
    record_root.mkdir(parents=True, exist_ok=False)

    backup_paths: dict[str, str] = {}
    applied: list[MigrationChange] = []
    try:
        for change in plan.changes:
            if change._before_bytes is None:
                continue
            backup_rel = (
                PurePosixPath(".sdai/migrations")
                / migration_id
                / "backups"
                / PurePosixPath(change.path)
            ).as_posix()
            backup_target = root.joinpath(*PurePosixPath(backup_rel).parts)
            _atomic_write(backup_target, change._before_bytes)
            if _sha_bytes(backup_target.read_bytes()) != change.before_sha256:
                raise MigrationSafetyError(
                    f"backup integrity verification failed for '{change.path}'"
                )
            backup_paths[change.path] = backup_rel

        for change in plan.changes:
            target = _safe_target(root, change.path)
            _atomic_write(target, change._after_bytes)
            if _sha_bytes(target.read_bytes()) != change.after_sha256:
                raise MigrationSafetyError(
                    f"post-write integrity verification failed for '{change.path}'"
                )
            applied.append(change)

        manifest = _manifest_body(migration_id, plan, backup_paths)
        manifest["manifestSha256"] = _sha_json(manifest)
        manifest_path = record_root / "manifest.json"
        _write_canonical_json(manifest_path, manifest)
    except Exception:
        for change in reversed(applied):
            target = _safe_target(root, change.path)
            if change._before_bytes is None:
                if target.exists() and not target.is_symlink():
                    target.unlink()
            else:
                _atomic_write(target, change._before_bytes)
        shutil.rmtree(record_root, ignore_errors=True)
        raise

    return MigrationResult(
        status="applied",
        plan_sha256=plan.sha256,
        migration_id=migration_id,
        manifest_path=manifest_path.relative_to(root).as_posix(),
        changes=plan.changes,
    )


def _load_integrity_json(path: Path, hash_field: str) -> dict[str, object]:
    if not path.is_file() or path.is_symlink():
        raise MigrationSafetyError(f"migration evidence is missing or unsafe: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MigrationSafetyError(f"migration evidence is invalid JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise MigrationSafetyError(f"migration evidence must be a JSON object: {path}")
    claimed = payload.get(hash_field)
    if not isinstance(claimed, str):
        raise MigrationSafetyError(f"migration evidence has no {hash_field}: {path}")
    body = dict(payload)
    body.pop(hash_field, None)
    if claimed != _sha_json(body):
        raise MigrationSafetyError(f"migration evidence integrity mismatch: {path}")
    return payload


def _manifest_changes(payload: dict[str, object]) -> tuple[dict[str, object], ...]:
    raw = payload.get("changes")
    if not isinstance(raw, list):
        raise MigrationSafetyError("migration manifest changes must be a list")
    changes: list[dict[str, object]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise MigrationSafetyError("migration manifest change must be an object")
        path = item.get("path")
        action = item.get("action")
        before = item.get("beforeSha256")
        after = item.get("afterSha256")
        if not isinstance(path, str) or action not in {"create", "replace-stock"}:
            raise MigrationSafetyError("migration manifest change identity is invalid")
        _validate_managed_relative(path)
        if not isinstance(after, str):
            raise MigrationSafetyError("migration manifest afterSha256 is invalid")
        if action == "create" and before is not None:
            raise MigrationSafetyError("create manifest change has unexpected beforeSha256")
        if action == "replace-stock" and not isinstance(before, str):
            raise MigrationSafetyError("replace-stock manifest change lacks beforeSha256")
        changes.append(dict(item))
    paths = [str(item["path"]) for item in changes]
    if paths != sorted(paths) or len(paths) != len(set(paths)):
        raise MigrationSafetyError("migration manifest changes are not canonical")
    return tuple(changes)


def _preflight_rollback(
    root: Path,
    changes: tuple[dict[str, object], ...],
) -> dict[str, bytes]:
    backups: dict[str, bytes] = {}
    for item in changes:
        path = str(item["path"])
        target = _safe_target(root, path)
        if not target.is_file():
            raise MigrationSafetyError(
                f"cannot rollback '{path}': migrated file is missing"
            )
        current = _sha_bytes(target.read_bytes())
        if current != item["afterSha256"]:
            raise MigrationSafetyError(
                f"cannot rollback '{path}': file changed after migration"
            )
        if item["action"] != "replace-stock":
            continue
        backup_rel = item.get("backupPath")
        if not isinstance(backup_rel, str):
            raise MigrationSafetyError(f"cannot rollback '{path}': backup path is missing")
        backup_path = _safe_target(root, backup_rel)
        if not backup_path.is_file():
            raise MigrationSafetyError(f"cannot rollback '{path}': backup is missing")
        backup_bytes = backup_path.read_bytes()
        if _sha_bytes(backup_bytes) != item.get("backupSha256"):
            raise MigrationSafetyError(f"cannot rollback '{path}': backup integrity mismatch")
        backups[path] = backup_bytes
    return backups


def _prune_empty_managed_parents(root: Path, path: Path) -> None:
    current = path.parent
    protected = {root, root / ".sdai", root / ".agents"}
    while current not in protected:
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent


def rollback_migration(project_root: Path, migration_id: str) -> MigrationRollbackResult:
    """Rollback one recorded migration only while every migrated byte still matches."""

    root = _validate_project(project_root)
    if not migration_id or not migration_id.isalnum():
        raise MigrationSafetyError("migration id must be a non-empty alphanumeric value")
    record_root = _safe_target(root, f".sdai/migrations/{migration_id}/manifest.json").parent
    manifest_path = record_root / "manifest.json"
    manifest = _load_integrity_json(manifest_path, "manifestSha256")
    if manifest.get("apiVersion") != MIGRATION_MANIFEST_API_VERSION:
        raise MigrationSafetyError("unsupported migration manifest apiVersion")
    if manifest.get("migrationId") != migration_id:
        raise MigrationSafetyError("migration manifest id does not match requested migration")
    plan_sha = manifest.get("planSha256")
    if not isinstance(plan_sha, str):
        raise MigrationSafetyError("migration manifest planSha256 is invalid")
    changes = _manifest_changes(manifest)

    receipt_path = record_root / "rollback.json"
    if receipt_path.exists():
        receipt = _load_integrity_json(receipt_path, "rollbackSha256")
        if receipt.get("migrationId") != migration_id or receipt.get("planSha256") != plan_sha:
            raise MigrationSafetyError("rollback receipt does not match migration manifest")
        return MigrationRollbackResult(
            status="already-rolled-back",
            migration_id=migration_id,
            plan_sha256=plan_sha,
            changes=changes,
        )

    backups = _preflight_rollback(root, changes)
    for item in reversed(changes):
        path = str(item["path"])
        target = _safe_target(root, path)
        if item["action"] == "create":
            target.unlink()
            _prune_empty_managed_parents(root, target)
        else:
            _atomic_write(target, backups[path])

    result = MigrationRollbackResult(
        status="rolled-back",
        migration_id=migration_id,
        plan_sha256=plan_sha,
        changes=changes,
    )
    _write_canonical_json(receipt_path, result.as_dict())
    return result


__all__ = [
    "MIGRATION_MANIFEST_API_VERSION",
    "MIGRATION_PLAN_API_VERSION",
    "MIGRATION_RESULT_API_VERSION",
    "MIGRATION_ROLLBACK_API_VERSION",
    "MigrationChange",
    "MigrationError",
    "MigrationPlan",
    "MigrationResult",
    "MigrationRollbackResult",
    "MigrationSafetyError",
    "apply_migration",
    "plan_migration",
    "rollback_migration",
]
