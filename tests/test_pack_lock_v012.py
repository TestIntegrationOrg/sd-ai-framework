from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path

import pytest

from sdai.pack_lock import (
    PACK_LOCK_API_VERSION,
    PackCandidate,
    PackLock,
    PackLockError,
    compare_pack_lock,
    load_pack_lock,
    resolve_pack_lock,
    write_pack_lock,
)
from sdai.pack_manifest import PACK_MANIFEST_API_VERSION, PackManifest


def _digest(seed: str) -> str:
    return "sha256:" + sha256(seed.encode("utf-8")).hexdigest()


def _manifest(
    publisher: str,
    pack_id: str,
    version: str,
    *,
    dependencies: list[tuple[str, str, str]] | None = None,
) -> PackManifest:
    return PackManifest.from_dict(
        {
            "apiVersion": PACK_MANIFEST_API_VERSION,
            "id": pack_id,
            "publisher": publisher,
            "version": version,
            "description": f"{publisher}/{pack_id} {version} café Δ",
            "capabilities": ["skills"],
            "contentRoots": ["skills"],
            "dependencies": [
                {"publisher": dep_publisher, "id": dep_id, "version": constraint}
                for dep_publisher, dep_id, constraint in (dependencies or [])
            ],
            "compatibility": {"framework": ">=0.5.4,<1.0.0", "apis": []},
        }
    )


def _candidate(
    publisher: str,
    pack_id: str,
    version: str,
    *,
    dependencies: list[tuple[str, str, str]] | None = None,
    source: str | None = None,
) -> PackCandidate:
    manifest = _manifest(publisher, pack_id, version, dependencies=dependencies)
    return PackCandidate(
        manifest=manifest,
        source=source or f"catalog://corp/{publisher}/{pack_id}/{version}",
        content_sha256=_digest(manifest.identity),
    )


def test_resolver_backtracks_from_higher_transitive_conflict_and_is_input_order_independent() -> None:
    root = _candidate(
        "acme",
        "root",
        "1.0.0",
        dependencies=[
            ("acme", "library", ">=1.0.0,<3.0.0"),
            ("acme", "runtime", "=1.0.0"),
        ],
        source="catalog://équipe/root",
    )
    library_2 = _candidate(
        "acme",
        "library",
        "2.0.0",
        dependencies=[("acme", "runtime", "=2.0.0")],
    )
    library_1 = _candidate(
        "acme",
        "library",
        "1.5.0",
        dependencies=[("acme", "runtime", "=1.0.0")],
    )
    runtime_1 = _candidate("acme", "runtime", "1.0.0")
    runtime_2 = _candidate("acme", "runtime", "2.0.0")
    available = [library_2, runtime_2, runtime_1, library_1]

    first = resolve_pack_lock([root], available)
    second = resolve_pack_lock([root], reversed(available))

    assert first.to_text() == second.to_text()
    assert first.sha256 == second.sha256
    assert first.roots == ("acme/root@1.0.0",)
    by_coordinate = {entry.coordinate: entry for entry in first.packages}
    assert str(by_coordinate["acme/library"].version) == "1.5.0"
    assert str(by_coordinate["acme/runtime"].version) == "1.0.0"
    assert by_coordinate["acme/root"].source == "catalog://équipe/root"
    assert by_coordinate["acme/root"].manifest_sha256 == root.manifest.sha256
    assert by_coordinate["acme/root"].content_sha256 == root.content_sha256
    assert by_coordinate["acme/root"].dependencies == (
        "acme/library@1.5.0",
        "acme/runtime@1.0.0",
    )
    assert json.loads(first.to_json())["apiVersion"] == PACK_LOCK_API_VERSION


def test_resolver_prefers_highest_semver_precedence_with_stable_build_tie_break() -> None:
    root = _candidate(
        "acme",
        "root",
        "1.0.0",
        dependencies=[("acme", "library", ">=1.0.0,<2.0.0")],
    )
    stable = _candidate("acme", "library", "1.9.0")
    prerelease = _candidate("acme", "library", "1.9.0-rc.1")
    build_z = _candidate("acme", "library", "1.9.0+z")
    build_a = _candidate("acme", "library", "1.9.0+a")

    lock = resolve_pack_lock([root], [prerelease, stable, build_z, build_a])
    selected = next(entry for entry in lock.packages if entry.coordinate == "acme/library")

    assert str(selected.version) == "1.9.0"


def test_resolver_reports_missing_dependency() -> None:
    root = _candidate(
        "acme",
        "root",
        "1.0.0",
        dependencies=[("acme", "missing", ">=1.0.0,<2.0.0")],
    )

    with pytest.raises(PackLockError, match="SDAI-PACK-LOCK-003.*acme/missing"):
        resolve_pack_lock([root], [])


