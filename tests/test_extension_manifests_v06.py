from __future__ import annotations

from pathlib import Path

import pytest

from sdai.extensions import (
    API_VERSION,
    ExtensionKind,
    ExtensionManifestError,
    load_extension_manifest,
    parse_extension_manifest,
    parse_extension_manifest_text,
)
from sdai.path_safety import PathSafetyError


def _manifest(kind: str = "Skill") -> dict[str, object]:
    return {
        "apiVersion": API_VERSION,
        "kind": kind,
        "metadata": {
            "id": "secure-coding",
            "version": "1.2.0-beta.1",
            "description": "Secure coding guidance",
        },
        "spec": {"capabilities": ["coding", "security"]},
    }


@pytest.mark.parametrize("kind", [item.value for item in ExtensionKind])
def test_manifest_accepts_every_supported_extension_kind(kind: str) -> None:
    manifest = parse_extension_manifest(_manifest(kind), source="unit-test")

    assert manifest.api_version == API_VERSION
    assert manifest.kind.value == kind
    assert manifest.metadata.id == "secure-coding"
    assert manifest.metadata.version == "1.2.0-beta.1"
    assert manifest.metadata.description == "Secure coding guidance"
    assert manifest.spec == {"capabilities": ["coding", "security"]}
    assert manifest.source == "unit-test"


def test_manifest_rejects_unknown_top_level_fields() -> None:
    raw = _manifest()
    raw["permissions"] = {"shell": True}

    with pytest.raises(ExtensionManifestError, match="SDAI-EXT-002"):
        parse_extension_manifest(raw)


def test_manifest_rejects_unknown_metadata_fields() -> None:
    raw = _manifest()
    metadata = dict(raw["metadata"])  # type: ignore[arg-type]
    metadata["owner"] = "platform-team"
    raw["metadata"] = metadata

    with pytest.raises(ExtensionManifestError, match="SDAI-EXT-006"):
        parse_extension_manifest(raw)


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("apiVersion", "sdai/v2", "SDAI-EXT-003"),
        ("kind", "ExecutablePlugin", "SDAI-EXT-004"),
    ],
)
def test_manifest_rejects_unsupported_envelope_values(
    field: str, value: str, code: str
) -> None:
    raw = _manifest()
    raw[field] = value

    with pytest.raises(ExtensionManifestError, match=code):
        parse_extension_manifest(raw)


@pytest.mark.parametrize("extension_id", ["Java-Agent", "bad id", "../escape", ""])
def test_manifest_rejects_unsafe_extension_ids(extension_id: str) -> None:
    raw = _manifest()
    metadata = dict(raw["metadata"])  # type: ignore[arg-type]
    metadata["id"] = extension_id
    raw["metadata"] = metadata

    with pytest.raises(ExtensionManifestError, match="SDAI-EXT-007"):
        parse_extension_manifest(raw)


@pytest.mark.parametrize("version", ["1", "1.0", "v1.0.0", "1.0.x", "01.0.0"])
def test_manifest_requires_semantic_version(version: str) -> None:
    raw = _manifest()
    metadata = dict(raw["metadata"])  # type: ignore[arg-type]
    metadata["version"] = version
    raw["metadata"] = metadata

    with pytest.raises(ExtensionManifestError, match="SDAI-EXT-008"):
        parse_extension_manifest(raw)


def test_manifest_requires_mapping_spec() -> None:
    raw = _manifest()
    raw["spec"] = ["not", "a", "mapping"]

    with pytest.raises(ExtensionManifestError, match="SDAI-EXT-010"):
        parse_extension_manifest(raw)


def test_manifest_text_uses_safe_yaml_parser_and_requires_mapping() -> None:
    with pytest.raises(ExtensionManifestError, match="SDAI-EXT-001"):
        parse_extension_manifest_text("- one\n- two\n", source="list.yaml")

    with pytest.raises(ExtensionManifestError, match="SDAI-EXT-001"):
        parse_extension_manifest_text("metadata: [\n", source="broken.yaml")


def test_load_manifest_reads_utf8_inside_project(tmp_path: Path) -> None:
    manifest_path = tmp_path / ".sdai" / "extensions" / "example.yaml"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(
        """apiVersion: sdai/v1
kind: Skill
metadata:
  id: java-security
  version: 1.0.0
  description: Security guidance — 日本語
spec:
  capabilities:
    - security
""",
        encoding="utf-8",
    )

    manifest = load_extension_manifest(tmp_path, Path(".sdai/extensions/example.yaml"))

    assert manifest.metadata.description == "Security guidance — 日本語"
    assert manifest.source == str(manifest_path)


def test_load_manifest_rejects_path_outside_project(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside-extension.yaml"
    outside.write_text("apiVersion: sdai/v1\n", encoding="utf-8")
    try:
        with pytest.raises(PathSafetyError, match="must stay inside"):
            load_extension_manifest(tmp_path, outside)
    finally:
        outside.unlink(missing_ok=True)


def test_load_manifest_reports_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ExtensionManifestError, match="SDAI-EXT-011"):
        load_extension_manifest(tmp_path, Path(".sdai/extensions/missing.yaml"))
