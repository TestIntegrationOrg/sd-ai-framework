from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from pathlib import Path
import threading

from sdai.pack_lock import (
    PackCandidate,
    PackLock,
    PackLockError,
    load_pack_lock,
    pack_lock_file_sha256,
    resolve_pack_lock,
    write_pack_lock,
)
from sdai.pack_manifest import PACK_MANIFEST_API_VERSION, PackManifest


def _candidate(version: str) -> PackCandidate:
    manifest = PackManifest.from_dict(
        {
            "apiVersion": PACK_MANIFEST_API_VERSION,
            "id": "root",
            "publisher": "acme",
            "version": version,
            "description": f"root {version}",
            "capabilities": ["skills"],
            "contentRoots": ["skills"],
            "dependencies": [],
            "compatibility": {"framework": ">=0.5.4,<1.0.0", "apis": []},
        }
    )
    return PackCandidate(
        manifest=manifest,
        source=f"catalog://corp/acme/root/{version}",
        content_sha256="sha256:" + sha256(manifest.identity.encode("utf-8")).hexdigest(),
    )


def _lock(version: str) -> PackLock:
    return resolve_pack_lock([_candidate(version)], [])


def test_two_writers_from_same_expected_hash_cannot_lose_update(tmp_path: Path) -> None:
    path = tmp_path / "packs.lock.json"
    original = _lock("1.0.0")
    first_update = _lock("1.0.1")
    second_update = _lock("1.0.2")
    write_pack_lock(path, original)
    expected = pack_lock_file_sha256(path)
    start = threading.Barrier(2)

    def attempt(candidate: PackLock) -> tuple[str, str]:
        start.wait()
        try:
            write_pack_lock(path, candidate, expected_current_sha256=expected)
            return ("success", candidate.sha256)
        except PackLockError as exc:
            return ("error", str(exc))

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(attempt, (first_update, second_update)))

    assert [status for status, _ in results].count("success") == 1
    assert [status for status, _ in results].count("error") == 1
    error = next(value for status, value in results if status == "error")
    assert "changed concurrently" in error
    final = load_pack_lock(path)
    assert final.sha256 in {first_update.sha256, second_update.sha256}
    assert (tmp_path / ".packs.lock.json.write-lock").is_file()
