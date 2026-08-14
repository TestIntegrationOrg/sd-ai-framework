from __future__ import annotations

from pathlib import Path

import pytest

from sdai.extensions.registry import RegistryLayer
from sdai.integration_execution import (
    IntegrationExecutionError,
    IntegrationExecutionRequest,
    IntegrationExecutionStatus,
    build_integration_execution_plan,
    execute_integration_plan,
)
from sdai.integration_manifest import INTEGRATION_MANIFEST_API_VERSION, IntegrationManifest
from sdai.integration_registry import IntegrationRegistry, ResolvedIntegration
from sdai.policy import EffectiveConfiguration, OperatingMode


def _policy(*, workspace_write: bool = True, protected_paths: tuple[str, ...] = (".sdai/**",)) -> EffectiveConfiguration:
    return EffectiveConfiguration(
        operating_mode=OperatingMode.ENTERPRISE,
        sources=("test",),
        allowed_profiles=None,
        allowed_providers=None,
        allowed_models={},
        capability_profiles={},
        capability_providers={},
        workspace_write=workspace_write,
        require_prior_approval_for_workspace_write=False,
        allow_force_approval_bypass=False,
        protected_paths=protected_paths,
        environment_allowlist=None,
        required_skills_map={},
    )


def _resolved(*, output_path: str | None = None) -> ResolvedIntegration:
    output_mode = "file" if output_path else "stdout"
    manifest = IntegrationManifest.from_dict(
        {
            "apiVersion": INTEGRATION_MANIFEST_API_VERSION,
            "id": "hardening-cli",
            "version": "1.0.0",
            "displayName": "Hardening CLI",
            "description": "Runtime ownership regression",
            "capabilities": ["agent-execution"],
            "projections": [],
            "execution": {
                "executable": "python",
                "argsBeforeInput": ["-c", "print('ok')"],
                "inputMode": "none",
                "inputPath": None,
                "argsAfterInput": [],
                "outputMode": output_mode,
                "outputPath": output_path,
                "timeoutSeconds": 5,
            },
            "security": {
                "requiresNetwork": False,
                "requiresWorkspaceWrite": bool(output_path),
                "environment": [],
            },
        }
    )
    registry = IntegrationRegistry()
    registry.register(
        manifest,
        layer=RegistryLayer.BUILTIN,
        source="framework",
        path="hardening.integration.yaml",
    )
    resolved = registry.resolve("hardening-cli", "1.0.0")
    assert resolved is not None
    return resolved


def test_request_constructor_rejects_private_input_that_does_not_match_declared_hash() -> None:
    resolved = _resolved()
    valid = IntegrationExecutionRequest.create(resolved, "safe")

    with pytest.raises(IntegrationExecutionError, match="input hash/length"):
        IntegrationExecutionRequest(
            integration_identity=valid.integration_identity,
            manifest_sha256=valid.manifest_sha256,
            input_sha256=valid.input_sha256,
            input_bytes=valid.input_bytes,
            _input_text="different runtime input",
        )


def test_preexisting_output_file_is_preserved_byte_for_byte(tmp_path: Path) -> None:
    relative = ".integration-runtime/result.txt"
    output = tmp_path / relative
    output.parent.mkdir(parents=True)
    original = b"user-owned-output\n"
    output.write_bytes(original)
    resolved = _resolved(output_path=relative)
    request = IntegrationExecutionRequest.create(resolved, "")
    policy = _policy(workspace_write=True)
    plan = build_integration_execution_plan(resolved, request, policy)

    result = execute_integration_plan(
        plan,
        request,
        project_root=tmp_path,
        policy=policy,
    )

    assert result.status == IntegrationExecutionStatus.IO_ERROR
    assert output.read_bytes() == original


def test_policy_protected_path_is_rechecked_after_plan_creation(tmp_path: Path) -> None:
    relative = ".integration-runtime/result.txt"
    resolved = _resolved(output_path=relative)
    request = IntegrationExecutionRequest.create(resolved, "")
    permissive = _policy(workspace_write=True, protected_paths=(".sdai/**",))
    plan = build_integration_execution_plan(resolved, request, permissive)

    tightened = _policy(
        workspace_write=True,
        protected_paths=(".sdai/**", ".integration-runtime/**"),
    )
    with pytest.raises(IntegrationExecutionError, match="now protected"):
        execute_integration_plan(
            plan,
            request,
            project_root=tmp_path,
            policy=tightened,
        )
