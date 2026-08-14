from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from sdai.extensions.registry import RegistryLayer
from sdai.integration_manifest import INTEGRATION_MANIFEST_API_VERSION, IntegrationManifest
from sdai.integration_registry import (
    INTEGRATION_REGISTRY_API_VERSION,
    INTEGRATION_RESOLUTION_API_VERSION,
    IntegrationRegistry,
    IntegrationRegistryError,
    IntegrationSource,
    build_integration_registry,
    discover_integration_manifests,
)


def _manifest(
    integration_id: str = "acme-agent",
    version: str = "1.0.0",
    *,
    description: str = "Acme café Δ integration",
) -> IntegrationManifest:
    return IntegrationManifest.from_dict(
        {
            "apiVersion": INTEGRATION_MANIFEST_API_VERSION,
            "id": integration_id,
            "version": version,
            "displayName": f"{integration_id} café Δ",
            "description": description,
            "capabilities": ["skills"],
            "projections": [
                {
                    "kind": "skill",
                    "source": ".agents/skills",
                    "target": f".{integration_id}/skills",
                }
            ],
            "execution": None,
            "security": {
                "requiresNetwork": False,
                "requiresWorkspaceWrite": False,
                "environment": [],
            },
        }
    )


def _register(
    registry: IntegrationRegistry,
    manifest: IntegrationManifest,
    layer: RegistryLayer,
    *,
    source: str | None = None,
    path: str | None = None,
    locked: bool = False,
) -> None:
    registry.register(
        manifest,
        layer=layer,
        source=source or f"{layer.value}-catalog",
        path=path or f"{manifest.id}/{manifest.version}.integration.yaml",
        locked=locked,
    )


