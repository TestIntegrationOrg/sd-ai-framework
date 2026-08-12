from __future__ import annotations

from pathlib import Path

import pytest

from sdai.agent_platform.definitions import load_agent_definition
from sdai.agent_platform.skills import load_skill
from sdai.entrypoint import main as entrypoint_main
from sdai.extensions import ExtensionKind
from sdai.extensions.scaffolding import (
    ScaffoldKind,
    create_extension_scaffold,
    validate_extension_scaffold,
)
from sdai.workflows import load_workflow


def _initialized(root: Path) -> None:
    config = root / ".sdai" / "config.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("version: 1\n", encoding="utf-8")


def test_create_skill_is_immediately_loadable_by_canonical_runtime(tmp_path: Path) -> None:
    result = create_extension_scaffold(tmp_path, ScaffoldKind.SKILL, "java-security")

    skill = load_skill(tmp_path, "java-security")
    assert result.kind is ScaffoldKind.SKILL
    assert [path.relative_to(tmp_path).as_posix() for path in result.paths] == [
        ".agents/skills/java-security/SKILL.md",
        ".agents/skills/java-security/sdai.yaml",
    ]
    assert skill.name == "java-security"
    assert skill.description
    assert "engineering technique" in skill.instructions


def test_create_agent_is_immediately_loadable_by_semantic_runtime(tmp_path: Path) -> None:
    result = create_extension_scaffold(tmp_path, ScaffoldKind.AGENT, "performance-engineer")

    definition = load_agent_definition(tmp_path, "performance-engineer")
    assert result.paths[0].relative_to(tmp_path).as_posix() == (
        ".sdai/agents/performance-engineer.agent.md"
    )
    assert definition.name == "performance-engineer"
    assert definition.description
    assert definition.capabilities


def test_create_workflow_is_immediately_loadable(tmp_path: Path) -> None:
    result = create_extension_scaffold(tmp_path, ScaffoldKind.WORKFLOW, "service-review")

    workflow = load_workflow(tmp_path, "service-review")
    assert result.paths[0].relative_to(tmp_path).as_posix() == (
        ".sdai/workflows/service-review.yaml"
    )
    assert workflow.name == "service-review"
    assert [step.id for step in workflow.steps] == ["validate"]


@pytest.mark.parametrize(
    ("kind", "extension_kind", "directory"),
    [
        (ScaffoldKind.WORKFLOW_COMPONENT, ExtensionKind.WORKFLOW_COMPONENT, "workflow-components"),
        (ScaffoldKind.VALIDATOR, ExtensionKind.VALIDATOR, "validators"),
        (ScaffoldKind.QUALITY_GATE, ExtensionKind.QUALITY_GATE, "quality-gates"),
        (ScaffoldKind.INTEGRATION, ExtensionKind.INTEGRATION, "integrations"),
        (ScaffoldKind.PACK, ExtensionKind.PACK, "packs"),
    ],
)
def test_create_manifest_extension_is_valid_immediately(
    tmp_path: Path,
    kind: ScaffoldKind,
    extension_kind: ExtensionKind,
    directory: str,
) -> None:
    result = create_extension_scaffold(tmp_path, kind, "example-extension")
    manifest = validate_extension_scaffold(tmp_path, kind, "example-extension")

    assert manifest is not None
    assert manifest.kind is extension_kind
    assert manifest.metadata.id == "example-extension"
    assert manifest.metadata.version == "0.1.0"
    assert result.paths[0].relative_to(tmp_path).as_posix() == (
        f".sdai/extensions/{directory}/example-extension.yaml"
    )


def test_manifest_validation_accepts_explicit_relative_path(tmp_path: Path) -> None:
    result = create_extension_scaffold(tmp_path, ScaffoldKind.VALIDATOR, "java-layering")

    manifest = validate_extension_scaffold(
        tmp_path,
        ScaffoldKind.VALIDATOR,
        str(result.paths[0].relative_to(tmp_path)),
    )

    assert manifest is not None
    assert manifest.metadata.id == "java-layering"


