from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from sdai.integration_manifest import (
    INTEGRATION_MANIFEST_API_VERSION,
    IntegrationCapability,
    IntegrationInputMode,
    IntegrationManifest,
    IntegrationManifestError,
    IntegrationOutputMode,
    ProjectionKind,
    load_integration_manifest,
)


def _manifest_dict() -> dict[str, object]:
    return {
        "apiVersion": INTEGRATION_MANIFEST_API_VERSION,
        "id": "acme-agent",
        "version": "1.2.3",
        "displayName": "Acme Agent café Δ",
        "description": "Declarative agent and IDE integration.",
        "capabilities": ["skills", "agent-execution", "commands", "agent-files"],
        "projections": [
            {"kind": "skill", "source": ".agents/skills", "target": ".acme/skills"},
            {"kind": "command", "source": ".sdai/commands", "target": ".acme/commands"},
            {"kind": "agent-file", "source": ".sdai/agents", "target": ".acme/agents"},
        ],
        "execution": {
            "executable": "acme-agent",
            "argsBeforeInput": ["run", "--mode", "safe mode"],
            "inputMode": "argument",
            "inputPath": None,
            "argsAfterInput": ["--format", "json"],
            "outputMode": "json-stdout",
            "outputPath": None,
            "timeoutSeconds": 600,
        },
        "security": {
            "requiresNetwork": True,
            "requiresWorkspaceWrite": False,
            "environment": ["ACME_API_KEY", "HTTPS_PROXY"],
        },
    }


def test_manifest_canonicalizes_unordered_sets_but_preserves_argv_order() -> None:
    first = IntegrationManifest.from_dict(_manifest_dict())
    reordered = deepcopy(_manifest_dict())
    reordered["capabilities"] = list(reversed(reordered["capabilities"]))  # type: ignore[arg-type]
    reordered["projections"] = list(reversed(reordered["projections"]))  # type: ignore[arg-type]
    reordered["security"]["environment"] = ["HTTPS_PROXY", "ACME_API_KEY"]  # type: ignore[index]
    second = IntegrationManifest.from_dict(reordered)

    assert first.to_text() == second.to_text()
    assert first.sha256 == second.sha256
    assert first.identity == "acme-agent@1.2.3"
    assert first.capabilities == (
        IntegrationCapability.AGENT_EXECUTION,
        IntegrationCapability.AGENT_FILES,
        IntegrationCapability.COMMANDS,
        IntegrationCapability.SKILLS,
    )
    assert tuple(item.kind for item in first.projections) == (
        ProjectionKind.AGENT_FILE,
        ProjectionKind.COMMAND,
        ProjectionKind.SKILL,
    )
    assert first.execution is not None
    assert first.execution.args_before_input == ("run", "--mode", "safe mode")
    assert first.execution.args_after_input == ("--format", "json")
    assert first.execution.input_mode == IntegrationInputMode.ARGUMENT
    assert first.execution.input_path is None
    assert first.execution.output_mode == IntegrationOutputMode.JSON_STDOUT
    assert first.to_text().endswith("\n")


def test_json_round_trip_is_exact_and_provider_metadata_is_not_embedded() -> None:
    manifest = IntegrationManifest.from_dict(_manifest_dict())

    restored = IntegrationManifest.from_json(manifest.to_json())

    assert restored == manifest
    assert restored.to_json() == manifest.to_json()
    assert "ACME_API_KEY" in restored.to_json()
    assert "secret" not in restored.to_json().lower()


def test_yaml_parsing_normalizes_unicode_to_nfc() -> None:
    raw = _manifest_dict()
    raw["displayName"] = "Cafe\u0301 Δ"
    raw["description"] = "E\u0301diteur integration"
    raw["projections"] = [
        {"kind": "skill", "source": ".agents/cafe\u0301", "target": ".acme/cafe\u0301"}
    ]
    raw["capabilities"] = ["skills"]
    raw["execution"] = None
    manifest = IntegrationManifest.from_yaml(
        yaml.safe_dump(raw, sort_keys=False, allow_unicode=True)
    )

    assert manifest.display_name == "Café Δ"
    assert manifest.description == "Éditeur integration"
    assert manifest.projections[0].source == ".agents/café"
    assert manifest.projections[0].target == ".acme/café"


def test_projection_only_integration_is_valid_without_execution() -> None:
    raw = _manifest_dict()
    raw["capabilities"] = ["skills"]
    raw["projections"] = [
        {"kind": "skill", "source": ".agents/skills", "target": ".agents-native/skills"}
    ]
    raw["execution"] = None
    raw["security"] = {
        "requiresNetwork": False,
        "requiresWorkspaceWrite": False,
        "environment": [],
    }

    manifest = IntegrationManifest.from_dict(raw)

    assert manifest.execution is None
    assert manifest.capabilities == (IntegrationCapability.SKILLS,)


