from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
from pathlib import Path

import pytest

from sdai.pack_lock import (
    PackCandidate,
    PackLock,
    PackLockError,
    pack_lock_file_sha256,
    resolve_pack_lock,
    write_pack_lock,
)
from sdai.pack_manifest import PACK_MANIFEST_API_VERSION, PackManifest


def _digest(seed: str) -> str:
    return "sha256:" + sha256(seed.encode("utf-8")).hexdigest()


def _candidate(
    publisher: str,
    pack_id: str,
    version: str,
    *,
    dependencies: list[tuple[str, str, str]] | None = None,
) -> PackCandidate:
    manifest = PackManifest.from_dict(
        {
            "apiVersion": PACK_MANIFEST_API_VERSION,
            "id": pack_id,
            "publisher": publisher,
            "version": version,
            "description": f"{publisher}/{pack_id} {version}",
            "capabilities": ["skills"],
            "contentRoots": ["skills"],
            "dependencies": [
                {"publisher": dep_publisher, "id": dep_id, "version": constraint}
                for dep_publisher, dep_id, constraint in (dependencies or [])
            ],
            "compatibility": {"framework": ">=0.5.4,<1.0.0", "apis": []},
        }
    )
    return PackCandidate(
        manifest=manifest,
        source=f"catalog://corp/{publisher}/{pack_id}/{version}",
        content_sha256=_digest(manifest.identity),
    )


def test_identical_root_can_also_be_present_in_available_universe() -> None:
    root = _candidate(
        "acme",
        "root",
        "1.0.0",
        dependencies=[("acme", "library", "=1.0.0")],
    )
    library = _candidate("acme", "library", "1.0.0")

    first = resolve_pack_lock([root], [root, library])
    second = resolve_pack_lock([root], [library])

    assert first.to_text() == second.to_text()


def test_same_identity_with_different_source_remains_ambiguous() -> None:
    root = _candidate("acme", "root", "1.0.0")
    conflicting = PackCandidate(
        manifest=root.manifest,
        source="catalog://different/root/1.0.0",
        content_sha256=root.content_sha256,
    )

    with pytest.raises(PackLockError, match="SDAI-PACK-LOCK-002.*ambiguous"):
        resolve_pack_lock([root], [conflicting])


def test_loaded_lock_rejects_unportable_package_and_dependency_identifiers() -> None:
    root = _candidate("acme", "root", "1.0.0")
    lock = resolve_pack_lock([root], [])
    raw = deepcopy(lock.as_dict())
    raw["packages"][0]["publisher"] = "ACME"  # type: ignore[index]

    with pytest.raises(PackLockError, match="portable lowercase identifier"):
        PackLock.from_dict(raw)

    raw = deepcopy(lock.as_dict())
    raw["roots"][0] = "ACME/root@1.0.0"  # type: ignore[index]
    with pytest.raises(PackLockError, match="portable lowercase identifier"):
        PackLock.from_dict(raw)


def test_loaded_lock_rejects_dependency_cycle_even_if_all_exact_entries_exist() -> None:
    root = _candidate(
        "acme",
        "a",
        "1.0.0",
        dependencies=[("acme", "b", "=1.0.0")],
    )
    b = _candidate(
        "acme",
        "b",
        "1.0.0",
        dependencies=[("acme", "a", "=1.0.0")],
    )
    # The resolver itself rejects this graph, so construct a syntactically complete
    # lock payload to prove corruption is also rejected at load time.
    raw = {
        "apiVersion": "sdai.pack-lock/v1",
        "roots": ["acme/a@1.0.0"],
        "packages": [
            {
                "publisher": "acme",
                "id": "a",
                "version": "1.0.0",
                "source": root.source,
                "manifestSha256": root.manifest.sha256,
                "contentSha256": root.content_sha256,
                "dependencies": ["acme/b@1.0.0"],
            },
            {
                "publisher": "acme",
                "id": "b",
                "version": "1.0.0",
                "source": b.source,
                "manifestSha256": b.manifest.sha256,
                "contentSha256": b.content_sha256,
                "dependencies": ["acme/a@1.0.0"],
            },
        ],
    }

    with pytest.raises(PackLockError, match="SDAI-PACK-LOCK-005.*acme/a.*acme/b.*acme/a"):
        PackLock.from_dict(raw)


def test_file_sha_is_exact_on_disk_digest_and_drives_guarded_replacement(tmp_path: Path) -> None:
    path = tmp_path / "packs.lock.json"
    first = resolve_pack_lock([_candidate("acme", "root", "1.0.0")], [])
    second = resolve_pack_lock([_candidate("acme", "root", "1.0.1")], [])

    write_pack_lock(path, first)
    expected = pack_lock_file_sha256(path)

    assert expected == "sha256:" + sha256(path.read_bytes()).hexdigest()
    assert expected != first.sha256

    write_pack_lock(path, second, expected_current_sha256=expected)
    assert path.read_text(encoding="utf-8") == second.to_text()


def test_lock_file_helpers_reject_directories_and_symlinks(tmp_path: Path) -> None:
    directory = tmp_path / "packs.lock.json"
    directory.mkdir()
    lock = resolve_pack_lock([_candidate("acme", "root", "1.0.0")], [])

    with pytest.raises(PackLockError, match="not a file"):
        write_pack_lock(directory, lock)
    with pytest.raises(PackLockError, match="existing file"):
        pack_lock_file_sha256(directory)

    target = tmp_path / "real.lock.json"
    write_pack_lock(target, lock)
    link = tmp_path / "linked.lock.json"
    try:
        link.symlink_to(target.name)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable on this platform: {exc}")
    with pytest.raises(PackLockError, match="must not be a symlink"):
        pack_lock_file_sha256(link)
