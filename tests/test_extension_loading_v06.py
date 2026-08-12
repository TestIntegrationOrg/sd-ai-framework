from __future__ import annotations

from pathlib import Path

import pytest

from sdai.extensions import (
    ExtensionKind,
    ExtensionRegistry,
    ExtensionRegistryError,
    ExtensionSource,
    RegistryLayer,
    build_extension_registry,
    register_extension_source,
)
from sdai.path_safety import PathSafetyError


def _write_manifest(
    root: Path,
    relative_path: str,
    *,
    extension_id: str,
    version: str,
    kind: str = "Skill",
) -> Path:
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"""apiVersion: sdai/v1
kind: {kind}
metadata:
  id: {extension_id}
  version: {version}
spec:
  marker: {version}
""",
        encoding="utf-8",
    )
    return path


def test_builder_applies_unlocked_precedence_in_deterministic_layer_order(
    tmp_path: Path,
) -> None:
    roots: dict[RegistryLayer, Path] = {}
    sources: list[ExtensionSource] = []
    for layer, version in [
        (RegistryLayer.USER, "5.0.0"),
        (RegistryLayer.BUILTIN, "1.0.0"),
        (RegistryLayer.REPO, "4.0.0"),
        (RegistryLayer.PACK, "2.0.0"),
        (RegistryLayer.ORG, "3.0.0"),
    ]:
        root = tmp_path / layer.value
        roots[layer] = root
        _write_manifest(root, "example.yaml", extension_id="example", version=version)
        sources.append(
            ExtensionSource(root=root, path=Path("example.yaml"), layer=layer)
        )

    registry = build_extension_registry(sources)
    resolved = registry.resolve(ExtensionKind.SKILL, "example")

    assert resolved is not None
    assert resolved.layer is RegistryLayer.USER
    assert resolved.manifest.metadata.version == "5.0.0"
    assert [entry.layer for entry in registry.history(ExtensionKind.SKILL, "example")] == [
        RegistryLayer.BUILTIN,
        RegistryLayer.PACK,
        RegistryLayer.ORG,
        RegistryLayer.REPO,
        RegistryLayer.USER,
    ]


def test_builder_loads_org_lock_before_repo_even_when_input_is_reversed(
    tmp_path: Path,
) -> None:
    org_root = tmp_path / "org"
    repo_root = tmp_path / "repo"
    _write_manifest(org_root, "policy.yaml", extension_id="secure-coding", version="1.0.0")
    _write_manifest(repo_root, "override.yaml", extension_id="secure-coding", version="2.0.0")

    sources = [
        ExtensionSource(
            root=repo_root,
            path=Path("override.yaml"),
            layer=RegistryLayer.REPO,
        ),
        ExtensionSource(
            root=org_root,
            path=Path("policy.yaml"),
            layer=RegistryLayer.ORG,
            locked=True,
            label="organization-security-policy",
        ),
    ]

    with pytest.raises(ExtensionRegistryError, match="SDAI-REG-003"):
        build_extension_registry(sources)


def test_register_extension_source_preserves_safe_absolute_file_provenance(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    manifest_path = _write_manifest(
        root,
        ".sdai/extensions/api-review.yaml",
        extension_id="api-review",
        version="1.0.0",
    )
    registry = ExtensionRegistry()

    entry = register_extension_source(
        registry,
        ExtensionSource(
            root=root,
            path=Path(".sdai/extensions/api-review.yaml"),
            layer=RegistryLayer.REPO,
        ),
    )

    assert entry.path == manifest_path.resolve()
    assert entry.source == str(manifest_path.resolve())
    assert entry.manifest.source == str(manifest_path.resolve())


def test_register_extension_source_can_use_human_readable_source_label(
    tmp_path: Path,
) -> None:
    root = tmp_path / "org"
    manifest_path = _write_manifest(
        root,
        "extensions/secure.yaml",
        extension_id="secure-coding",
        version="1.0.0",
    )
    registry = ExtensionRegistry()

    entry = register_extension_source(
        registry,
        ExtensionSource(
            root=root,
            path=Path("extensions/secure.yaml"),
            layer=RegistryLayer.ORG,
            label="company-engineering-policy",
        ),
    )

    assert entry.source == "company-engineering-policy"
    assert entry.path == manifest_path.resolve()
    assert entry.manifest.source == str(manifest_path.resolve())


def test_each_source_uses_its_own_containment_root(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    org_root = tmp_path / "organization-policy-root"
    _write_manifest(repo_root, "repo.yaml", extension_id="repo-skill", version="1.0.0")
    _write_manifest(org_root, "org.yaml", extension_id="org-skill", version="1.0.0")

    registry = build_extension_registry(
        [
            ExtensionSource(repo_root, Path("repo.yaml"), RegistryLayer.REPO),
            ExtensionSource(org_root, Path("org.yaml"), RegistryLayer.ORG),
        ]
    )

    assert registry.resolve(ExtensionKind.SKILL, "repo-skill") is not None
    assert registry.resolve(ExtensionKind.SKILL, "org-skill") is not None


def test_source_path_escape_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "outside.yaml"
    _write_manifest(tmp_path, "outside.yaml", extension_id="escape", version="1.0.0")

    with pytest.raises(PathSafetyError, match="must stay inside"):
        build_extension_registry(
            [
                ExtensionSource(
                    root=root,
                    path=outside,
                    layer=RegistryLayer.REPO,
                )
            ]
        )


def test_builder_returns_empty_registry_for_empty_source_set() -> None:
    registry = build_extension_registry([])

    assert isinstance(registry, ExtensionRegistry)
    assert len(registry) == 0


def test_builder_rejects_unknown_source_layer_before_loading(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _write_manifest(root, "example.yaml", extension_id="example", version="1.0.0")

    with pytest.raises(ExtensionRegistryError, match="SDAI-REG-004"):
        build_extension_registry(
            [
                ExtensionSource(
                    root=root,
                    path=Path("example.yaml"),
                    layer="remote",  # type: ignore[arg-type]
                )
            ]
        )
