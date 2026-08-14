from __future__ import annotations

from hashlib import sha256

from sdai.pack_lock import PackCandidate, PackLockEntry, PackLockError, resolve_pack_lock
from sdai.pack_manifest import PACK_MANIFEST_API_VERSION, PackManifest, SemVer


def _digest(seed: str) -> str:
    return "sha256:" + sha256(seed.encode("utf-8")).hexdigest()


def _candidate(
    pack_id: str,
    version: str,
    *,
    dependencies: list[tuple[str, str]] | None = None,
) -> PackCandidate:
    manifest = PackManifest.from_dict(
        {
            "apiVersion": PACK_MANIFEST_API_VERSION,
            "id": pack_id,
            "publisher": "acme",
            "version": version,
            "description": f"{pack_id} {version}",
            "capabilities": ["skills"],
            "contentRoots": ["skills"],
            "dependencies": [
                {"publisher": "acme", "id": dep_id, "version": constraint}
                for dep_id, constraint in (dependencies or [])
            ],
            "compatibility": {"framework": ">=0.5.4,<1.0.0", "apis": []},
        }
    )
    return PackCandidate(
        manifest=manifest,
        source=f"catalog://corp/acme/{pack_id}/{version}",
        content_sha256=_digest(manifest.identity),
    )


def test_resolver_backtracks_when_highest_candidate_creates_cycle() -> None:
    root = _candidate("root", "1.0.0", dependencies=[("helper", ">=1.0.0,<3.0.0")])
    helper_high = _candidate("helper", "2.0.0", dependencies=[("bridge", "=1.0.0")])
    bridge = _candidate("bridge", "1.0.0", dependencies=[("helper", ">=2.0.0,<3.0.0")])
    helper_low = _candidate("helper", "1.5.0")

    lock = resolve_pack_lock([root], [helper_high, bridge, helper_low])

    selected = {entry.coordinate: str(entry.version) for entry in lock.packages}
    assert selected == {"acme/helper": "1.5.0", "acme/root": "1.0.0"}


def test_direct_lock_entry_constructor_rejects_invalid_contract_values() -> None:
    try:
        PackLockEntry(
            publisher="ACME",
            id="root",
            version=SemVer.parse("1.0.0"),
            source="catalog://corp/root",
            manifest_sha256=_digest("manifest"),
            content_sha256=_digest("content"),
            dependencies=(),
        )
    except PackLockError as exc:
        assert "portable lowercase identifier" in str(exc)
    else:
        raise AssertionError("invalid direct PackLockEntry construction must fail closed")
