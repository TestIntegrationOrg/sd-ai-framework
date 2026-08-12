from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
import re
from typing import Any, Mapping

import yaml

from sdai.path_safety import ensure_within_project
from sdai.text import read_utf8_text


API_VERSION = "sdai/v1"


class ExtensionManifestError(RuntimeError):
    """Raised when an SDAI extension manifest is malformed or unsupported."""


class ExtensionKind(StrEnum):
    SKILL = "Skill"
    AGENT = "Agent"
    WORKFLOW = "Workflow"
    WORKFLOW_COMPONENT = "WorkflowComponent"
    ARTIFACT_SCHEMA = "ArtifactSchema"
    VALIDATOR = "Validator"
    QUALITY_GATE = "QualityGate"
    INTEGRATION = "Integration"
    PACK = "Pack"


@dataclass(frozen=True)
class ExtensionMetadata:
    id: str
    version: str
    description: str = ""


@dataclass(frozen=True)
class ExtensionManifest:
    api_version: str
    kind: ExtensionKind
    metadata: ExtensionMetadata
    spec: dict[str, Any]
    source: str = "<memory>"


_TOP_LEVEL_KEYS = frozenset({"apiVersion", "kind", "metadata", "spec"})
_METADATA_KEYS = frozenset({"id", "version", "description"})
_EXTENSION_ID = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?$")
_SEMVER = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)


def _error(code: str, message: str) -> ExtensionManifestError:
    return ExtensionManifestError(f"{code}: {message}")


def _require_mapping(value: object, *, code: str, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _error(code, f"{label} must be a mapping")
    return value


def _unknown_keys(value: Mapping[str, Any], allowed: frozenset[str]) -> list[str]:
    return sorted(str(key) for key in value.keys() if key not in allowed)


def _valid_extension_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(_EXTENSION_ID.fullmatch(value))
        and ".." not in value
    )


def parse_extension_manifest(
    data: Mapping[str, Any],
    *,
    source: str = "<memory>",
) -> ExtensionManifest:
    """Validate and normalize an ``sdai/v1`` extension manifest.

    The envelope is deliberately strict so future extension loaders can rely on a
    stable contract. Kind-specific ``spec`` validation is owned by the extension
    type and is intentionally not performed here.
    """

    root = _require_mapping(data, code="SDAI-EXT-001", label="extension manifest")
    unknown = _unknown_keys(root, _TOP_LEVEL_KEYS)
    if unknown:
        raise _error(
            "SDAI-EXT-002",
            f"extension manifest contains unknown top-level field(s): {', '.join(unknown)}",
        )

    api_version = root.get("apiVersion")
    if api_version != API_VERSION:
        raise _error(
            "SDAI-EXT-003",
            f"unsupported apiVersion {api_version!r}; expected {API_VERSION!r}",
        )

    raw_kind = root.get("kind")
    try:
        kind = ExtensionKind(raw_kind)
    except (TypeError, ValueError) as exc:
        supported = ", ".join(item.value for item in ExtensionKind)
        raise _error(
            "SDAI-EXT-004",
            f"unsupported extension kind {raw_kind!r}; supported kinds: {supported}",
        ) from exc

    metadata = _require_mapping(
        root.get("metadata"), code="SDAI-EXT-005", label="extension metadata"
    )
    unknown_metadata = _unknown_keys(metadata, _METADATA_KEYS)
    if unknown_metadata:
        raise _error(
            "SDAI-EXT-006",
            "extension metadata contains unknown field(s): "
            + ", ".join(unknown_metadata),
        )

    extension_id = metadata.get("id")
    if not _valid_extension_id(extension_id):
        raise _error(
            "SDAI-EXT-007",
            "metadata.id must be a portable lowercase extension id using letters, "
            "numbers, dots, underscores, or hyphens",
        )

    version = metadata.get("version")
    if not isinstance(version, str) or not _SEMVER.fullmatch(version):
        raise _error(
            "SDAI-EXT-008",
            "metadata.version must be semantic version text such as '1.2.3'",
        )

    description = metadata.get("description", "")
    if not isinstance(description, str):
        raise _error("SDAI-EXT-009", "metadata.description must be a string")

    spec = _require_mapping(root.get("spec", {}), code="SDAI-EXT-010", label="spec")
    return ExtensionManifest(
        api_version=API_VERSION,
        kind=kind,
        metadata=ExtensionMetadata(
            id=extension_id,
            version=version,
            description=description.strip(),
        ),
        spec=dict(spec),
        source=source,
    )


def load_extension_manifest(project_root: Path, path: Path) -> ExtensionManifest:
    root = project_root.resolve()
    supplied = path if path.is_absolute() else root / path
    safe_path = ensure_within_project(root, supplied, label="extension manifest path")
    try:
        text = read_utf8_text(safe_path)
    except OSError as exc:
        raise _error(
            "SDAI-EXT-011",
            f"unable to read extension manifest {safe_path}: {exc}",
        ) from exc
    try:
        raw = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise _error(
            "SDAI-EXT-012",
            f"invalid YAML in extension manifest {safe_path}: {exc}",
        ) from exc
    if not isinstance(raw, Mapping):
        raise _error("SDAI-EXT-001", "extension manifest must be a mapping")
    source = safe_path.relative_to(root).as_posix()
    return parse_extension_manifest(raw, source=source)