def _write_manifest(path: Path, manifest: IntegrationManifest, *, json_format: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if json_format:
        path.write_text(manifest.to_json() + "\n", encoding="utf-8", newline="\n")
    else:
        path.write_text(
            yaml.safe_dump(manifest.as_dict(), sort_keys=False, allow_unicode=True),
            encoding="utf-8",
            newline="\n",
        )


def test_exact_identity_can_repeat_across_layers_only_with_identical_canonical_content() -> None:
    registry = IntegrationRegistry()
    manifest = _manifest()
    _register(registry, manifest, RegistryLayer.BUILTIN, source="framework", path="acme.integration.yaml")
    _register(registry, manifest, RegistryLayer.REPO, source="repository", path="nested/acme.integration.yaml")

    resolved = registry.resolve("acme-agent", "1.0.0")

    assert resolved is not None
    assert resolved.identity == "acme-agent@1.0.0"
    assert resolved.selected_provenance.layer == RegistryLayer.REPO
    assert [item.layer for item in resolved.provenance] == [RegistryLayer.BUILTIN, RegistryLayer.REPO]
    assert all(item.manifest_sha256 == manifest.sha256 for item in resolved.provenance)
    assert resolved.as_dict()["apiVersion"] == INTEGRATION_RESOLUTION_API_VERSION

    conflicting = _manifest(description="Different canonical content")
    with pytest.raises(IntegrationRegistryError, match="SDAI-INTEGRATION-REG-003.*conflicting canonical content"):
        _register(registry, conflicting, RegistryLayer.USER)


def test_same_layer_duplicate_exact_identity_is_ambiguous_even_when_bytes_match() -> None:
    registry = IntegrationRegistry()
    manifest = _manifest()
    _register(registry, manifest, RegistryLayer.REPO, source="repo-one", path="one.integration.yaml")

    with pytest.raises(IntegrationRegistryError, match="SDAI-INTEGRATION-REG-002.*duplicate"):
        _register(registry, manifest, RegistryLayer.REPO, source="repo-two", path="two.integration.yaml")


def test_resolution_uses_semver_precedence_and_exact_version_lookup() -> None:
    registry = IntegrationRegistry()
    for version in ("1.0.0", "1.1.0-rc.1", "1.1.0", "1.0.9"):
        _register(registry, _manifest(version=version), RegistryLayer.BUILTIN, path=f"{version}.integration.yaml")

    latest = registry.resolve("acme-agent")
    prerelease = registry.resolve("acme-agent", "1.1.0-rc.1")
    versions = registry.list_versions("acme-agent")

    assert latest is not None and latest.identity == "acme-agent@1.1.0"
    assert prerelease is not None and prerelease.identity == "acme-agent@1.1.0-rc.1"
    assert [item.identity for item in versions] == [
        "acme-agent@1.1.0",
        "acme-agent@1.1.0-rc.1",
        "acme-agent@1.0.9",
        "acme-agent@1.0.0",
    ]
    assert registry.resolve("acme-agent", "9.9.9") is None


def test_latest_fails_closed_when_build_variants_have_equal_semver_precedence() -> None:
    registry = IntegrationRegistry()
    _register(registry, _manifest(version="2.0.0+a"), RegistryLayer.BUILTIN, path="a.integration.yaml")
    _register(registry, _manifest(version="2.0.0+b"), RegistryLayer.BUILTIN, path="b.integration.yaml")

    with pytest.raises(IntegrationRegistryError, match="SDAI-INTEGRATION-REG-004.*request an exact version"):
        registry.resolve("acme-agent")

    assert registry.resolve("acme-agent", "2.0.0+a") is not None
    assert registry.resolve("acme-agent", "2.0.0+b") is not None


def test_authoritative_lock_blocks_every_higher_layer_version_and_is_order_independent() -> None:
    organization = _manifest(version="1.0.0")
    user = _manifest(version="2.0.0")

    first = IntegrationRegistry()
    _register(first, organization, RegistryLayer.ORG, locked=True, source="enterprise")
    with pytest.raises(IntegrationRegistryError, match="SDAI-INTEGRATION-REG-005.*locked"):
        _register(first, user, RegistryLayer.USER, source="developer")

    reverse = IntegrationRegistry()
    _register(reverse, user, RegistryLayer.USER, source="developer")
    with pytest.raises(IntegrationRegistryError, match="SDAI-INTEGRATION-REG-005.*locked"):
        _register(reverse, organization, RegistryLayer.ORG, locked=True, source="enterprise")


def test_lock_is_allowed_only_for_builtin_and_organization_layers() -> None:
    registry = IntegrationRegistry()
    with pytest.raises(IntegrationRegistryError, match="SDAI-INTEGRATION-REG-005.*authoritative"):
        _register(registry, _manifest(), RegistryLayer.REPO, locked=True)

    with pytest.raises(IntegrationRegistryError, match="SDAI-INTEGRATION-REG-005.*authoritative"):
        IntegrationSource(Path("unused"), RegistryLayer.USER, "developer", locked=True)


def test_lower_layers_may_coexist_beneath_an_organization_lock() -> None:
    registry = IntegrationRegistry()
    _register(registry, _manifest(version="0.9.0"), RegistryLayer.BUILTIN, source="framework")
    _register(registry, _manifest(version="0.9.5"), RegistryLayer.PACK, source="signed-pack")
    _register(registry, _manifest(version="1.0.0"), RegistryLayer.ORG, source="enterprise", locked=True)

    resolved = registry.resolve("acme-agent")

    assert resolved is not None and resolved.identity == "acme-agent@1.0.0"


def test_search_info_and_snapshot_are_stable_and_machine_readable() -> None:
    registry = IntegrationRegistry()
    _register(registry, _manifest("alpha-agent", "1.0.0", description="Security reviewer"), RegistryLayer.BUILTIN)
    _register(registry, _manifest("beta-agent", "1.0.0", description="Database helper"), RegistryLayer.BUILTIN)

    assert [item.id for item in registry.search()] == ["alpha-agent", "beta-agent"]
    assert [item.id for item in registry.search("SECURITY")] == ["alpha-agent"]
    assert registry.info("beta-agent") is not None
    assert registry.info("missing-agent") is None
    payload = registry.as_dict()
    assert payload["apiVersion"] == INTEGRATION_REGISTRY_API_VERSION
    assert registry.to_json() == registry.to_json()
    assert registry.sha256.startswith("sha256:") and len(registry.sha256) == 71


def test_build_registry_is_independent_of_source_and_filesystem_order_and_omits_absolute_roots(tmp_path: Path) -> None:
    framework = tmp_path / "framework-équipe"
    repository = tmp_path / "répository"
    common = _manifest("acme-agent", "1.0.0")
    newer = _manifest("beta-agent", "2.0.0")
    _write_manifest(framework / "z" / "acme.integration.yaml", common)
    _write_manifest(framework / "a" / "beta.integration.json", newer, json_format=True)
    _write_manifest(repository / "nested" / "acme.integration.yml", common)
    (framework / "ignore.txt").write_text("not a manifest\n", encoding="utf-8")

    built_in = IntegrationSource(framework, RegistryLayer.BUILTIN, "framework-catalog")
    repo = IntegrationSource(repository, RegistryLayer.REPO, "repository-catalog")
    first = build_integration_registry([repo, built_in])
    second = build_integration_registry([built_in, repo])

    assert first.to_json() == second.to_json()
    assert first.sha256 == second.sha256
    assert [item.id for item in first.list_resolved()] == ["acme-agent", "beta-agent"]
    acme = first.resolve("acme-agent")
    assert acme is not None
    assert acme.selected_provenance.layer == RegistryLayer.REPO
    assert acme.selected_provenance.path == "nested/acme.integration.yml"
    assert str(tmp_path) not in first.to_json()
    assert "framework-équipe" not in first.to_json()
    assert "repository-catalog" in first.to_json()


def test_discovery_is_recursive_sorted_and_does_not_follow_symlink_directories(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    _write_manifest(root / "z" / "z.integration.yaml", _manifest("z-agent"))
    _write_manifest(root / "a" / "a.integration.yaml", _manifest("a-agent"))
    outside = tmp_path / "outside"
    _write_manifest(outside / "escape.integration.yaml", _manifest("escape-agent"))
    linked = root / "linked"
    try:
        linked.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable on this platform: {exc}")

    discovered = discover_integration_manifests(
        IntegrationSource(root, RegistryLayer.REPO, "repo")
    )

    assert [relative for _, relative in discovered] == [
        "a/a.integration.yaml",
        "z/z.integration.yaml",
    ]


def test_symlink_source_root_and_symlink_manifest_fail_closed(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    manifest_path = real / "agent.integration.yaml"
    _write_manifest(manifest_path, _manifest())
    root_link = tmp_path / "root-link"
    file_link = real / "linked.integration.yaml"
    try:
        root_link.symlink_to(real, target_is_directory=True)
        file_link.symlink_to(manifest_path.name)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable on this platform: {exc}")

    with pytest.raises(IntegrationRegistryError, match="SDAI-INTEGRATION-REG-006.*symlink"):
        discover_integration_manifests(
            IntegrationSource(root_link, RegistryLayer.REPO, "repo")
        )

    with pytest.raises(Exception, match="symlink"):
        build_integration_registry(
            [IntegrationSource(real, RegistryLayer.REPO, "repo")]
        )


def test_conflicting_exact_identity_across_discovery_roots_fails_regardless_of_input_order(tmp_path: Path) -> None:
    framework = tmp_path / "framework"
    repository = tmp_path / "repository"
    _write_manifest(framework / "agent.integration.yaml", _manifest(description="framework"))
    _write_manifest(repository / "agent.integration.yaml", _manifest(description="repo mutation"))
    sources = (
        IntegrationSource(framework, RegistryLayer.BUILTIN, "framework"),
        IntegrationSource(repository, RegistryLayer.REPO, "repository"),
    )

    for ordering in (sources, tuple(reversed(sources))):
        with pytest.raises(IntegrationRegistryError, match="SDAI-INTEGRATION-REG-003"):
            build_integration_registry(ordering)


def test_same_layer_duplicate_from_multiple_roots_fails_deterministically(tmp_path: Path) -> None:
    left = tmp_path / "left"
    right = tmp_path / "right"
    manifest = _manifest()
    _write_manifest(left / "agent.integration.yaml", manifest)
    _write_manifest(right / "agent.integration.yaml", manifest)

    with pytest.raises(IntegrationRegistryError, match="SDAI-INTEGRATION-REG-002"):
        build_integration_registry(
            [
                IntegrationSource(right, RegistryLayer.REPO, "right"),
                IntegrationSource(left, RegistryLayer.REPO, "left"),
            ]
        )