def test_resolver_reports_constraint_conflict_with_available_versions() -> None:
    root = _candidate(
        "acme",
        "root",
        "1.0.0",
        dependencies=[("acme", "library", ">=2.0.0,<3.0.0")],
    )
    only_old = _candidate("acme", "library", "1.9.9")

    with pytest.raises(PackLockError, match="SDAI-PACK-LOCK-004.*available.*1.9.9"):
        resolve_pack_lock([root], [only_old])


def test_resolver_rejects_dependency_cycle() -> None:
    root = _candidate(
        "acme",
        "root",
        "1.0.0",
        dependencies=[("acme", "a", "=1.0.0")],
    )
    a = _candidate(
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

    with pytest.raises(PackLockError, match="SDAI-PACK-LOCK-005.*acme/a.*acme/b.*acme/a"):
        resolve_pack_lock([root], [a, b])


def test_resolver_rejects_ambiguous_exact_candidate_identity() -> None:
    root = _candidate(
        "acme",
        "root",
        "1.0.0",
        dependencies=[("acme", "library", "=1.0.0")],
    )
    first = _candidate("acme", "library", "1.0.0", source="catalog://one/library")
    second = _candidate("acme", "library", "1.0.0", source="catalog://two/library")

    with pytest.raises(PackLockError, match="SDAI-PACK-LOCK-002.*ambiguous"):
        resolve_pack_lock([root], [first, second])


def test_lock_round_trip_rejects_corruption_duplicate_keys_and_noncanonical_order() -> None:
    root = _candidate("acme", "root", "1.0.0")
    lock = resolve_pack_lock([root], [])

    assert PackLock.from_json(lock.to_json()).to_json() == lock.to_json()

    duplicate = lock.to_json().replace(
        '"apiVersion":"sdai.pack-lock/v1"',
        '"apiVersion":"sdai.pack-lock/v1","apiVersion":"sdai.pack-lock/v1"',
    )
    with pytest.raises(PackLockError, match="duplicate key 'apiVersion'"):
        PackLock.from_json(duplicate)

    corrupt = deepcopy(lock.as_dict())
    corrupt["packages"][0]["contentSha256"] = "sha256:BAD"  # type: ignore[index]
    with pytest.raises(PackLockError, match="lowercase SHA-256"):
        PackLock.from_dict(corrupt)


def test_compare_lock_reports_changed_missing_extra_and_roots() -> None:
    root = _candidate(
        "acme",
        "root",
        "1.0.0",
        dependencies=[("acme", "library", ">=1.0.0,<2.0.0")],
    )
    first = resolve_pack_lock([root], [_candidate("acme", "library", "1.0.0")])
    second = resolve_pack_lock([root], [_candidate("acme", "library", "1.1.0")])

    status = compare_pack_lock(first, second)

    assert status.outdated is True
    assert status.current_sha256 == first.sha256
    assert status.expected_sha256 == second.sha256
    assert status.differences == ("changed:acme/library",)


def test_atomic_lock_write_is_idempotent_and_requires_explicit_stale_replacement(tmp_path: Path) -> None:
    path = tmp_path / "packs.lock.json"
    root = _candidate(
        "acme",
        "root",
        "1.0.0",
        dependencies=[("acme", "library", ">=1.0.0,<2.0.0")],
    )
    first = resolve_pack_lock([root], [_candidate("acme", "library", "1.0.0")])
    second = resolve_pack_lock([root], [_candidate("acme", "library", "1.1.0")])

    write_pack_lock(path, first)
    first_bytes = path.read_bytes()
    write_pack_lock(path, first)
    assert path.read_bytes() == first_bytes == first.to_text().encode("utf-8")
    assert load_pack_lock(path).sha256 == first.sha256

    with pytest.raises(PackLockError, match="explicit expected_current_sha256"):
        write_pack_lock(path, second)

    file_hash = "sha256:" + sha256(path.read_bytes()).hexdigest()
    write_pack_lock(path, second, expected_current_sha256=file_hash)
    assert load_pack_lock(path).sha256 == second.sha256
    assert not list(tmp_path.glob(".packs.lock.json.*.tmp"))


def test_atomic_lock_write_rejects_concurrent_change_and_symlink_target(tmp_path: Path) -> None:
    path = tmp_path / "packs.lock.json"
    root = _candidate("acme", "root", "1.0.0")
    first = resolve_pack_lock([root], [])
    write_pack_lock(path, first)

    with pytest.raises(PackLockError, match="changed concurrently"):
        write_pack_lock(path, first, expected_current_sha256=_digest("not-current"))

    target = tmp_path / "real-lock.json"
    target.write_text(first.to_text(), encoding="utf-8", newline="\n")
    link = tmp_path / "linked-lock.json"
    try:
        link.symlink_to(target.name)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable on this platform: {exc}")
    with pytest.raises(PackLockError, match="must not be a symlink"):
        load_pack_lock(link)
