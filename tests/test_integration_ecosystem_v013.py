from __future__ import annotations

from importlib import resources
from pathlib import Path

import pytest

from sdai.extensions.registry import RegistryLayer
from sdai.integration_manifest import (
    IntegrationCapability,
    IntegrationInputMode,
    IntegrationOutputMode,
    ProjectionKind,
    load_integration_manifest,
)
from sdai.integration_materialization import (
    IntegrationFileStatus,
    integration_status,
    materialize_integration,
)
from sdai.integration_registry import IntegrationSource, build_integration_registry


REPO_ROOT = Path(__file__).resolve().parents[1]
BUILTIN_ROOT = REPO_ROOT / "src" / "sdai" / "builtin_integrations"
CUSTOM_CLI_EXAMPLE = REPO_ROOT / "docs" / "examples" / "integrations" / "custom-cli.integration.yaml"

BASE_ECOSYSTEM = {
    "claude-code": ".claude/skills",
    "cline": ".cline/skills",
    "codex": ".codex/skills",
    "cursor": ".cursor/skills",
    "devin": ".cognition/skills",
    "factory-droid": ".factory/skills",
    "gemini-cli": ".gemini/skills",
    "generic-agents": ".agents/skills",
    "github-copilot": ".github/skills",
    "junie": ".junie/skills",
    "kimi-code": ".kimi-code/skills",
    "kiro": ".kiro/skills",
    "opencode": ".opencode/skills",
    "qwen-code": ".qwen/skills",
    "rovo-dev": ".rovodev/skills",
}

OPTIONAL_COMPANIONS = {
    "cursor-commands": (ProjectionKind.COMMAND, ".cursor/commands"),
    "factory-droids": (ProjectionKind.AGENT_FILE, ".factory/droids"),
    "qwen-code-commands": (ProjectionKind.COMMAND, ".qwen/commands"),
    "rovo-dev-subagents": (ProjectionKind.AGENT_FILE, ".rovodev/subagents"),
}

EXPECTED_IDS = set(BASE_ECOSYSTEM) | set(OPTIONAL_COMPANIONS)
ADVISORY_EXECUTION_IDS = {"claude-code", "codex", "gemini-cli", "github-copilot"}


def _registry():
    return build_integration_registry(
        (
            IntegrationSource(
                BUILTIN_ROOT,
                RegistryLayer.BUILTIN,
                "framework",
                locked=False,
            ),
        )
    )


def _create_projection_source(project: Path, kind: ProjectionKind, source: str) -> tuple[str, bytes]:
    root = project.joinpath(*Path(source).parts)
    if kind == ProjectionKind.SKILL:
        relative = "review/SKILL.md"
        path = root / "review" / "SKILL.md"
        data = b"---\nname: review\ndescription: Review code safely.\n---\n# Review\n"
    elif kind == ProjectionKind.COMMAND:
        relative = "review.md"
        path = root / "review.md"
        data = b"# Review command\nReview the current diff.\n"
    else:
        relative = "reviewer.md"
        path = root / "reviewer.md"
        data = b"---\nname: reviewer\ndescription: Review code.\n---\n# Reviewer\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return relative, data


def test_builtin_catalog_is_complete_versioned_and_deterministic() -> None:
    paths = sorted(BUILTIN_ROOT.glob("*.integration.yaml"))
    assert len(paths) == len(EXPECTED_IDS)

    manifests = [load_integration_manifest(path, root=BUILTIN_ROOT) for path in paths]
    assert {manifest.id for manifest in manifests} == EXPECTED_IDS
    assert all(str(manifest.version) == "1.0.0" for manifest in manifests)
    assert len({manifest.sha256 for manifest in manifests}) == len(manifests)

    registry = _registry()
    assert len(registry) == len(EXPECTED_IDS)
    exact = registry.list_all_exact()
    assert {item.id for item in exact} == EXPECTED_IDS
    assert [item.identity for item in exact] == sorted(item.identity for item in exact)
    assert all(item.selected_provenance.layer == RegistryLayer.BUILTIN for item in exact)
    assert registry.to_json() == _registry().to_json()
    assert registry.sha256 == _registry().sha256


def test_package_data_exposes_every_builtin_manifest() -> None:
    packaged = resources.files("sdai").joinpath("builtin_integrations")
    names = {
        item.name
        for item in packaged.iterdir()
        if item.name.endswith(".integration.yaml")
    }
    assert names == {f"{integration_id}.integration.yaml" for integration_id in EXPECTED_IDS}


