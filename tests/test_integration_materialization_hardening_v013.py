from __future__ import annotations

import json
from pathlib import Path

import pytest

import sdai.integration_materialization as materialization
from sdai.extensions.registry import RegistryLayer
from sdai.integration_manifest import INTEGRATION_MANIFEST_API_VERSION, IntegrationManifest
from sdai.integration_materialization import (
    INTEGRATION_INSTALL_STATE_API_VERSION,
    IntegrationFileStatus,
    IntegrationMaterializationError,
    integration_status,
    load_install_state,
    materialize_integration,
    operation_journal_path,
    repair_integration,
)
from sdai.integration_registry import IntegrationRegistry, ResolvedIntegration


def _resolved(version: str = "1.0.0") -> ResolvedIntegration:
    manifest = IntegrationManifest.from_dict(
        {
            "apiVersion": INTEGRATION_MANIFEST_API_VERSION,
            "id": "hardening-tool",
            "version": version,
            "displayName": "Hardening Tool",
            "description": "Materialization hardening",
            "capabilities": ["skills"],
            "projections": [
                {"kind": "skill", "source": "canonical/skills", "target": ".hardening/skills"}
            ],
            "execution": None,
            "security": {
                "requiresNetwork": False,
                "requiresWorkspaceWrite": False,
                "environment": [],
            },
        }
    )
    registry = IntegrationRegistry()
    registry.register(
        manifest,
        layer=RegistryLayer.BUILTIN,
        source="framework",
        path=f"hardening/{version}.integration.yaml",
    )
    resolved = registry.resolve("hardening-tool", version)
    assert resolved is not None
    return resolved


def _source(root: Path, text: str = "stable\n") -> Path:
    path = root / "canonical" / "skills" / "skill.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def test_state_rejects_duplicate_json_keys_and_noncanonical_hash(tmp_path: Path) -> None:
    state_path = tmp_path / ".sdai" / "integrations" / "install-state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        '{"apiVersion":"sdai.integration-install-state/v1","apiVersion":"sdai.integration-install-state/v1","integrations":[]}\n',
        encoding="utf-8",
    )
    with pytest.raises(IntegrationMaterializationError, match="duplicate key 'apiVersion'"):
        load_install_state(tmp_path)

    state_path.write_text(
        json.dumps(
            {
                "apiVersion": INTEGRATION_INSTALL_STATE_API_VERSION,
                "integrations": [
                    {
                        "files": [],
                        "id": "hardening-tool",
                        "identity": "hardening-tool@1.0.0",
                        "manifestSha256": "sha256:BAD",
                        "preservedPaths": [],
                        "provenance": {
                            "layer": "builtin",
                            "path": "hardening/1.0.0.integration.yaml",
                            "source": "framework",
                        },
                        "version": "1.0.0",
                    }
                ],
            },
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    with pytest.raises(IntegrationMaterializationError, match="lowercase SHA-256"):
        load_install_state(tmp_path)


def test_missing_source_is_reported_broken_without_touching_destination(tmp_path: Path) -> None:
    resolved = _resolved()

    report = integration_status(tmp_path, resolved)

    assert report.status == IntegrationFileStatus.BROKEN
    assert report.findings[0].path is None
    with pytest.raises(IntegrationMaterializationError):
        materialize_integration(tmp_path, resolved)
    assert not (tmp_path / ".hardening").exists()


def test_repair_refuses_unmanaged_conflict_and_creates_no_recovery_journal(tmp_path: Path) -> None:
    _source(tmp_path)
    resolved = _resolved()
    destination = tmp_path / ".hardening" / "skills" / "skill.md"
    destination.parent.mkdir(parents=True)
    destination.write_text("unmanaged\n", encoding="utf-8")

    with pytest.raises(IntegrationMaterializationError, match="unmanaged-conflict"):
        repair_integration(tmp_path, resolved)

    assert destination.read_text(encoding="utf-8") == "unmanaged\n"
    assert not operation_journal_path(tmp_path).exists()


def test_interrupted_upgrade_can_adopt_only_new_planned_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = _source(tmp_path, "v1\n")
    resolved = _resolved()
    materialize_integration(tmp_path, resolved)
    source.write_text("v2 Δ\n", encoding="utf-8", newline="\n")
    original_write_state = materialization._write_state
    monkeypatch.setattr(
        materialization,
        "_write_state",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("crash after upgrade bytes")),
    )
    with pytest.raises(RuntimeError):
        repair_integration(tmp_path, resolved)
    output = tmp_path / ".hardening" / "skills" / "skill.md"
    assert output.read_text(encoding="utf-8") == "v2 Δ\n"
    assert operation_journal_path(tmp_path).exists()

    monkeypatch.setattr(materialization, "_write_state", original_write_state)
    repaired = repair_integration(tmp_path, resolved)

    assert repaired.files[0].sha256 == materialization._hash_bytes("v2 Δ\n".encode("utf-8"))
    assert integration_status(tmp_path, resolved).status == IntegrationFileStatus.EXACT
    assert not operation_journal_path(tmp_path).exists()


def test_mismatched_stale_journal_blocks_a_different_integration_operation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _source(tmp_path)
    first = _resolved("1.0.0")
    original_write_state = materialization._write_state
    monkeypatch.setattr(
        materialization,
        "_write_state",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("crash")),
    )
    with pytest.raises(RuntimeError):
        materialize_integration(tmp_path, first)
    assert operation_journal_path(tmp_path).exists()

    source = tmp_path / "canonical" / "skills" / "skill.md"
    source.write_text("changed before a different version\n", encoding="utf-8")
    second = _resolved("2.0.0")
    monkeypatch.setattr(materialization, "_write_state", original_write_state)

    with pytest.raises(IntegrationMaterializationError, match="requires recovery"):
        materialize_integration(tmp_path, second)


def test_destination_directory_where_file_is_expected_is_broken_and_preserved(tmp_path: Path) -> None:
    _source(tmp_path)
    resolved = _resolved()
    destination = tmp_path / ".hardening" / "skills" / "skill.md"
    destination.mkdir(parents=True)

    report = integration_status(tmp_path, resolved)

    assert report.status == IntegrationFileStatus.BROKEN
    with pytest.raises(IntegrationMaterializationError):
        materialize_integration(tmp_path, resolved)
    assert destination.is_dir()
