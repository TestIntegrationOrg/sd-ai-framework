from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from sdai.entrypoint import main
from sdai.extensions.registry import RegistryLayer
from sdai.integration_execution import (
    IntegrationExecutionError,
    IntegrationExecutionRequest,
    IntegrationExecutionStatus,
    build_integration_execution_plan,
    execute_integration_plan,
)
from sdai.integration_manifest import (
    INTEGRATION_MANIFEST_API_VERSION,
    IntegrationInputMode,
    IntegrationManifest,
    IntegrationManifestError,
    IntegrationOutputMode,
    load_integration_manifest,
)
from sdai.integration_materialization import (
    IntegrationFileStatus,
    IntegrationMaterializationError,
    integration_status,
    load_install_state,
    materialize_integration,
    remove_integration,
    repair_integration,
)
from sdai.integration_registry import (
    IntegrationRegistry,
    IntegrationRegistryError,
    IntegrationSource,
    build_integration_registry,
)
from sdai.policy import EffectiveConfiguration, OperatingMode


REPO_ROOT = Path(__file__).resolve().parents[1]
BUILTIN_ROOT = REPO_ROOT / "src" / "sdai" / "builtin_integrations"
CUSTOM_CLI_EXAMPLE = (
    REPO_ROOT / "docs" / "examples" / "integrations" / "custom-cli.integration.yaml"
)


def _policy(*, workspace_write: bool = False) -> EffectiveConfiguration:
    return EffectiveConfiguration(
        operating_mode=OperatingMode.ENTERPRISE,
        sources=("0.13-release-gate",),
        allowed_profiles=None,
        allowed_providers=None,
        allowed_models={},
        capability_profiles={},
        capability_providers={},
        workspace_write=workspace_write,
        require_prior_approval_for_workspace_write=False,
        allow_force_approval_bypass=False,
        protected_paths=(".sdai/**", ".agents/**", "specs/**"),
        environment_allowlist=frozenset(),
        required_skills_map={},
    )


def _manifest_dict(
    version: str,
    *,
    source: str,
    target: str = ".release-gate/skills",
    input_mode: str = "argument",
    output_mode: str = "json-stdout",
    script: str | None = None,
    requires_workspace_write: bool = False,
) -> dict[str, object]:
    if script is None:
        script = (
            "import json,sys; "
            f"print(json.dumps({{'input': sys.argv[1], 'version': '{version}'}}, ensure_ascii=False))"
        )
    return {
        "apiVersion": INTEGRATION_MANIFEST_API_VERSION,
        "id": "release-gate-agent",
        "version": version,
        "displayName": "Release Gate Agent café Δ",
        "description": f"Extension-first 0.13 compatibility fixture {version}",
        "capabilities": ["agent-execution", "skills"],
        "projections": [
            {
                "kind": "skill",
                "source": source,
                "target": target,
            }
        ],
        "execution": {
            "executable": "python",
            "argsBeforeInput": ["-X", "utf8", "-c", script],
            "inputMode": input_mode,
            "inputPath": None,
            "argsAfterInput": [],
            "outputMode": output_mode,
            "outputPath": None,
            "timeoutSeconds": 30,
        },
        "security": {
            "requiresNetwork": False,
            "requiresWorkspaceWrite": requires_workspace_write,
            "environment": [],
        },
    }


