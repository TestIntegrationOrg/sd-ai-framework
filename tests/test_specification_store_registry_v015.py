from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from sdai.pack_manifest import SemVer
from sdai.specification_stores import (
    SPECIFICATION_STORE_MANIFEST_API_VERSION,
    SPECIFICATION_STORE_REGISTRY_API_VERSION,
    SPECIFICATION_STORE_RESOLUTION_API_VERSION,
    SpecificationStoreError,
    SpecificationStoreLayer,
    SpecificationStoreManifest,
    SpecificationStoreSource,
    SpecificationRoot,
    build_specification_store_registry,
    load_specification_store_manifest,
)


def _store(
    root: Path,
    store_id: str = "platform-specs",
    *,
    version: str = "1.0.0",
    description: str = "Platform specifications café Δ",
    capabilities: tuple[str, ...] = ("changes", "current-specifications"),
    roots: dict[str, object] | None = None,
    metadata: dict[str, object] | None = None,
) -> Path:
    selected_roots = roots or {
        "changes": "specs/changes",
        "current": "specs/current",
    }
    for relative in selected_roots.values():
        if isinstance(relative, str) and ".." not in relative and not Path(relative).is_absolute():
            (root / relative).mkdir(parents=True, exist_ok=True)
    path = root / ".sdai-store" / "store.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(
            {
                "apiVersion": SPECIFICATION_STORE_MANIFEST_API_VERSION,
                "kind": "SpecificationStore",
                "metadata": {
                    "id": store_id,
                    "version": version,
                    "description": description,
                },
                "spec": {
                    "specificationRoots": selected_roots,
                    "capabilities": list(capabilities),
                    "metadata": metadata or {},
                },
            },
            sort_keys=False,
            allow_unicode=True,
        ),
        encoding="utf-8",
        newline="\n",
    )
    return path


def _source(
    root: Path,
    layer: SpecificationStoreLayer,
    source: str,
    *,
    locked: bool = False,
) -> SpecificationStoreSource:
    return SpecificationStoreSource(root, layer, source, locked)


def test_manifest_is_strict_canonical_utf8_and_layout_bound(tmp_path: Path) -> None:
    _store(
        tmp_path,
        metadata={"owner": "équipe Δ", "retention": {"years": 7}},
    )
    manifest = load_specification_store_manifest(tmp_path)

    assert manifest.identity == "platform-specs@1.0.0"
    assert [item.id for item in manifest.specification_roots] == ["changes", "current"]
    assert manifest.capabilities == ("changes", "current-specifications")
    assert manifest.as_dict()["spec"]["metadata"]["owner"] == "équipe Δ"  # type: ignore[index]
    assert manifest.sha256.startswith("sha256:")
    assert manifest.to_json().encode("utf-8").decode("utf-8") == manifest.to_json()


