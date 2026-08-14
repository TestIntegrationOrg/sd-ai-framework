from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
from pathlib import Path

import pytest

from sdai.pack_catalog import (
    CatalogScope,
    PackCatalog,
    PackCatalogEntry,
    PackCatalogError,
    canonical_catalog_source,
    load_pack_catalog,
    resolve_pack_catalogs,
)
from sdai.pack_manifest import PACK_MANIFEST_API_VERSION, PackManifest


def _digest(seed: str) -> str:
    return "sha256:" + sha256(seed.encode("utf-8")).hexdigest()


def _manifest(
    publisher: str,
    pack_id: str,
    version: str,
    *,
    description: str | None = None,
) -> PackManifest:
    return PackManifest.from_dict(
        {
            "apiVersion": PACK_MANIFEST_API_VERSION,
            "id": pack_id,
            "publisher": publisher,
            "version": version,
            "description": description or f"{publisher}/{pack_id} {version} café Δ",
            "capabilities": ["skills"],
            "contentRoots": ["skills"],
            "dependencies": [],
            "compatibility": {"framework": ">=0.5.4,<1.0.0", "apis": []},
        }
    )


def _entry(
    publisher: str,
    pack_id: str,
    version: str,
    *,
    package_source: str | None = None,
    description: str | None = None,
) -> PackCatalogEntry:
    manifest = _manifest(publisher, pack_id, version, description=description)
    return PackCatalogEntry(
        manifest=manifest,
        source=package_source or f"https://packages.example/{publisher}/{pack_id}/{version}",
        content_sha256=_digest(manifest.identity),
    )


def _catalog(
    *,
    catalog_id: str = "corp",
    source: str = "https://catalog.example/corp/index.json",
    entries: tuple[PackCatalogEntry, ...] | None = None,
) -> PackCatalog:
    return PackCatalog.create(
        id=catalog_id,
        source=source,
        entries=entries
        or (
            _entry("acme", "secure-coding", "2.0.0", description="Secure coding rules"),
            _entry("acme", "secure-coding", "1.5.0", description="Secure coding legacy"),
            _entry("sdai", "quality", "1.0.0", description="Quality workflow"),
        ),
    )


def test_catalog_serialization_query_order_and_hash_are_deterministic() -> None:
    entries = (
        _entry("sdai", "quality", "1.0.0"),
        _entry("acme", "secure-coding", "1.5.0"),
        _entry("acme", "secure-coding", "2.0.0"),
    )
    first = _catalog(entries=entries)
    second = _catalog(entries=tuple(reversed(entries)))

    assert first.to_json() == second.to_json()
    assert first.sha256 == second.sha256
    assert [entry.identity for entry in first.entries] == [
        "acme/secure-coding@2.0.0",
        "acme/secure-coding@1.5.0",
        "sdai/quality@1.0.0",
    ]
    assert [entry.identity for entry in first.search("secure")] == [
        "acme/secure-coding@2.0.0",
        "acme/secure-coding@1.5.0",
    ]
    assert [entry.identity for entry in first.info("acme", "secure-coding")] == [
        "acme/secure-coding@2.0.0",
        "acme/secure-coding@1.5.0",
    ]
    assert [entry.identity for entry in first.info("acme", "secure-coding", version="1.5.0")] == [
        "acme/secure-coding@1.5.0"
    ]


def test_catalog_round_trip_is_strict_and_provider_neutral() -> None:
    catalog = _catalog()

    round_trip = PackCatalog.from_json(catalog.to_json())

    assert round_trip.to_json() == catalog.to_json()
    assert round_trip.sha256 == catalog.sha256
    assert all(candidate.identity.startswith(("acme/", "sdai/")) for candidate in resolve_pack_catalogs(organization=[catalog]).candidates())


def test_duplicate_and_conflicting_exact_entries_fail_closed() -> None:
    entry = _entry("acme", "secure-coding", "1.0.0")
    with pytest.raises(PackCatalogError, match="duplicate entry"):
        PackCatalog.create(id="corp", source="catalog://corp", entries=[entry, entry])

    conflicting = PackCatalogEntry(
        manifest=entry.manifest,
        source="https://other.example/acme/secure-coding/1.0.0",
        content_sha256=entry.content_sha256,
    )
    with pytest.raises(PackCatalogError, match="conflicting entry"):
        PackCatalog.create(id="corp", source="catalog://corp", entries=[entry, conflicting])


