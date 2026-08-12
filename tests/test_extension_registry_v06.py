from __future__ import annotations

from pathlib import Path

import pytest

from sdai.extensions import (
    ExtensionKind,
    ExtensionRegistry,
    ExtensionRegistryError,
    RegistryLayer,
    parse_extension_manifest,
)


def _manifest(
    extension_id: str,
    *,
    kind: ExtensionKind = ExtensionKind.SKILL,
    version: str = "1.0.0",
    source: str | None = None,
):
    return parse_extension_manifest(
        {
            "apiVersion": "sdai/v1",
            "kind": kind.value,
            "metadata": {
                "id": extension_id,
                "version": version,
                "description": f"{extension_id} {version}",
            },
            "spec": {"marker": version},
        },
        source=source or f"{extension_id}-{version}.yaml",
    )


def test_normal_precedence_is_deterministic_and_user_wins() -> None:
    registry = ExtensionRegistry()
    for layer, version in [
        (RegistryLayer.USER, "5.0.0"),
        (RegistryLayer.BUILTIN, "1.0.0"),
        (RegistryLayer.REPO, "4.0.0"),
        (RegistryLayer.PACK, "2.0.0"),
        (RegistryLayer.ORG, "3.0.0"),
    ]:
        registry.register(_manifest("example", version=version), layer=layer)

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


def test_duplicate_definition_in_same_layer_fails_without_replacing_original() -> None:
    registry = ExtensionRegistry()
    original = registry.register(
        _manifest("example", version="1.0.0", source="first.yaml"),
        layer=RegistryLayer.REPO,
    )

    with pytest.raises(ExtensionRegistryError, match="SDAI-REG-001"):
        registry.register(
            _manifest("example", version="2.0.0", source="second.yaml"),
            layer=RegistryLayer.REPO,
        )

    assert registry.resolve(ExtensionKind.SKILL, "example") == original


@pytest.mark.parametrize(
    "layer",
    [RegistryLayer.PACK, RegistryLayer.REPO, RegistryLayer.USER],
)
def test_only_authoritative_layers_may_lock_definitions(layer: RegistryLayer) -> None:
    registry = ExtensionRegistry()

    with pytest.raises(ExtensionRegistryError, match="SDAI-REG-002"):
        registry.register(_manifest("example"), layer=layer, locked=True)

    assert len(registry) == 0


def test_invalid_registry_layer_is_rejected_with_actionable_error() -> None:
    registry = ExtensionRegistry()

    with pytest.raises(ExtensionRegistryError, match="SDAI-REG-004"):
        registry.register(_manifest("example"), layer="remote" )  # type: ignore[arg-type]

    assert len(registry) == 0


def test_locked_org_definition_blocks_repo_and_user_overrides() -> None:
    registry = ExtensionRegistry()
    registry.register(_manifest("example", version="1.0.0"), layer=RegistryLayer.BUILTIN)
    locked = registry.register(
        _manifest("example", version="2.0.0", source="org-policy"),
        layer=RegistryLayer.ORG,
        locked=True,
    )

    for layer in (RegistryLayer.REPO, RegistryLayer.USER):
        with pytest.raises(ExtensionRegistryError, match="SDAI-REG-003"):
            registry.register(_manifest("example", version="3.0.0"), layer=layer)

    assert registry.resolve(ExtensionKind.SKILL, "example") == locked
    assert [entry.layer for entry in registry.history(ExtensionKind.SKILL, "example")] == [
        RegistryLayer.BUILTIN,
        RegistryLayer.ORG,
    ]


def test_locked_builtin_definition_blocks_all_later_layers() -> None:
    registry = ExtensionRegistry()
    locked = registry.register(
        _manifest("example", source="core"),
        layer=RegistryLayer.BUILTIN,
        locked=True,
    )

    with pytest.raises(ExtensionRegistryError, match="SDAI-REG-003"):
        registry.register(_manifest("example", version="2.0.0"), layer=RegistryLayer.PACK)

    assert registry.resolve(ExtensionKind.SKILL, "example") == locked


def test_late_authoritative_lock_fails_without_mutating_existing_registry() -> None:
    registry = ExtensionRegistry()
    user = registry.register(
        _manifest("example", version="3.0.0"),
        layer=RegistryLayer.USER,
    )

    with pytest.raises(ExtensionRegistryError, match="SDAI-REG-003"):
        registry.register(
            _manifest("example", version="2.0.0", source="org-policy"),
            layer=RegistryLayer.ORG,
            locked=True,
        )

    assert registry.resolve(ExtensionKind.SKILL, "example") == user
    assert [entry.layer for entry in registry.history(ExtensionKind.SKILL, "example")] == [
        RegistryLayer.USER
    ]


def test_same_id_for_different_extension_kinds_does_not_conflict() -> None:
    registry = ExtensionRegistry()
    skill = registry.register(
        _manifest("quality", kind=ExtensionKind.SKILL),
        layer=RegistryLayer.REPO,
    )
    workflow = registry.register(
        _manifest("quality", kind=ExtensionKind.WORKFLOW),
        layer=RegistryLayer.REPO,
    )

    assert registry.resolve(ExtensionKind.SKILL, "quality") == skill
    assert registry.resolve(ExtensionKind.WORKFLOW, "quality") == workflow
    assert len(registry) == 2


def test_registry_preserves_provenance() -> None:
    registry = ExtensionRegistry()
    path = Path("/policy/extensions/secure-coding.yaml")
    entry = registry.register(
        _manifest("secure-coding", source="manifest-source"),
        layer=RegistryLayer.ORG,
        source="organization-policy",
        path=path,
        locked=True,
    )

    assert entry.layer is RegistryLayer.ORG
    assert entry.source == "organization-policy"
    assert entry.path == path
    assert entry.locked is True
    assert entry.manifest.source == "manifest-source"


def test_blank_explicit_source_falls_back_to_manifest_source() -> None:
    registry = ExtensionRegistry()
    entry = registry.register(
        _manifest("example", source="manifest.yaml"),
        layer=RegistryLayer.REPO,
        source="   ",
    )

    assert entry.source == "manifest.yaml"


def test_list_resolved_has_stable_kind_and_id_order_and_kind_filter() -> None:
    registry = ExtensionRegistry()
    registry.register(_manifest("zeta"), layer=RegistryLayer.REPO)
    registry.register(
        _manifest("beta", kind=ExtensionKind.WORKFLOW),
        layer=RegistryLayer.REPO,
    )
    registry.register(_manifest("alpha"), layer=RegistryLayer.REPO)
    registry.register(
        _manifest("alpha", kind=ExtensionKind.AGENT),
        layer=RegistryLayer.REPO,
    )

    all_keys = [(entry.key.kind.value, entry.key.id) for entry in registry.list_resolved()]
    skill_ids = [entry.key.id for entry in registry.list_resolved(ExtensionKind.SKILL)]

    assert all_keys == sorted(all_keys)
    assert skill_ids == ["alpha", "zeta"]


def test_unknown_extension_resolves_to_none_and_empty_history() -> None:
    registry = ExtensionRegistry()

    assert registry.resolve(ExtensionKind.SKILL, "missing") is None
    assert registry.history(ExtensionKind.SKILL, "missing") == ()