@pytest.mark.parametrize("integration_id", sorted(EXPECTED_IDS))
def test_each_builtin_manifest_materializes_and_repeats_idempotently(
    tmp_path: Path,
    integration_id: str,
) -> None:
    registry = _registry()
    resolved = registry.resolve(integration_id, "1.0.0")
    assert resolved is not None
    project = tmp_path / integration_id
    project.mkdir()

    expected_outputs: list[tuple[Path, bytes]] = []
    for projection in resolved.manifest.projections:
        relative, data = _create_projection_source(project, projection.kind, projection.source)
        target = project.joinpath(*Path(projection.target).parts, *Path(relative).parts)
        expected_outputs.append((target, data))

    first = materialize_integration(project, resolved)
    assert first.identity == f"{integration_id}@1.0.0"
    assert first.manifest_sha256 == resolved.manifest_sha256
    assert first.provenance_layer == "builtin"
    assert first.provenance_source == "framework"
    assert first.files
    for target, data in expected_outputs:
        assert target.read_bytes() == data

    report = integration_status(project, resolved)
    assert report.status == IntegrationFileStatus.EXACT
    assert all(finding.status == IntegrationFileStatus.EXACT for finding in report.findings)

    state = project / ".sdai" / "integrations" / "install-state.json"
    state_before = state.read_bytes()
    second = materialize_integration(project, resolved)
    assert second == first
    assert state.read_bytes() == state_before


def test_base_skill_targets_and_optional_projection_kinds_are_exact() -> None:
    registry = _registry()
    for integration_id, target in BASE_ECOSYSTEM.items():
        resolved = registry.resolve(integration_id, "1.0.0")
        assert resolved is not None
        manifest = resolved.manifest
        assert IntegrationCapability.SKILLS in manifest.capabilities
        assert len(manifest.projections) == 1
        assert manifest.projections[0].kind == ProjectionKind.SKILL
        assert manifest.projections[0].target == target

    for integration_id, (kind, target) in OPTIONAL_COMPANIONS.items():
        resolved = registry.resolve(integration_id, "1.0.0")
        assert resolved is not None
        manifest = resolved.manifest
        assert len(manifest.projections) == 1
        assert manifest.projections[0].kind == kind
        assert manifest.projections[0].target == target


def test_audited_cli_manifests_are_advisory_shell_free_and_explicit_about_io() -> None:
    registry = _registry()
    for integration_id in ADVISORY_EXECUTION_IDS:
        resolved = registry.resolve(integration_id, "1.0.0")
        assert resolved is not None
        manifest = resolved.manifest
        assert IntegrationCapability.AGENT_EXECUTION in manifest.capabilities
        execution = manifest.execution
        assert execution is not None
        assert execution.executable
        assert execution.input_mode == IntegrationInputMode.STDIN
        assert execution.output_mode == IntegrationOutputMode.STDOUT
        assert execution.input_path is None
        assert execution.output_path is None
        assert execution.timeout_seconds == 600
        assert manifest.security.requires_network is True
        assert manifest.security.requires_workspace_write is False
        assert "sh" not in execution.executable.casefold()
        assert "cmd" not in execution.executable.casefold()
        assert all("{prompt}" not in value for value in (*execution.args_before_input, *execution.args_after_input))


def test_custom_cli_template_demonstrates_safe_json_contract_without_core_adapter() -> None:
    manifest = load_integration_manifest(CUSTOM_CLI_EXAMPLE, root=CUSTOM_CLI_EXAMPLE.parent)
    assert manifest.id == "custom-cli-example"
    assert manifest.capabilities == (IntegrationCapability.AGENT_EXECUTION,)
    assert manifest.projections == ()
    assert manifest.execution is not None
    assert manifest.execution.executable == "your-agent-cli"
    assert manifest.execution.args_before_input == ("run", "--non-interactive")
    assert manifest.execution.input_mode == IntegrationInputMode.STDIN
    assert manifest.execution.output_mode == IntegrationOutputMode.JSON_STDOUT
    assert manifest.security.requires_network is True
    assert manifest.security.requires_workspace_write is False
    assert manifest.security.environment == ("YOUR_AGENT_API_KEY",)