def _write_manifest(path: Path, raw: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(raw, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
        newline="\n",
    )


def _write_skill(root: Path, relative: str, text: str) -> Path:
    path = root.joinpath(*Path(relative).parts, "review", "SKILL.md")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def _resolved_from_manifest(
    manifest: IntegrationManifest,
    *,
    source: str = "release-gate",
    path: str = "release-gate.integration.yaml",
):
    registry = IntegrationRegistry()
    registry.register(
        manifest,
        layer=RegistryLayer.REPO,
        source=source,
        path=path,
    )
    resolved = registry.resolve(manifest.id, str(manifest.version))
    assert resolved is not None
    return resolved


def test_extension_first_manifest_registry_execution_materialization_and_cli_journey(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = tmp_path / "project-café-Δ"
    catalog = project / ".sdai" / "integrations" / "manifests"
    project.mkdir()
    (project / ".sdai").mkdir()
    (project / ".sdai" / "config.yaml").write_text(
        "operating_mode: individual\n",
        encoding="utf-8",
    )
    empty_user = tmp_path / "empty-user-integrations"
    empty_user.mkdir()
    monkeypatch.setenv("SDAI_USER_INTEGRATIONS_PATH", str(empty_user))
    monkeypatch.delenv("SDAI_ORG_INTEGRATIONS_PATH", raising=False)

    _write_manifest(
        catalog / "release-gate-v1.integration.yaml",
        _manifest_dict("1.0.0", source="canonical/v1/skills"),
    )
    _write_manifest(
        catalog / "release-gate-v2.integration.yaml",
        _manifest_dict("2.0.0", source="canonical/v2/skills"),
    )
    _write_skill(
        project,
        "canonical/v1/skills",
        "---\nname: review\ndescription: Release review v1 café Δ.\n---\n# V1\n",
    )
    _write_skill(
        project,
        "canonical/v2/skills",
        "---\nname: review\ndescription: Release review v2 café Δ.\n---\n# V2\n",
    )

    builtin_only = build_integration_registry(
        (IntegrationSource(BUILTIN_ROOT, RegistryLayer.BUILTIN, "framework"),)
    )
    assert builtin_only.resolve("release-gate-agent") is None
    assert builtin_only.resolve("codex", "1.0.0") is not None
    generic = builtin_only.resolve("generic-agents", "1.0.0")
    assert generic is not None
    assert generic.manifest.projections[0].target == ".agents/skills"

    registry = build_integration_registry(
        (
            IntegrationSource(catalog, RegistryLayer.REPO, "release-repository"),
            IntegrationSource(BUILTIN_ROOT, RegistryLayer.BUILTIN, "framework"),
        )
    )
    v1 = registry.resolve("release-gate-agent", "1.0.0")
    v2 = registry.resolve("release-gate-agent", "2.0.0")
    latest = registry.resolve("release-gate-agent")
    assert v1 is not None and v2 is not None and latest is not None
    assert latest.identity == "release-gate-agent@2.0.0"
    assert v1.selected_provenance.layer == RegistryLayer.REPO
    assert v1.selected_provenance.source == "release-repository"
    assert v1.selected_provenance.path == "release-gate-v1.integration.yaml"
    assert v1.manifest_sha256 == v1.manifest.sha256

    payload = "literal; echo HACKED && $(touch never) | café Δ"
    request = IntegrationExecutionRequest.create(v1, payload)
    plan = build_integration_execution_plan(v1, request, _policy())
    assert payload not in request.to_json()
    assert payload not in plan.to_json()
    assert plan.input_mode == IntegrationInputMode.ARGUMENT
    assert plan.output_mode == IntegrationOutputMode.JSON_STDOUT
    assert plan.requires_workspace_write is False
    assert plan.runtime_argv(request)[-1] == payload

    result = execute_integration_plan(
        plan,
        request,
        project_root=project,
        policy=_policy(),
    )
    assert result.succeeded is True
    assert result.output == {"input": payload, "version": "1.0.0"}
    assert not (project / "never").exists()

    installed_v1 = materialize_integration(project, v1)
    native = project / ".release-gate" / "skills" / "review" / "SKILL.md"
    assert installed_v1.identity == "release-gate-agent@1.0.0"
    assert installed_v1.manifest_sha256 == v1.manifest_sha256
    assert installed_v1.provenance_layer == "repo"
    assert native.read_text(encoding="utf-8").endswith("# V1\n")
    assert integration_status(project, v1).status == IntegrationFileStatus.EXACT

    cli_code = main(
        [
            "integration",
            "status",
            "release-gate-agent",
            "--version",
            "1.0.0",
            "--repo-source",
            str(catalog),
            "--user-source",
            str(empty_user),
            "--json",
            "--path",
            str(project),
        ]
    )
    captured = capsys.readouterr()
    assert cli_code == 0
    assert captured.err == ""
    cli_payload = json.loads(captured.out)
    assert cli_payload["apiVersion"] == "sdai.integration-status-command/v1"
    assert cli_payload["status"] == "exact"
    assert cli_payload["installed"]["manifestSha256"] == v1.manifest_sha256
    assert cli_payload["report"]["desiredManifestSha256"] == v1.manifest_sha256

    native.unlink()
    repaired = repair_integration(project, v1)
    assert repaired == installed_v1
    assert native.read_text(encoding="utf-8").endswith("# V1\n")

    stale = integration_status(project, v2)
    assert stale.status == IntegrationFileStatus.STALE
    assert stale.installed_identity == "release-gate-agent@1.0.0"
    assert stale.desired_identity == "release-gate-agent@2.0.0"

    installed_v2 = materialize_integration(project, v2)
    assert installed_v2.identity == "release-gate-agent@2.0.0"
    assert native.read_text(encoding="utf-8").endswith("# V2\n")
    state_path = project / ".sdai" / "integrations" / "install-state.json"
    state_before = state_path.read_bytes()
    assert materialize_integration(project, v2) == installed_v2
    assert state_path.read_bytes() == state_before

    preserved = remove_integration(project, "release-gate-agent")
    assert preserved == ()
    assert not native.exists()
    assert load_install_state(project).integrations == ()

    custom = load_integration_manifest(CUSTOM_CLI_EXAMPLE, root=CUSTOM_CLI_EXAMPLE.parent)
    assert custom.id == "custom-cli-example"
    assert custom.execution is not None
    assert custom.execution.input_mode == IntegrationInputMode.STDIN
    assert custom.execution.output_mode == IntegrationOutputMode.JSON_STDOUT


def test_release_gate_rejects_shell_traversal_ambiguity_policy_malformed_output_and_ownership_loss(
    tmp_path: Path,
) -> None:
    shell = _manifest_dict("1.0.0", source="canonical/skills")
    assert isinstance(shell["execution"], dict)
    shell["execution"]["executable"] = "python -c"
    with pytest.raises(
        IntegrationManifestError,
        match="SDAI-INTEGRATION-003.*one executable",
    ):
        IntegrationManifest.from_dict(shell)

    traversal = _manifest_dict("1.0.0", source="../escape")
    with pytest.raises(
        IntegrationManifestError,
        match="SDAI-INTEGRATION-002.*unsafe path segment",
    ):
        IntegrationManifest.from_dict(traversal)

    ambiguous = IntegrationRegistry()
    for suffix in ("a", "b"):
        manifest = IntegrationManifest.from_dict(
            _manifest_dict(f"2.0.0+{suffix}", source="canonical/skills")
        )
        ambiguous.register(
            manifest,
            layer=RegistryLayer.REPO,
            source="release-repository",
            path=f"{suffix}.integration.yaml",
        )
    with pytest.raises(
        IntegrationRegistryError,
        match="SDAI-INTEGRATION-REG-004.*exact version",
    ):
        ambiguous.resolve("release-gate-agent")

    write_required = IntegrationManifest.from_dict(
        _manifest_dict(
            "1.0.0",
            source="canonical/skills",
            requires_workspace_write=True,
        )
    )
    write_resolved = _resolved_from_manifest(write_required)
    write_request = IntegrationExecutionRequest.create(write_resolved, "safe")
    with pytest.raises(
        IntegrationExecutionError,
        match="SDAI-INTEGRATION-EXEC-002.*workspace-write",
    ):
        build_integration_execution_plan(
            write_resolved,
            write_request,
            _policy(workspace_write=False),
        )

    malformed_raw = _manifest_dict(
        "1.0.0",
        source="canonical/skills",
        input_mode="none",
        script="print('{not-json}')",
    )
    malformed = IntegrationManifest.from_dict(malformed_raw)
    malformed_resolved = _resolved_from_manifest(malformed)
    malformed_request = IntegrationExecutionRequest.create(malformed_resolved, "")
    malformed_plan = build_integration_execution_plan(
        malformed_resolved,
        malformed_request,
        _policy(),
    )
    execution_root = tmp_path / "execution"
    execution_root.mkdir()
    malformed_result = execute_integration_plan(
        malformed_plan,
        malformed_request,
        project_root=execution_root,
        policy=_policy(),
    )
    assert malformed_result.status == IntegrationExecutionStatus.MALFORMED_OUTPUT
    assert malformed_result.error is not None
    assert malformed_result.error.code == "SDAI-INTEGRATION-EXEC-008"

    unmanaged_root = tmp_path / "unmanaged"
    unmanaged_root.mkdir()
    _write_skill(
        unmanaged_root,
        "canonical/skills",
        "---\nname: review\ndescription: Managed source.\n---\n# Managed\n",
    )
    unmanaged_manifest = IntegrationManifest.from_dict(
        _manifest_dict("1.0.0", source="canonical/skills")
    )
    unmanaged_resolved = _resolved_from_manifest(unmanaged_manifest)
    unmanaged_target = (
        unmanaged_root / ".release-gate" / "skills" / "review" / "SKILL.md"
    )
    unmanaged_target.parent.mkdir(parents=True)
    unmanaged_target.write_text("USER OWNED\n", encoding="utf-8")
    with pytest.raises(
        IntegrationMaterializationError,
        match="SDAI-INTEGRATION-MAT-005.*unmanaged-conflict",
    ):
        materialize_integration(unmanaged_root, unmanaged_resolved)
    assert unmanaged_target.read_text(encoding="utf-8") == "USER OWNED\n"

    modified_root = tmp_path / "modified"
    modified_root.mkdir()
    _write_skill(
        modified_root,
        "canonical/skills",
        "---\nname: review\ndescription: Original bytes.\n---\n# Original\n",
    )
    modified_resolved = _resolved_from_manifest(
        IntegrationManifest.from_dict(
            _manifest_dict("1.0.0", source="canonical/skills")
        )
    )
    materialize_integration(modified_root, modified_resolved)
    modified_target = (
        modified_root / ".release-gate" / "skills" / "review" / "SKILL.md"
    )
    modified_target.write_text("USER EDIT café Δ\n", encoding="utf-8", newline="\n")
    preserved = remove_integration(modified_root, "release-gate-agent")
    assert preserved == (".release-gate/skills/review/SKILL.md",)
    assert modified_target.read_text(encoding="utf-8") == "USER EDIT café Δ\n"
    assert load_install_state(modified_root).integrations == ()


def test_release_gate_rejects_symlink_ancestry_when_platform_supports_symlinks(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    project.mkdir()
    outside.mkdir()
    _write_skill(
        project,
        "canonical/skills",
        "---\nname: review\ndescription: Symlink check.\n---\n# Safe\n",
    )
    target_parent = project / ".release-gate"
    try:
        target_parent.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable on this platform: {exc}")

    resolved = _resolved_from_manifest(
        IntegrationManifest.from_dict(
            _manifest_dict("1.0.0", source="canonical/skills")
        )
    )
    report = integration_status(project, resolved)
    assert report.status == IntegrationFileStatus.BROKEN
    with pytest.raises(
        IntegrationMaterializationError,
        match="SDAI-INTEGRATION-MAT-003.*escapes the project root",
    ):
        materialize_integration(project, resolved)
    assert list(outside.iterdir()) == []