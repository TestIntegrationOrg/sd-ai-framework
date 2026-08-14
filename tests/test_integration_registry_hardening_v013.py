from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from sdai.extensions.registry import RegistryLayer
from sdai.integration_manifest import INTEGRATION_MANIFEST_API_VERSION, IntegrationManifest
from sdai.integration_registry import (
    IntegrationRegistry,
    IntegrationRegistryError,
    IntegrationSource,
    build_integration_registry,
    register_integration_source,
)


def _manifest(integration_id: str, *, description: str = "stable", version: str = "1.0.0") -> IntegrationManifest:
    return IntegrationManifest.from_dict(
        {
            "apiVersion": INTEGRATION_MANIFEST_API_VERSION,
            "id": integration_id,
            "version": version,
            "displayName": integration_id,
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


def _write(path: Path, manifest: IntegrationManifest) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(manifest.as_dict(), sort_keys=False, allow_unicode=True),
        encoding="utf-8",
        newline="\n",
    )


def test_source_registration_is_atomic_when_a_later_manifest_conflicts(tmp_path: Path) -> None:
    registry = IntegrationRegistry()
    original = _manifest("alpha-agent")
    registry.register(
        original,
        layer=RegistryLayer.BUILTIN,
        source="framework",
        path="alpha.integration.yaml",
    )
    before = registry.to_json()

    root = tmp_path / "repo"
    _write(root / "a-beta.integration.yaml", _manifest("beta-agent"))
    _write(
        root / "z-alpha.integration.yaml",
        _manifest("alpha-agent", description="conflicting repo content"),
    )

    with pytest.raises(IntegrationRegistryError, match="SDAI-INTEGRATION-REG-003"):
        register_integration_source(
            registry,
            IntegrationSource(root, RegistryLayer.REPO, "repository"),
        )

    assert registry.to_json() == before
    assert registry.resolve("alpha-agent") is not None
    assert registry.resolve("beta-agent") is None


def test_provenance_paths_are_portable_not_merely_relative() -> None:
    registry = IntegrationRegistry()
    manifest = _manifest("alpha-agent")

    for path in (
        "folder/name?.integration.yaml",
        "folder/CON.integration.yaml",
        "folder/trailing /.integration.yaml",
        "folder/name. /manifest.integration.yaml",
    ):
        with pytest.raises(IntegrationRegistryError, match="SDAI-INTEGRATION-REG-001"):
            registry.register(
                manifest,
                layer=RegistryLayer.REPO,
                source="repository",
                path=path,
            )


def test_integration_source_coerces_pathlike_root_and_rejects_non_source_builder_values(tmp_path: Path) -> None:
    source = IntegrationSource(str(tmp_path), RegistryLayer.REPO, "repository")  # type: ignore[arg-type]
    assert source.root == tmp_path

    with pytest.raises(IntegrationRegistryError, match="all registry sources"):
        build_integration_registry([object()])  # type: ignore[list-item]