def test_file_output_requires_exact_safe_path_and_workspace_write() -> None:
    raw = _manifest_dict()
    raw["execution"]["outputMode"] = "file"  # type: ignore[index]
    raw["execution"]["outputPath"] = ".sdai/integration-output/result.json"  # type: ignore[index]

    with pytest.raises(IntegrationManifestError, match="requiresWorkspaceWrite=true"):
        IntegrationManifest.from_dict(raw)

    raw["security"]["requiresWorkspaceWrite"] = True  # type: ignore[index]
    manifest = IntegrationManifest.from_dict(raw)
    assert manifest.execution is not None
    assert manifest.execution.output_path == ".sdai/integration-output/result.json"


def test_file_input_is_explicit_and_requires_workspace_write() -> None:
    raw = _manifest_dict()
    raw["execution"]["inputMode"] = "file"  # type: ignore[index]
    raw["execution"]["inputPath"] = ".sdai/integration-input/request.txt"  # type: ignore[index]

    with pytest.raises(IntegrationManifestError, match="requiresWorkspaceWrite=true"):
        IntegrationManifest.from_dict(raw)

    raw["security"]["requiresWorkspaceWrite"] = True  # type: ignore[index]
    manifest = IntegrationManifest.from_dict(raw)
    assert manifest.execution is not None
    assert manifest.execution.input_mode == IntegrationInputMode.FILE
    assert manifest.execution.input_path == ".sdai/integration-input/request.txt"


def test_stderr_modes_are_explicit_without_changing_argv_semantics() -> None:
    raw = _manifest_dict()
    raw["execution"]["outputMode"] = "json-stderr"  # type: ignore[index]

    manifest = IntegrationManifest.from_dict(raw)

    assert manifest.execution is not None
    assert manifest.execution.output_mode == IntegrationOutputMode.JSON_STDERR
    assert manifest.execution.output_path is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("id", "Acme"),
        ("id", "acme..agent"),
        ("id", " acme-agent"),
        ("version", "01.2.3"),
        ("version", "1.2"),
        ("version", "1.2.3 "),
    ],
)
def test_identity_and_version_are_strict(field: str, value: str) -> None:
    raw = _manifest_dict()
    raw[field] = value

    with pytest.raises(IntegrationManifestError, match="SDAI-INTEGRATION-001"):
        IntegrationManifest.from_dict(raw)


def test_unknown_fields_and_duplicate_yaml_keys_fail_closed() -> None:
    raw = _manifest_dict()
    raw["unexpected"] = True
    with pytest.raises(IntegrationManifestError, match="unsupported field"):
        IntegrationManifest.from_dict(raw)

    duplicate_yaml = """
apiVersion: sdai.integration-manifest/v1
id: acme-agent
id: shadow-agent
version: 1.0.0
displayName: Acme
description: test
capabilities: [skills]
projections:
  - kind: skill
    source: .agents/skills
    target: .acme/skills
execution: null
security:
  requiresNetwork: false
  requiresWorkspaceWrite: false
  environment: []
"""
    with pytest.raises(IntegrationManifestError, match="YAML is malformed"):
        IntegrationManifest.from_yaml(duplicate_yaml)


def test_duplicate_json_keys_fail_closed() -> None:
    value = IntegrationManifest.from_dict(_manifest_dict()).to_json()
    duplicate = value.replace(
        '"id":"acme-agent"',
        '"id":"acme-agent","id":"shadow-agent"',
    )

    with pytest.raises(IntegrationManifestError, match="duplicate key 'id'"):
        IntegrationManifest.from_json(duplicate)


@pytest.mark.parametrize(
    "path",
    [
        "../escape",
        "/absolute/path",
        "C:/windows/path",
        "folder\\windows",
        "folder//double",
        "folder/./dot",
        "NUL/file",
        "folder/CON.txt",
        "folder/name?bad",
        "folder/trailing ",
    ],
)
def test_projection_paths_are_portable_and_fail_closed(path: str) -> None:
    raw = _manifest_dict()
    raw["capabilities"] = ["skills"]
    raw["projections"] = [{"kind": "skill", "source": ".agents/skills", "target": path}]
    raw["execution"] = None

    with pytest.raises(IntegrationManifestError, match="SDAI-INTEGRATION-002"):
        IntegrationManifest.from_dict(raw)


def test_projection_targets_must_not_overlap_or_repeat() -> None:
    raw = _manifest_dict()
    raw["capabilities"] = ["skills", "commands"]
    raw["projections"] = [
        {"kind": "skill", "source": ".agents/skills", "target": ".tool"},
        {"kind": "command", "source": ".sdai/commands", "target": ".tool/commands"},
    ]
    raw["execution"] = None

    with pytest.raises(IntegrationManifestError, match="overlap"):
        IntegrationManifest.from_dict(raw)


def test_projection_kind_must_match_declared_capability() -> None:
    raw = _manifest_dict()
    raw["capabilities"] = ["skills"]
    raw["projections"] = [
        {"kind": "command", "source": ".sdai/commands", "target": ".tool/commands"}
    ]
    raw["execution"] = None

    with pytest.raises(IntegrationManifestError, match="requires capability 'commands'"):
        IntegrationManifest.from_dict(raw)