def test_manifest_rejects_unknown_duplicate_and_invalid_semver(tmp_path: Path) -> None:
    path = _store(tmp_path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw["unsafe"] = True
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(SpecificationStoreError, match="unsupported field"):
        load_specification_store_manifest(tmp_path)

    path.write_text(
        "apiVersion: sdai.specification-store/v1\n"
        "kind: SpecificationStore\n"
        "kind: Other\n",
        encoding="utf-8",
    )
    with pytest.raises(SpecificationStoreError, match="YAML is malformed"):
        load_specification_store_manifest(tmp_path)

    _store(tmp_path, version="01.0.0")
    with pytest.raises(SpecificationStoreError, match="invalid SpecificationStore version"):
        load_specification_store_manifest(tmp_path)


def test_optional_metadata_rejects_unicode_normalization_key_collision(
    tmp_path: Path,
) -> None:
    _store(tmp_path, metadata={"é": 1, "e\u0301": 2})
    with pytest.raises(SpecificationStoreError, match="normalization-colliding"):
        load_specification_store_manifest(tmp_path)


def test_optional_metadata_string_values_are_nfc_normalized(tmp_path: Path) -> None:
    _store(tmp_path, metadata={"owner": "e\u0301quipe"})
    manifest = load_specification_store_manifest(tmp_path)
    assert manifest.as_dict()["spec"]["metadata"]["owner"] == "équipe"  # type: ignore[index]


def test_recursive_yaml_metadata_is_reported_as_store_domain_error(tmp_path: Path) -> None:
    path = _store(tmp_path)
    manifest = path.read_text(encoding="utf-8")
    recursive = manifest.replace(
        "  metadata: {}\n",
        "  metadata:\n    cycle: &cycle\n      - *cycle\n",
    )
    assert recursive != manifest
    path.write_text(recursive, encoding="utf-8")
    with pytest.raises(SpecificationStoreError, match="contains a recursive value"):
        load_specification_store_manifest(tmp_path)


def test_deeply_nested_metadata_is_bounded(tmp_path: Path) -> None:
    metadata: object = "leaf"
    for _ in range(66):
        metadata = [metadata]
    _store(tmp_path, metadata={"nested": metadata})  # type: ignore[arg-type]
    with pytest.raises(SpecificationStoreError, match="maximum nesting depth"):
        load_specification_store_manifest(tmp_path)


@pytest.mark.parametrize(
    "roots, message",
    [
        ({"current": "../escape"}, "portable relative path"),
        ({"current": "/absolute"}, "portable relative path"),
        ({"current": "specs\\current"}, "portable relative path"),
        ({"current": ".sdai-store/private"}, "cannot contain store metadata"),
        ({"current": ".SDAI-STORE/private"}, "cannot contain store metadata"),
        ({"one": "specs", "two": "specs/current"}, "must not overlap"),
        ({"one": "Specs", "two": "specs/current"}, "must not overlap"),
        ({"one": "Specs", "two": "specs"}, "path collision"),
        ({"current": "COM¹/private"}, "reserved Windows segment"),
        ({"current": "lpt³/private"}, "reserved Windows segment"),
    ],
)
def test_manifest_rejects_unsafe_colliding_or_overlapping_roots(
    tmp_path: Path,
    roots: dict[str, object],
    message: str,
) -> None:
    _store(tmp_path, roots=roots)
    with pytest.raises(SpecificationStoreError, match=message):
        load_specification_store_manifest(tmp_path)


def test_manifest_rejects_missing_root_and_symlink_redirection(tmp_path: Path) -> None:
    path = _store(tmp_path)
    (tmp_path / "specs" / "changes").rmdir()
    with pytest.raises(SpecificationStoreError, match="must be an existing directory"):
        load_specification_store_manifest(tmp_path)

    _store(tmp_path)
    current = tmp_path / "specs" / "current"
    current.rmdir()
    outside = tmp_path.parent / "outside-store-specs"
    outside.mkdir(exist_ok=True)
    try:
        current.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable on this platform")
    with pytest.raises(SpecificationStoreError, match="symlink component"):
        load_specification_store_manifest(tmp_path)
    assert path.is_file()


def test_invalid_utf8_is_reported_as_store_domain_error(tmp_path: Path) -> None:
    path = _store(tmp_path)
    path.write_bytes(b"apiVersion: \xff")
    with pytest.raises(SpecificationStoreError, match="manifest YAML is malformed"):
        load_specification_store_manifest(tmp_path)


def test_programmatic_semver_instances_are_revalidated(tmp_path: Path) -> None:
    roots = (SpecificationRoot("current", "specs/current"),)
    with pytest.raises(SpecificationStoreError, match="invalid SpecificationStore version"):
        SpecificationStoreManifest(
            "platform-specs",
            SemVer(-1, 0, 0),
            "Invalid programmatic version",
            roots,
            ("current-specifications",),
        )


def test_malformed_programmatic_semver_is_reported_as_store_domain_error() -> None:
    roots = (SpecificationRoot("current", "specs/current"),)
    with pytest.raises(SpecificationStoreError, match="invalid SpecificationStore version"):
        SpecificationStoreManifest(
            "platform-specs",
            SemVer(1, 0, 0, (1,), ()),  # type: ignore[arg-type]
            "Malformed programmatic version",
            roots,
            ("current-specifications",),
        )


def test_oversized_semver_is_reported_as_store_domain_error(tmp_path: Path) -> None:
    _store(tmp_path, version=f"{'1' * 5000}.0.0")
    with pytest.raises(SpecificationStoreError, match="version exceeds 256 characters"):
        load_specification_store_manifest(tmp_path)


@pytest.mark.parametrize("capabilities", ["changes", None])
def test_programmatic_capabilities_require_a_sequence(
    capabilities: object,
) -> None:
    roots = (SpecificationRoot("current", "specs/current"),)
    with pytest.raises(SpecificationStoreError, match="capabilities must be"):
        SpecificationStoreManifest(
            "platform-specs",
            SemVer(1, 0, 0),
            "Invalid programmatic capabilities",
            roots,
            capabilities,  # type: ignore[arg-type]
        )


def test_latest_semver_and_registry_json_are_source_order_independent(tmp_path: Path) -> None:
    core = tmp_path / "core"
    repo = tmp_path / "repo"
    user = tmp_path / "user"
    _store(core, version="1.0.0", description="Core")
    _store(repo, version="2.0.0", description="Repository")
    _store(user, version="1.5.0", description="User")
    sources = [
        _source(user, SpecificationStoreLayer.USER, "user"),
        _source(core, SpecificationStoreLayer.CORE, "framework"),
        _source(repo, SpecificationStoreLayer.REPO, "repository"),
    ]

    forward = build_specification_store_registry(sources)
    reverse = build_specification_store_registry(reversed(sources))

    assert forward.resolve("platform-specs").identity == "platform-specs@2.0.0"  # type: ignore[union-attr]
    assert forward.resolve("platform-specs", "1.5.0").selected_provenance.layer == SpecificationStoreLayer.USER  # type: ignore[union-attr]
    assert forward.to_json() == reverse.to_json()
    assert forward.as_dict()["apiVersion"] == SPECIFICATION_STORE_REGISTRY_API_VERSION
    assert forward.sha256.startswith("sha256:")


def test_identical_exact_manifest_preserves_provenance_and_selects_higher_layer(
    tmp_path: Path,
) -> None:
    core = tmp_path / "core"
    repo = tmp_path / "repo"
    _store(core)
    _store(repo)
    resolved = build_specification_store_registry(
        [
            _source(repo, SpecificationStoreLayer.REPO, "repository"),
            _source(core, SpecificationStoreLayer.CORE, "framework"),
        ]
    ).resolve("platform-specs", "1.0.0")

    assert resolved is not None
    assert resolved.selected_provenance.layer == SpecificationStoreLayer.REPO
    assert [item.layer for item in resolved.provenance] == [
        SpecificationStoreLayer.CORE,
        SpecificationStoreLayer.REPO,
    ]
    assert resolved.as_dict()["apiVersion"] == SPECIFICATION_STORE_RESOLUTION_API_VERSION
    assert str(tmp_path) not in resolved.to_json()


def test_conflicting_exact_identity_and_same_layer_duplicate_fail_closed(
    tmp_path: Path,
) -> None:
    core = tmp_path / "core"
    repo = tmp_path / "repo"
    duplicate = tmp_path / "duplicate"
    _store(core, description="Core")
    _store(repo, description="Changed")
    _store(duplicate, description="Duplicate")

    with pytest.raises(SpecificationStoreError, match="conflicting canonical content"):
        build_specification_store_registry(
            [
                _source(core, SpecificationStoreLayer.CORE, "framework"),
                _source(repo, SpecificationStoreLayer.REPO, "repository"),
            ]
        )

    with pytest.raises(SpecificationStoreError, match="duplicate SpecificationStore"):
        build_specification_store_registry(
            [
                _source(repo, SpecificationStoreLayer.REPO, "one"),
                _source(duplicate, SpecificationStoreLayer.REPO, "two"),
            ]
        )


def test_authoritative_lock_blocks_all_higher_layer_versions(tmp_path: Path) -> None:
    org = tmp_path / "org"
    repo = tmp_path / "repo"
    _store(org, version="1.0.0")
    _store(repo, version="2.0.0")

    with pytest.raises(SpecificationStoreError, match="locked by org"):
        build_specification_store_registry(
            [
                _source(repo, SpecificationStoreLayer.REPO, "repository"),
                _source(org, SpecificationStoreLayer.ORG, "company", locked=True),
            ]
        )
    with pytest.raises(SpecificationStoreError, match="only core/org"):
        _source(repo, SpecificationStoreLayer.REPO, "repository", locked=True)


def test_latest_build_variants_are_ambiguous_but_exact_versions_resolve(
    tmp_path: Path,
) -> None:
    core = tmp_path / "core"
    repo = tmp_path / "repo"
    _store(core, version="1.0.0+one", description="One")
    _store(repo, version="1.0.0+two", description="Two")
    registry = build_specification_store_registry(
        [
            _source(core, SpecificationStoreLayer.CORE, "framework"),
            _source(repo, SpecificationStoreLayer.REPO, "repository"),
        ]
    )

    with pytest.raises(SpecificationStoreError, match="ambiguous.*build variants"):
        registry.resolve("platform-specs")
    assert registry.resolve("platform-specs", "1.0.0+one").identity == "platform-specs@1.0.0+one"  # type: ignore[union-attr]


def test_list_search_unicode_provenance_and_atomic_source_loading(tmp_path: Path) -> None:
    alpha = tmp_path / "alpha"
    zeta = tmp_path / "zeta"
    invalid = tmp_path / "invalid"
    _store(alpha, "alpha", description="Architecture specifications")
    _store(zeta, "zeta", description="Sécurité Δ")
    _store(invalid, "invalid", roots={"current": "missing"})
    (invalid / "missing").rmdir()
    registry = build_specification_store_registry(
        [
            _source(zeta, SpecificationStoreLayer.REPO, "équipe Δ"),
            _source(alpha, SpecificationStoreLayer.CORE, "framework"),
        ]
    )

    assert [item.id for item in registry.list_resolved()] == ["alpha", "zeta"]
    assert [item.id for item in registry.search("sécurité")] == ["zeta"]
    assert registry.resolve("zeta").selected_provenance.source == "équipe Δ"  # type: ignore[union-attr]
    assert str(zeta) not in registry.to_json()
    with pytest.raises(SpecificationStoreError, match="must be an existing directory"):
        build_specification_store_registry(
            [
                _source(alpha, SpecificationStoreLayer.CORE, "framework"),
                _source(invalid, SpecificationStoreLayer.REPO, "invalid"),
            ]
        )
