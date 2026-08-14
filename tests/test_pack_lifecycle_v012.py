from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path

import pytest
import yaml

from sdai.pack_integrity import build_pack_content_index
from sdai.pack_lifecycle import (
    ManagedFile,
    OperationJournal,
    PackLifecycleError,
    install_from_local,
    install_state_path,
    load_install_state,
    operation_journal_path,
    outdated_packs,
    remove_pack,
)
from sdai.pack_lock import PackLock, PackLockEntry
from sdai.pack_manifest import PACK_MANIFEST_API_VERSION, SemVer, load_pack_manifest


def _raw_manifest(version: str = "1.2.3") -> dict[str, object]:
    return {
        "apiVersion": PACK_MANIFEST_API_VERSION,
        "id": "secure-coding",
        "publisher": "acme",
        "version": version,
        "description": "Secure café engineering Δ pack",
        "capabilities": ["skills", "workflows"],
        "contentRoots": ["skills", "workflows"],
        "dependencies": [],
        "compatibility": {
            "framework": ">=0.5.4,<1.0.0",
            "apis": ["sdai.pack-manifest/v1"],
        },
    }


def _pack(root: Path, version: str = "1.2.3", *, review: str = "Review Δ requirements.\n"):
    (root / "skills" / "café").mkdir(parents=True)
    (root / "workflows").mkdir(parents=True)
    (root / "skills" / "café" / "review.md").write_text(review, encoding="utf-8", newline="\n")
    (root / "workflows" / "secure.yaml").write_text(
        "steps:\n  - verify\n", encoding="utf-8", newline="\n"
    )
    manifest_path = root / "pack.yaml"
    manifest_path.write_text(
        yaml.safe_dump(_raw_manifest(version), sort_keys=False, allow_unicode=True),
        encoding="utf-8",
        newline="\n",
    )
    manifest = load_pack_manifest(manifest_path)
    content = build_pack_content_index(root, manifest)
    lock = PackLock(
        roots=(manifest.identity,),
        packages=(
            PackLockEntry(
                publisher=manifest.publisher,
                id=manifest.id,
                version=manifest.version,
                source=f"file://artifact/{manifest.identity}",
                manifest_sha256=manifest.sha256,
                content_sha256=content.sha256,
                dependencies=(),
            ),
        ),
    )
    return manifest, content, lock


def _project(root: Path) -> None:
    (root / ".sdai").mkdir(parents=True)
    (root / ".sdai" / "config.yaml").write_text("provider: mock\n", encoding="utf-8")


def test_install_is_exact_lock_driven_utf8_and_idempotent(tmp_path: Path) -> None:
    project = tmp_path / "project"
    source = tmp_path / "source"
    _project(project)
    manifest, _, lock = _pack(source)

    first = install_from_local(project, source, lock, manifest.coordinate)
    second = install_from_local(project, source, lock, manifest.coordinate)

    assert first == second
    assert first.mode == "installed"
    assert first.lock_sha256 == lock.sha256
    assert first.preserved_paths == ()
    managed = project / ".sdai" / "installed-packs" / "acme" / "secure-coding" / "1.2.3" / "skills" / "café" / "review.md"
    assert managed.read_text(encoding="utf-8") == "Review Δ requirements.\n"
    state = load_install_state(project)
    assert state.packs == (first,)
    assert json.loads(install_state_path(project).read_text(encoding="utf-8"))["apiVersion"] == "sdai.pack-install-state/v1"
    assert not operation_journal_path(project).exists()


def test_install_rejects_manifest_or_content_not_matching_lock(tmp_path: Path) -> None:
    project = tmp_path / "project"
    source = tmp_path / "source"
    _project(project)
    manifest, _, lock = _pack(source)
    (source / "skills" / "café" / "review.md").write_text("tampered\n", encoding="utf-8")

    with pytest.raises(PackLifecycleError, match="content does not match exact lock"):
        install_from_local(project, source, lock, manifest.coordinate)


def test_remove_preserves_user_modified_managed_content(tmp_path: Path) -> None:
    project = tmp_path / "project"
    source = tmp_path / "source"
    _project(project)
    manifest, _, lock = _pack(source)
    record = install_from_local(project, source, lock, manifest.coordinate)
    changed = project / record.files[0].path
    changed.write_text("user edit Δ\n", encoding="utf-8")

    preserved = remove_pack(project, manifest.coordinate)

    assert record.files[0].path in preserved
    assert changed.read_text(encoding="utf-8") == "user edit Δ\n"
    assert load_install_state(project).packs == ()
    # Other clean Pack-managed files are removed, while user content is never deleted.
    for managed in record.files[1:]:
        assert not (project / managed.path).exists()


