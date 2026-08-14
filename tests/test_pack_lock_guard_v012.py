from __future__ import annotations

from hashlib import sha256
from pathlib import Path

import pytest

from sdai.pack_lock import PackCandidate, PackLockError, resolve_pack_lock, write_pack_lock
from sdai.pack_manifest import PACK_MANIFEST_API_VERSION, PackManifest


def _lock(version: str = "1.0.0"):
    manifest = PackManifest.from_dict(
        {
            "apiVersion": PACK_MANIFEST_API_VERSION,
            "id": "root",
            "publisher": "acme",
            "version": version,
            "description": "write guard regression",
            "capabilities": ["skills"],
            "contentRoots": ["skills"],
            "dependencies": [],
            "compatibility": {"framework": ">=0.5.4,<1.0.0", "apis": []},
        }
    )
    candidate = PackCandidate(
        manifest=manifest,
        source=f"catalog://corp/acme/root/{version}",
        content_sha256="sha256:" + sha256(manifest.identity.encode("utf-8")).hexdigest(),
    )
    return resolve_pack_lock([candidate], [])


def test_write_guard_is_stable_sibling_file(tmp_path: Path) -> None:
    path = tmp_path / "packs.lock.json"

    write_pack_lock(path, _lock())
    guard = tmp_path / ".packs.lock.json.write-lock"

    assert guard.is_file()
    assert not guard.is_symlink()
    first_guard_bytes = guard.read_bytes()
    write_pack_lock(path, _lock())
    assert guard.read_bytes() == first_guard_bytes


def test_symlinked_write_guard_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "packs.lock.json"
    target = tmp_path / "attacker-controlled-guard"
    target.write_bytes(b"x")
    guard = tmp_path / ".packs.lock.json.write-lock"
    try:
        guard.symlink_to(target.name)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable on this platform: {exc}")

    with pytest.raises(PackLockError, match="write guard.*must not be a symlink"):
        write_pack_lock(path, _lock())
