from __future__ import annotations

from hashlib import sha256
import json

import pytest

import sdai.extensions as extensions
from sdai.extensions import (
    API_VERSION,
    EXTENSION_CONTRACT_API_VERSION,
    EXTENSION_STABILITY,
    ExtensionKind,
    ExtensionRegistry,
    ExtensionRegistryError,
    RegistryLayer,
    extension_contract,
    extension_contract_json,
    parse_extension_manifest,
)
from sdai.extensions.contract import LEGACY_PUBLIC_PYTHON_SYMBOLS


EXPECTED_KINDS = [
    "Skill",
    "Agent",
    "Workflow",
    "WorkflowComponent",
    "ArtifactSchema",
    "PluginStep",
    "Validator",
    "QualityGate",
    "Integration",
    "Pack",
]
EXPECTED_LAYERS = [
    {"name": "builtin", "priority": 0, "lockable": True},
    {"name": "pack", "priority": 10, "lockable": False},
    {"name": "org", "priority": 20, "lockable": True},
    {"name": "repo", "priority": 30, "lockable": False},
    {"name": "user", "priority": 40, "lockable": False},
]


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _manifest(*, kind: str = "Skill", extension_id: str = "stable-extension"):
    return parse_extension_manifest(
        {
            "apiVersion": "sdai/v1",
            "kind": kind,
            "metadata": {"id": extension_id, "version": "1.0.0"},
            # spec and metadata.description were optional in the 0.6 contract and
            # remain optional at the 1.0 compatibility boundary.
        },
        source="v100-compatibility-fixture",
    )


def test_stable_extension_contract_is_deterministic_versioned_and_self_hashed() -> None:
    first = extension_contract_json()
    second = extension_contract_json()
    assert first == second
    assert first.endswith("\n")

    payload = json.loads(first)
    assert payload["apiVersion"] == EXTENSION_CONTRACT_API_VERSION
    assert payload["apiVersion"] == "sdai.extension-contract/v1"
    assert payload["stability"] == EXTENSION_STABILITY == "stable-1.0"
    assert payload["manifestApiVersions"] == [API_VERSION] == ["sdai/v1"]
    assert payload["extensionKinds"] == EXPECTED_KINDS
    assert payload["extensionKinds"] == [item.value for item in ExtensionKind]
    assert payload["registryLayers"] == EXPECTED_LAYERS

    claimed = payload.pop("contractSha256")
    assert claimed == "sha256:" + sha256(_canonical(payload)).hexdigest()
    assert extension_contract().sha256 == claimed


def test_stable_manifest_schema_descriptor_matches_the_v06_parser_contract() -> None:
    descriptor = extension_contract().as_dict()["manifestEnvelope"]
    assert descriptor == {
        "allowedTopLevelFields": ["apiVersion", "kind", "metadata", "spec"],
        "requiredTopLevelFields": ["apiVersion", "kind", "metadata"],
        "optionalTopLevelFields": ["spec"],
        "metadataFields": ["id", "version", "description"],
        "requiredMetadataFields": ["id", "version"],
        "optionalMetadataFields": ["description"],
        "specShape": "mapping",
        "specDefault": {},
        "descriptionDefault": "",
        "unknownTopLevelFields": "reject",
        "unknownMetadataFields": "reject",
        "extensionIdGrammar": "portable-lowercase-v1",
        "versionGrammar": "semver",
    }

    manifest = _manifest()
    assert manifest.api_version == "sdai/v1"
    assert manifest.metadata.description == ""
    assert manifest.spec == {}


def test_all_current_extension_kinds_and_registry_authority_are_frozen_for_1x() -> None:
    contract = extension_contract()
    assert contract.extension_kinds == tuple(EXPECTED_KINDS)
    assert [item.as_dict() for item in contract.registry_layers] == EXPECTED_LAYERS
    assert [(item.value, item.priority, item.lockable) for item in RegistryLayer] == [
        ("builtin", 0, True),
        ("pack", 10, False),
        ("org", 20, True),
        ("repo", 30, False),
        ("user", 40, False),
    ]


def test_pre_1x_public_python_extension_imports_remain_available() -> None:
    contract = extension_contract()
    for symbol in LEGACY_PUBLIC_PYTHON_SYMBOLS:
        assert symbol in extensions.__all__
        assert hasattr(extensions, symbol)
    assert set(contract.stable_python_symbols) == set(extensions.__all__)


def test_existing_registry_precedence_duplicate_and_lock_fail_closed_semantics_remain() -> None:
    registry = ExtensionRegistry()
    manifest = _manifest()
    registry.register(manifest, layer=RegistryLayer.BUILTIN, source="builtin")
    registry.register(manifest, layer=RegistryLayer.PACK, source="pack")
    registry.register(manifest, layer=RegistryLayer.REPO, source="repo")
    registry.register(manifest, layer=RegistryLayer.USER, source="user")
    resolved = registry.resolve(ExtensionKind.SKILL, "stable-extension")
    assert resolved is not None
    assert resolved.layer is RegistryLayer.USER

    with pytest.raises(ExtensionRegistryError, match="SDAI-REG-001"):
        registry.register(manifest, layer=RegistryLayer.USER, source="user-duplicate")

    with pytest.raises(ExtensionRegistryError, match="SDAI-REG-002"):
        ExtensionRegistry().register(
            manifest,
            layer=RegistryLayer.REPO,
            source="repo-lock",
            locked=True,
        )

    authoritative = ExtensionRegistry()
    authoritative.register(
        manifest,
        layer=RegistryLayer.ORG,
        source="org-policy",
        locked=True,
    )
    with pytest.raises(ExtensionRegistryError, match="SDAI-REG-003"):
        authoritative.register(manifest, layer=RegistryLayer.USER, source="user-override")

    with pytest.raises(ExtensionRegistryError, match="SDAI-REG-004"):
        ExtensionRegistry().register(manifest, layer="future")  # type: ignore[arg-type]


def test_existing_manifest_errors_remain_stable_for_unsupported_api_and_kind() -> None:
    with pytest.raises(Exception, match="SDAI-EXT-003"):
        parse_extension_manifest(
            {
                "apiVersion": "sdai/v2",
                "kind": "Skill",
                "metadata": {"id": "stable-extension", "version": "1.0.0"},
            }
        )
    with pytest.raises(Exception, match="SDAI-EXT-004"):
        parse_extension_manifest(
            {
                "apiVersion": "sdai/v1",
                "kind": "FutureKind",
                "metadata": {"id": "stable-extension", "version": "1.0.0"},
            }
        )


def test_contract_lists_the_stable_existing_error_code_families() -> None:
    contract = extension_contract()
    assert contract.manifest_error_codes == tuple(
        f"SDAI-EXT-{index:03d}" for index in range(1, 12)
    )
    assert contract.registry_error_codes == tuple(
        f"SDAI-REG-{index:03d}" for index in range(1, 5)
    )
