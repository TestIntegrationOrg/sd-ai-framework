from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json

from sdai.extensions.manifests import API_VERSION, ExtensionKind
from sdai.extensions.registry import RegistryLayer


EXTENSION_CONTRACT_API_VERSION = "sdai.extension-contract/v1"
EXTENSION_STABILITY = "stable-1.0"

# These codes have been part of the public extension authoring error model since
# the 0.6 foundation. Keep this list explicit: adding a new code is additive, but
# changing the meaning/removing one requires a future versioned compatibility plan.
MANIFEST_ERROR_CODES = tuple(f"SDAI-EXT-{index:03d}" for index in range(1, 12))
REGISTRY_ERROR_CODES = tuple(f"SDAI-REG-{index:03d}" for index in range(1, 5))

# Public imports that existed before the 1.0 stability declaration. They remain a
# compatibility floor even as new extension APIs are added in later releases.
LEGACY_PUBLIC_PYTHON_SYMBOLS = (
    "API_VERSION",
    "ExtensionKey",
    "ExtensionKind",
    "ExtensionManifest",
    "ExtensionManifestError",
    "ExtensionMetadata",
    "ExtensionRegistry",
    "ExtensionRegistryError",
    "ExtensionSource",
    "RegistryEntry",
    "RegistryLayer",
    "build_extension_registry",
    "load_extension_manifest",
    "parse_extension_manifest",
    "parse_extension_manifest_text",
    "register_extension_source",
)


@dataclass(frozen=True, slots=True)
class ExtensionLayerContract:
    name: str
    priority: int
    lockable: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "priority": self.priority,
            "lockable": self.lockable,
        }


@dataclass(frozen=True, slots=True)
class ExtensionContract:
    """Machine-inspectable 1.0 stability boundary for SDAI extensions."""

    manifest_api_versions: tuple[str, ...]
    extension_kinds: tuple[str, ...]
    registry_layers: tuple[ExtensionLayerContract, ...]
    stable_python_symbols: tuple[str, ...]
    manifest_error_codes: tuple[str, ...]
    registry_error_codes: tuple[str, ...]

    def _body(self) -> dict[str, object]:
        return {
            "apiVersion": EXTENSION_CONTRACT_API_VERSION,
            "stability": EXTENSION_STABILITY,
            "manifestApiVersions": list(self.manifest_api_versions),
            "extensionKinds": list(self.extension_kinds),
            "registryLayers": [item.as_dict() for item in self.registry_layers],
            "manifestEnvelope": {
                "requiredTopLevelFields": ["apiVersion", "kind", "metadata", "spec"],
                "metadataFields": ["id", "version", "description"],
                "specShape": "mapping",
                "unknownTopLevelFields": "reject",
                "unknownMetadataFields": "reject",
                "extensionIdGrammar": "portable-lowercase-v1",
                "versionGrammar": "semver",
            },
            "resolution": {
                "precedence": [item.name for item in self.registry_layers],
                "highestPriorityWinsWhenUnlocked": True,
                "duplicateSameLayer": "reject",
                "lockedOverride": "reject",
            },
            "errorCodes": {
                "manifest": list(self.manifest_error_codes),
                "registry": list(self.registry_error_codes),
            },
            "stablePythonSymbols": list(self.stable_python_symbols),
            "compatibility": {
                "sdaiV1Manifest": "supported-through-1.x",
                "existingPublicImports": "preserved-through-1.x",
                "breakingChangeRequiresNewContractVersion": True,
            },
        }

    @property
    def sha256(self) -> str:
        return "sha256:" + sha256(_canonical_bytes(self._body())).hexdigest()

    def as_dict(self) -> dict[str, object]:
        result = self._body()
        result["contractSha256"] = self.sha256
        return result

    def to_json(self) -> str:
        return _canonical_bytes(self.as_dict()).decode("utf-8") + "\n"


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def extension_contract() -> ExtensionContract:
    """Return the deterministic stable extension contract derived from runtime enums."""
    layers = tuple(
        ExtensionLayerContract(
            name=layer.value,
            priority=layer.priority,
            lockable=layer.lockable,
        )
        for layer in sorted(RegistryLayer, key=lambda item: item.priority)
    )
    return ExtensionContract(
        manifest_api_versions=(API_VERSION,),
        extension_kinds=tuple(item.value for item in ExtensionKind),
        registry_layers=layers,
        stable_python_symbols=LEGACY_PUBLIC_PYTHON_SYMBOLS,
        manifest_error_codes=MANIFEST_ERROR_CODES,
        registry_error_codes=REGISTRY_ERROR_CODES,
    )


def extension_contract_json() -> str:
    """Return canonical newline-terminated JSON for compatibility/tooling checks."""
    return extension_contract().to_json()


__all__ = [
    "EXTENSION_CONTRACT_API_VERSION",
    "EXTENSION_STABILITY",
    "ExtensionContract",
    "ExtensionLayerContract",
    "LEGACY_PUBLIC_PYTHON_SYMBOLS",
    "MANIFEST_ERROR_CODES",
    "REGISTRY_ERROR_CODES",
    "extension_contract",
    "extension_contract_json",
]