def test_catalog_scope_resolution_deduplicates_identical_catalog_with_explicit_provenance() -> None:
    catalog = _catalog()

    resolved = resolve_pack_catalogs(
        organization=[catalog],
        repository=[catalog],
        user=[catalog],
    )

    assert len(resolved.catalogs) == 1
    item = resolved.catalogs[0]
    assert item.provenance == (
        CatalogScope.ORGANIZATION,
        CatalogScope.REPOSITORY,
        CatalogScope.USER,
    )
    assert item.catalog.sha256 == catalog.sha256
    assert [candidate.identity for candidate in resolved.candidates()] == sorted(
        candidate.identity for candidate in resolved.candidates()
    )


def test_lower_scope_cannot_replace_catalog_id_or_source_with_different_truth() -> None:
    organization = _catalog()
    changed = _catalog(entries=(_entry("acme", "secure-coding", "3.0.0"),))

    with pytest.raises(PackCatalogError, match="conflicting identities or bytes"):
        resolve_pack_catalogs(organization=[organization], repository=[changed])

    spoofed_id = _catalog(catalog_id="corp", source="https://evil.example/index.json")
    with pytest.raises(PackCatalogError, match="catalog id 'corp'.*conflicting sources"):
        resolve_pack_catalogs(organization=[organization], user=[spoofed_id])


def test_resolved_catalogs_reject_cross_catalog_disagreement_on_exact_pack_identity() -> None:
    first_entry = _entry("acme", "secure-coding", "1.0.0")
    second_entry = PackCatalogEntry(
        manifest=first_entry.manifest,
        source="https://mirror.example/acme/secure-coding/1.0.0",
        content_sha256=first_entry.content_sha256,
    )
    first = _catalog(catalog_id="corp", source="catalog://corp", entries=(first_entry,))
    second = _catalog(catalog_id="partner", source="catalog://partner", entries=(second_entry,))
    resolved = resolve_pack_catalogs(organization=[first, second])

    with pytest.raises(PackCatalogError, match="disagree on exact Pack identity"):
        resolved.candidates()


def test_load_catalog_validates_expected_source_and_integrity_without_mutation(tmp_path: Path) -> None:
    catalog = _catalog()
    path = tmp_path / "catalog.json"
    path.write_text(catalog.to_json(), encoding="utf-8", newline="\n")
    before = path.read_bytes()

    loaded = load_pack_catalog(
        path,
        expected_source=catalog.source,
        expected_sha256=catalog.sha256,
    )

    assert loaded.sha256 == catalog.sha256
    assert path.read_bytes() == before
    with pytest.raises(PackCatalogError, match="catalog source mismatch"):
        load_pack_catalog(path, expected_source="catalog://other")
    with pytest.raises(PackCatalogError, match="catalog integrity mismatch"):
        load_pack_catalog(path, expected_sha256=_digest("wrong"))


def test_load_catalog_missing_and_symlink_sources_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(PackCatalogError, match="does not exist"):
        load_pack_catalog(tmp_path / "missing.json")

    catalog = _catalog()
    target = tmp_path / "catalog.json"
    target.write_text(catalog.to_json(), encoding="utf-8")
    link = tmp_path / "linked.json"
    try:
        link.symlink_to(target.name)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable on this platform: {exc}")
    with pytest.raises(PackCatalogError, match="must not be a symlink"):
        load_pack_catalog(link)


@pytest.mark.parametrize(
    "source",
    [
        "catalog-without-scheme",
        "https://user:password@example.com/catalog.json",
        "https://catalog.example/index.json#fragment",
        "https://catalog.example/has space/index.json",
        r"https://catalog.example\index.json",
        "https:///missing-authority",
    ],
)
def test_catalog_source_identity_rejects_ambiguous_or_secret_bearing_values(source: str) -> None:
    with pytest.raises(PackCatalogError, match="SDAI-PACK-CATALOG-001"):
        canonical_catalog_source(source)


def test_catalog_json_duplicate_keys_and_unknown_fields_fail_closed() -> None:
    catalog = _catalog()
    duplicate = catalog.to_json().replace(
        '"id":"corp"',
        '"id":"corp","id":"other"',
    )
    with pytest.raises(PackCatalogError, match="duplicate key 'id'"):
        PackCatalog.from_json(duplicate)

    raw = deepcopy(catalog.as_dict())
    raw["unexpected"] = True
    with pytest.raises(PackCatalogError, match="unsupported field"):
        PackCatalog.from_dict(raw)