def test_manifest_validation_rejects_wrong_kind(tmp_path: Path) -> None:
    result = create_extension_scaffold(tmp_path, ScaffoldKind.VALIDATOR, "example")

    with pytest.raises(ValueError, match="expected Integration manifest"):
        validate_extension_scaffold(
            tmp_path,
            ScaffoldKind.INTEGRATION,
            str(result.paths[0].relative_to(tmp_path)),
        )


def test_new_scaffold_ids_use_portable_lowercase_manifest_grammar(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="SDAI-EXT-007"):
        create_extension_scaffold(tmp_path, ScaffoldKind.SKILL, "LegacySkill")

    with pytest.raises(ValueError, match="agent id must be lowercase kebab-case"):
        create_extension_scaffold(tmp_path, ScaffoldKind.AGENT, "agent.with-dot")


def test_skill_collision_preflight_prevents_partial_creation(tmp_path: Path) -> None:
    skill_root = tmp_path / ".agents" / "skills" / "existing"
    skill_root.mkdir(parents=True)
    sidecar = skill_root / "sdai.yaml"
    sidecar.write_text("capabilities:\n  - review\n", encoding="utf-8")

    with pytest.raises(FileExistsError, match="extension scaffold already exists"):
        create_extension_scaffold(tmp_path, ScaffoldKind.SKILL, "existing")

    assert not (skill_root / "SKILL.md").exists()
    assert sidecar.read_text(encoding="utf-8") == "capabilities:\n  - review\n"


def test_force_is_required_to_replace_owned_scaffold_files(tmp_path: Path) -> None:
    create_extension_scaffold(tmp_path, ScaffoldKind.AGENT, "developer-helper")
    path = tmp_path / ".sdai" / "agents" / "developer-helper.agent.md"
    path.write_text("user-edited\n", encoding="utf-8")

    with pytest.raises(FileExistsError):
        create_extension_scaffold(tmp_path, ScaffoldKind.AGENT, "developer-helper")

    assert path.read_text(encoding="utf-8") == "user-edited\n"

    create_extension_scaffold(
        tmp_path,
        ScaffoldKind.AGENT,
        "developer-helper",
        force=True,
    )
    assert path.read_text(encoding="utf-8").startswith("---\n")


def test_console_create_and_validate_commands(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _initialized(tmp_path)

    assert entrypoint_main(
        ["create", "skill", "api-design", "--path", str(tmp_path)]
    ) == 0
    created_output = capsys.readouterr().out
    assert "Created skill 'api-design'" in created_output
    assert ".agents/skills/api-design/SKILL.md" in created_output

    assert entrypoint_main(
        ["extensions", "validate", "skill", "api-design", "--path", str(tmp_path)]
    ) == 0
    validate_output = capsys.readouterr().out
    assert "Validated skill 'api-design'" in validate_output


def test_console_extension_alias_validates_manifest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _initialized(tmp_path)
    create_extension_scaffold(tmp_path, ScaffoldKind.PACK, "java-enterprise")

    assert entrypoint_main(
        ["extension", "validate", "pack", "java-enterprise", "--path", str(tmp_path)]
    ) == 0
    output = capsys.readouterr().out
    assert "kind=Pack version=0.1.0" in output


def test_console_refuses_uninitialized_project(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert entrypoint_main(
        ["create", "skill", "sample", "--path", str(tmp_path)]
    ) == 1
    captured = capsys.readouterr()
    assert "Not an SD-AI project" in captured.err


def test_console_delegates_existing_commands_to_legacy_cli(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert entrypoint_main(["init", "--path", str(tmp_path)]) == 0
    output = capsys.readouterr().out
    assert "Initialized SD-AI project" in output
    assert (tmp_path / ".sdai" / "config.yaml").exists()