def test_update_preserves_modified_obsolete_version_and_records_provenance(tmp_path: Path) -> None:
    project = tmp_path / "project"
    old_source = tmp_path / "old"
    new_source = tmp_path / "new"
    _project(project)
    old_manifest, _, old_lock = _pack(old_source, "1.2.3")
    old = install_from_local(project, old_source, old_lock, old_manifest.coordinate)
    old_changed = project / old.files[0].path
    old_changed.write_text("user-owned after edit\n", encoding="utf-8")

    new_manifest, _, new_lock = _pack(new_source, "1.3.0", review="new Pack bytes\n")
    updated = install_from_local(project, new_source, new_lock, new_manifest.coordinate)

    assert updated.identity == "acme/secure-coding@1.3.0"
    assert old.files[0].path in updated.preserved_paths
    assert old_changed.read_text(encoding="utf-8") == "user-owned after edit\n"
    assert all("/1.3.0/" in item.path for item in updated.files)


def test_local_link_is_explicit_non_production_provenance_not_a_filesystem_symlink(tmp_path: Path) -> None:
    project = tmp_path / "project"
    source = tmp_path / "source"
    _project(project)
    manifest, _, lock = _pack(source)

    record = install_from_local(project, source, lock, manifest.coordinate, local_link=True)

    assert record.mode == "local-link"
    assert record.local_path == source.resolve().as_posix()
    assert record.source.startswith("local-link:")
    assert all(not (project / item.path).is_symlink() for item in record.files)
    raw = json.loads(install_state_path(project).read_text(encoding="utf-8"))["packs"][0]
    assert raw["apiVersion"] == "sdai.pack-local-link/v1"


def test_interrupted_install_can_adopt_only_byte_identical_planned_output(tmp_path: Path) -> None:
    project = tmp_path / "project"
    source = tmp_path / "source"
    _project(project)
    manifest, _, lock = _pack(source)

    source_file = source / "skills" / "café" / "review.md"
    data = source_file.read_bytes()
    destination_relative = ".sdai/installed-packs/acme/secure-coding/1.2.3/skills/café/review.md"
    destination = project / destination_relative
    destination.parent.mkdir(parents=True)
    destination.write_bytes(data)
    planned = ManagedFile(destination_relative, "sha256:" + sha256(data).hexdigest(), "skills/café/review.md")
    # Include every planned file in the stale journal, exactly as a hard crash would leave it.
    workflow_data = (source / "workflows" / "secure.yaml").read_bytes()
    workflow = ManagedFile(
        ".sdai/installed-packs/acme/secure-coding/1.2.3/workflows/secure.yaml",
        "sha256:" + sha256(workflow_data).hexdigest(),
        "workflows/secure.yaml",
    )
    operation_journal_path(project).parent.mkdir(parents=True, exist_ok=True)
    operation_journal_path(project).write_text(
        OperationJournal("install", manifest.coordinate, manifest.identity, tuple(sorted((planned, workflow), key=lambda item: item.path))).to_text(),
        encoding="utf-8",
    )

    recovered = install_from_local(project, source, lock, manifest.coordinate)

    assert recovered.identity == manifest.identity
    assert not operation_journal_path(project).exists()


def test_interrupted_install_rejects_changed_leftover_output(tmp_path: Path) -> None:
    project = tmp_path / "project"
    source = tmp_path / "source"
    _project(project)
    manifest, _, lock = _pack(source)
    record = install_from_local(project, source, lock, manifest.coordinate)
    remove_pack(project, manifest.coordinate)

    planned = record.files
    operation_journal_path(project).parent.mkdir(parents=True, exist_ok=True)
    operation_journal_path(project).write_text(
        OperationJournal("install", manifest.coordinate, manifest.identity, planned).to_text(),
        encoding="utf-8",
    )
    target = project / planned[0].path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("not the planned bytes\n", encoding="utf-8")

    with pytest.raises(PackLifecycleError, match="no longer matches planned bytes"):
        install_from_local(project, source, lock, manifest.coordinate)


def test_outdated_detects_exact_lock_change(tmp_path: Path) -> None:
    project = tmp_path / "project"
    source = tmp_path / "source"
    newer = tmp_path / "newer"
    _project(project)
    manifest, _, lock = _pack(source)
    installed = install_from_local(project, source, lock, manifest.coordinate)
    assert outdated_packs(load_install_state(project), lock) == ()

    _, _, new_lock = _pack(newer, "1.3.0")
    assert outdated_packs(load_install_state(project), new_lock) == (installed,)


def test_managed_destination_rejects_symlink_ancestor(tmp_path: Path) -> None:
    project = tmp_path / "project"
    source = tmp_path / "source"
    outside = tmp_path / "outside"
    _project(project)
    outside.mkdir()
    manifest, _, lock = _pack(source)
    installed_root = project / ".sdai" / "installed-packs"
    installed_root.parent.mkdir(parents=True, exist_ok=True)
    try:
        installed_root.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is not available")

    with pytest.raises(PackLifecycleError, match="symlink component"):
        install_from_local(project, source, lock, manifest.coordinate)