def test_projection_capability_requires_a_projection() -> None:
    raw = _manifest_dict()
    raw["capabilities"] = ["skills"]
    raw["projections"] = []
    raw["execution"] = None

    with pytest.raises(IntegrationManifestError, match="requires at least one 'skill' projection"):
        IntegrationManifest.from_dict(raw)


def test_execution_and_agent_execution_capability_must_match() -> None:
    raw = _manifest_dict()
    raw["capabilities"] = ["skills", "commands", "agent-files"]

    with pytest.raises(IntegrationManifestError, match="agent-execution"):
        IntegrationManifest.from_dict(raw)

    raw = _manifest_dict()
    raw["execution"] = None
    with pytest.raises(IntegrationManifestError, match="agent-execution"):
        IntegrationManifest.from_dict(raw)


@pytest.mark.parametrize(
    "executable",
    [
        "bash -lc echo",
        "/usr/bin/acme-agent",
        "C:\\tools\\acme.exe",
        "../acme-agent",
        "acme-agent;rm",
        " acme-agent",
    ],
)
def test_executable_is_one_portable_token_not_a_shell_command(executable: str) -> None:
    raw = _manifest_dict()
    raw["execution"]["executable"] = executable  # type: ignore[index]

    with pytest.raises(IntegrationManifestError, match="SDAI-INTEGRATION-003"):
        IntegrationManifest.from_dict(raw)


def test_argv_rejects_implicit_prompt_placeholder_but_preserves_atomic_shell_characters() -> None:
    raw = _manifest_dict()
    raw["execution"]["argsBeforeInput"] = ["--prompt={prompt}"]  # type: ignore[index]
    with pytest.raises(IntegrationManifestError, match="implicit prompt interpolation"):
        IntegrationManifest.from_dict(raw)

    raw["execution"]["argsBeforeInput"] = ["--literal", "$(not-a-shell-because-no-shell)"]  # type: ignore[index]
    manifest = IntegrationManifest.from_dict(raw)
    assert manifest.execution is not None
    assert manifest.execution.args_before_input[-1] == "$(not-a-shell-because-no-shell)"


@pytest.mark.parametrize("name", ["token=secret", "lowercase", "BAD-NAME", "1TOKEN"])
def test_security_environment_contains_names_only(name: str) -> None:
    raw = _manifest_dict()
    raw["security"]["environment"] = [name]  # type: ignore[index]

    with pytest.raises(IntegrationManifestError, match="environment"):
        IntegrationManifest.from_dict(raw)


def test_security_environment_is_sorted_and_deduplicated() -> None:
    raw = _manifest_dict()
    raw["security"]["environment"] = ["Z_TOKEN", "A_TOKEN"]  # type: ignore[index]
    manifest = IntegrationManifest.from_dict(raw)
    assert manifest.security.environment == ("A_TOKEN", "Z_TOKEN")

    raw["security"]["environment"] = ["A_TOKEN", "A_TOKEN"]  # type: ignore[index]
    with pytest.raises(IntegrationManifestError, match="must not contain duplicates"):
        IntegrationManifest.from_dict(raw)


def test_timeout_and_file_path_semantics_are_strict() -> None:
    raw = _manifest_dict()
    raw["execution"]["timeoutSeconds"] = True  # type: ignore[index]
    with pytest.raises(IntegrationManifestError, match="must be an integer"):
        IntegrationManifest.from_dict(raw)

    raw = _manifest_dict()
    raw["execution"]["outputPath"] = "unexpected.json"  # type: ignore[index]
    with pytest.raises(IntegrationManifestError, match="must be null"):
        IntegrationManifest.from_dict(raw)

    raw = _manifest_dict()
    raw["execution"]["inputPath"] = "unexpected.txt"  # type: ignore[index]
    with pytest.raises(IntegrationManifestError, match="must be null"):
        IntegrationManifest.from_dict(raw)


def test_load_manifest_reads_utf8_and_rejects_symlink_manifest(tmp_path: Path) -> None:
    root = tmp_path / "éditeur"
    root.mkdir()
    path = root / "integration.yaml"
    path.write_text(
        yaml.safe_dump(_manifest_dict(), sort_keys=False, allow_unicode=True),
        encoding="utf-8",
        newline="\n",
    )

    loaded = load_integration_manifest(path, root=root)
    assert loaded.display_name == "Acme Agent café Δ"

    link = root / "linked.yaml"
    try:
        link.symlink_to(path.name)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable on this platform: {exc}")
    with pytest.raises(IntegrationManifestError, match="must not be a symlink"):
        load_integration_manifest(link, root=root)


def test_load_manifest_rejects_symlink_ancestor(tmp_path: Path) -> None:
    root = tmp_path / "root"
    real = root / "real"
    real.mkdir(parents=True)
    manifest = real / "integration.yaml"
    manifest.write_text(
        yaml.safe_dump(_manifest_dict(), sort_keys=False, allow_unicode=True),
        encoding="utf-8",
        newline="\n",
    )
    linked = root / "linked"
    try:
        linked.symlink_to(real.name, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable on this platform: {exc}")

    with pytest.raises(IntegrationManifestError, match="symlink components"):
        load_integration_manifest(linked / "integration.yaml", root=root)
