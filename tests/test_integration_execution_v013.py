from __future__ import annotations

from pathlib import Path
import threading

import pytest

from sdai.extensions.registry import RegistryLayer
from sdai.integration_execution import (
    INTEGRATION_EXECUTION_ERROR_API_VERSION,
    INTEGRATION_EXECUTION_PLAN_API_VERSION,
    INTEGRATION_EXECUTION_REQUEST_API_VERSION,
    INTEGRATION_EXECUTION_RESULT_API_VERSION,
    CancellationToken,
    IntegrationExecutionError,
    IntegrationExecutionRequest,
    IntegrationExecutionStatus,
    build_integration_execution_plan,
    execute_integration_plan,
)
from sdai.integration_manifest import INTEGRATION_MANIFEST_API_VERSION, IntegrationManifest
from sdai.integration_registry import IntegrationRegistry, ResolvedIntegration
from sdai.policy import EffectiveConfiguration, OperatingMode


def _policy(
    *,
    workspace_write: bool = True,
    environment_allowlist: frozenset[str] | None = None,
    protected_paths: tuple[str, ...] = (".sdai/**", ".agents/**", "specs/**"),
) -> EffectiveConfiguration:
    return EffectiveConfiguration(
        operating_mode=OperatingMode.ENTERPRISE,
        sources=("test-org", "test-repo"),
        allowed_profiles=None,
        allowed_providers=None,
        allowed_models={},
        capability_profiles={},
        capability_providers={},
        workspace_write=workspace_write,
        require_prior_approval_for_workspace_write=False,
        allow_force_approval_bypass=False,
        protected_paths=protected_paths,
        environment_allowlist=environment_allowlist,
        required_skills_map={},
    )


def _resolved(
    *,
    input_mode: str = "argument",
    input_path: str | None = None,
    output_mode: str = "stdout",
    output_path: str | None = None,
    args_before: list[str] | None = None,
    args_after: list[str] | None = None,
    timeout: int = 10,
    workspace_write: bool = False,
    environment: list[str] | None = None,
    executable: str = "python",
) -> ResolvedIntegration:
    manifest = IntegrationManifest.from_dict(
        {
            "apiVersion": INTEGRATION_MANIFEST_API_VERSION,
            "id": "test-cli",
            "version": "1.2.3",
            "displayName": "Test CLI café Δ",
            "description": "Portable execution test Integration",
            "capabilities": ["agent-execution"],
            "projections": [],
            "execution": {
                "executable": executable,
                "argsBeforeInput": args_before or [],
                "inputMode": input_mode,
                "inputPath": input_path,
                "argsAfterInput": args_after or [],
                "outputMode": output_mode,
                "outputPath": output_path,
                "timeoutSeconds": timeout,
            },
            "security": {
                "requiresNetwork": False,
                "requiresWorkspaceWrite": workspace_write,
                "environment": environment or [],
            },
        }
    )
    registry = IntegrationRegistry()
    registry.register(
        manifest,
        layer=RegistryLayer.BUILTIN,
        source="framework",
        path="test-cli.integration.yaml",
    )
    resolved = registry.resolve("test-cli", "1.2.3")
    assert resolved is not None
    return resolved


def _plan(
    resolved: ResolvedIntegration,
    text: str,
    policy: EffectiveConfiguration | None = None,
):
    request = IntegrationExecutionRequest.create(resolved, text)
    return request, build_integration_execution_plan(resolved, request, policy or _policy())


def test_request_and_plan_bind_input_without_serializing_raw_input_or_environment_values() -> None:
    resolved = _resolved(
        args_before=["-c", "import sys; print(sys.argv[1])"],
        environment=["ACME_TOKEN"],
    )
    secret_input = "café Δ ; $(echo injected) && rm -rf /"
    request, plan = _plan(resolved, secret_input, _policy(environment_allowlist=frozenset({"ACME_TOKEN"})))

    assert request.as_dict()["apiVersion"] == INTEGRATION_EXECUTION_REQUEST_API_VERSION
    assert plan.as_dict()["apiVersion"] == INTEGRATION_EXECUTION_PLAN_API_VERSION
    assert secret_input not in request.to_json()
    assert secret_input not in plan.to_json()
    assert "ACME_TOKEN" in plan.to_json()
    assert plan.runtime_argv(request) == (
        "python",
        "-c",
        "import sys; print(sys.argv[1])",
        secret_input,
    )
    assert plan.sha256.startswith("sha256:") and len(plan.sha256) == 71


def test_argument_input_with_shell_metacharacters_stays_one_argv_token(tmp_path: Path) -> None:
    resolved = _resolved(args_before=["-c", "import sys; print(sys.argv[1])"])
    payload = "literal; echo HACKED && $(touch never) | café Δ"
    request, plan = _plan(resolved, payload)

    result = execute_integration_plan(plan, request, project_root=tmp_path, policy=_policy())

    assert result.succeeded is True
    assert result.status == IntegrationExecutionStatus.SUCCEEDED
    assert result.output == payload
    assert result.as_dict()["apiVersion"] == INTEGRATION_EXECUTION_RESULT_API_VERSION
    assert result.plan_sha256 == plan.sha256
    assert not (tmp_path / "never").exists()


def test_stdin_input_is_utf8_and_stdout_is_normalized(tmp_path: Path) -> None:
    resolved = _resolved(
        input_mode="stdin",
        args_before=["-c", "import sys; sys.stdout.write(sys.stdin.read())"],
    )
    request, plan = _plan(resolved, "éditeur café Δ\n")

    result = execute_integration_plan(plan, request, project_root=tmp_path, policy=_policy())

    assert result.succeeded is True
    assert result.output == "éditeur café Δ"


def test_json_stdout_and_json_stderr_are_parsed_to_machine_data(tmp_path: Path) -> None:
    stdout_resolved = _resolved(
        input_mode="none",
        output_mode="json-stdout",
        args_before=["-c", "import json; print(json.dumps({'z': 1, 'café': 'Δ'}, ensure_ascii=False))"],
    )
    request, plan = _plan(stdout_resolved, "")
    stdout_result = execute_integration_plan(plan, request, project_root=tmp_path, policy=_policy())
    assert stdout_result.output == {"z": 1, "café": "Δ"}

    stderr_resolved = _resolved(
        input_mode="none",
        output_mode="json-stderr",
        args_before=["-c", "import json,sys; sys.stderr.write(json.dumps({'ok': True}))"],
    )
    request, plan = _plan(stderr_resolved, "")
    stderr_result = execute_integration_plan(plan, request, project_root=tmp_path, policy=_policy())
    assert stderr_result.output == {"ok": True}


def test_file_input_and_output_are_ephemeral_safe_runtime_files(tmp_path: Path) -> None:
    input_path = ".integration-runtime/input-café.txt"
    output_path = ".integration-runtime/output-café.txt"
    script = (
        "from pathlib import Path; import sys; "
        "Path(sys.argv[2]).write_text(Path(sys.argv[1]).read_text(encoding='utf-8').upper(), encoding='utf-8')"
    )
    resolved = _resolved(
        input_mode="file",
        input_path=input_path,
        output_mode="file",
        output_path=output_path,
        args_before=["-c", script],
        args_after=[output_path],
        workspace_write=True,
    )
    request, plan = _plan(resolved, "café Δ", _policy(workspace_write=True))

    result = execute_integration_plan(
        plan,
        request,
        project_root=tmp_path,
        policy=_policy(workspace_write=True),
    )

    assert result.succeeded is True
    assert result.output == "CAFÉ Δ"
    assert not (tmp_path / input_path).exists()
    assert not (tmp_path / output_path).exists()
    assert not (tmp_path / ".integration-runtime").exists()


def test_policy_blocks_workspace_write_and_required_environment_before_launch() -> None:
    write_resolved = _resolved(
        input_mode="none",
        args_before=["-c", "print('ok')"],
        workspace_write=True,
    )
    request = IntegrationExecutionRequest.create(write_resolved, "")
    with pytest.raises(IntegrationExecutionError, match="SDAI-INTEGRATION-EXEC-002.*workspace-write"):
        build_integration_execution_plan(write_resolved, request, _policy(workspace_write=False))

    env_resolved = _resolved(
        input_mode="none",
        args_before=["-c", "print('ok')"],
        environment=["ACME_TOKEN", "OTHER_TOKEN"],
    )
    request = IntegrationExecutionRequest.create(env_resolved, "")
    with pytest.raises(IntegrationExecutionError, match="SDAI-INTEGRATION-EXEC-002.*OTHER_TOKEN"):
        build_integration_execution_plan(
            env_resolved,
            request,
            _policy(environment_allowlist=frozenset({"ACME_TOKEN"})),
        )


def test_file_io_cannot_target_policy_protected_paths() -> None:
    resolved = _resolved(
        input_mode="file",
        input_path=".sdai/request.txt",
        output_mode="file",
        output_path=".sdai/response.txt",
        args_before=["-c", "print('unused')"],
        workspace_write=True,
    )
    request = IntegrationExecutionRequest.create(resolved, "hello")

    with pytest.raises(IntegrationExecutionError, match="SDAI-INTEGRATION-EXEC-002.*protected path"):
        build_integration_execution_plan(resolved, request, _policy(workspace_write=True))


def test_environment_values_are_runtime_only_and_policy_is_rechecked(tmp_path: Path) -> None:
    resolved = _resolved(
        input_mode="none",
        args_before=["-c", "import os; print('yes' if os.getenv('ACME_TOKEN') else 'no')"],
        environment=["ACME_TOKEN"],
    )
    policy = _policy(environment_allowlist=frozenset({"ACME_TOKEN"}))
    request, plan = _plan(resolved, "", policy)
    secret = "super-secret-value"

    result = execute_integration_plan(
        plan,
        request,
        project_root=tmp_path,
        policy=policy,
        environment={"PATH": __import__("os").environ.get("PATH", ""), "ACME_TOKEN": secret, "OTHER_SECRET": "nope"},
    )

    assert result.output == "yes"
    assert secret not in plan.to_json()
    assert secret not in result.to_json()
    assert "OTHER_SECRET" not in plan.to_json()

    with pytest.raises(IntegrationExecutionError, match="no longer permits"):
        execute_integration_plan(
            plan,
            request,
            project_root=tmp_path,
            policy=_policy(environment_allowlist=frozenset()),
        )


def test_nonzero_exit_is_normalized_without_serializing_stderr(tmp_path: Path) -> None:
    resolved = _resolved(
        input_mode="none",
        args_before=["-c", "import sys; sys.stderr.write('sensitive diagnostic'); sys.exit(7)"],
    )
    request, plan = _plan(resolved, "")

    result = execute_integration_plan(plan, request, project_root=tmp_path, policy=_policy())

    assert result.status == IntegrationExecutionStatus.EXIT_ERROR
    assert result.exit_code == 7
    assert result.error is not None
    assert result.error.code == "SDAI-INTEGRATION-EXEC-007"
    assert result.error.as_dict()["apiVersion"] == INTEGRATION_EXECUTION_ERROR_API_VERSION
    assert "sensitive diagnostic" not in result.to_json()


def test_launch_failure_timeout_and_prelaunch_cancellation_have_stable_states(tmp_path: Path) -> None:
    missing = _resolved(
        input_mode="none",
        args_before=[],
        executable="definitely-not-a-real-sdai-command",
    )
    request, plan = _plan(missing, "")
    launch = execute_integration_plan(plan, request, project_root=tmp_path, policy=_policy())
    assert launch.status == IntegrationExecutionStatus.LAUNCH_ERROR
    assert launch.error is not None and launch.error.code == "SDAI-INTEGRATION-EXEC-004"

    slow = _resolved(
        input_mode="none",
        args_before=["-c", "import time; time.sleep(5)"],
        timeout=1,
    )
    request, plan = _plan(slow, "")
    timed_out = execute_integration_plan(plan, request, project_root=tmp_path, policy=_policy())
    assert timed_out.status == IntegrationExecutionStatus.TIMED_OUT
    assert timed_out.error is not None and timed_out.error.code == "SDAI-INTEGRATION-EXEC-005"

    token = CancellationToken()
    token.cancel()
    request, plan = _plan(slow, "")
    cancelled = execute_integration_plan(
        plan,
        request,
        project_root=tmp_path,
        policy=_policy(),
        cancellation=token,
    )
    assert cancelled.status == IntegrationExecutionStatus.CANCELLED
    assert cancelled.error is not None and cancelled.error.code == "SDAI-INTEGRATION-EXEC-006"


def test_running_process_can_be_cancelled(tmp_path: Path) -> None:
    resolved = _resolved(
        input_mode="none",
        args_before=["-c", "import time; time.sleep(10)"],
        timeout=20,
    )
    request, plan = _plan(resolved, "")
    token = CancellationToken()
    timer = threading.Timer(0.2, token.cancel)
    timer.start()
    try:
        result = execute_integration_plan(
            plan,
            request,
            project_root=tmp_path,
            policy=_policy(),
            cancellation=token,
        )
    finally:
        timer.cancel()

    assert result.status == IntegrationExecutionStatus.CANCELLED


@pytest.mark.parametrize(
    "script",
    [
        "print('{not-json}')",
        "print('{\"a\":1,\"a\":2}')",
        "print('NaN')",
    ],
)
def test_malformed_json_is_normalized(script: str, tmp_path: Path) -> None:
    resolved = _resolved(
        input_mode="none",
        output_mode="json-stdout",
        args_before=["-c", script],
    )
    request, plan = _plan(resolved, "")

    result = execute_integration_plan(plan, request, project_root=tmp_path, policy=_policy())

    assert result.status == IntegrationExecutionStatus.MALFORMED_OUTPUT
    assert result.error is not None and result.error.code == "SDAI-INTEGRATION-EXEC-008"


def test_invalid_utf8_output_is_malformed(tmp_path: Path) -> None:
    resolved = _resolved(
        input_mode="none",
        args_before=["-c", "import sys; sys.stdout.buffer.write(bytes([255]))"],
    )
    request, plan = _plan(resolved, "")

    result = execute_integration_plan(plan, request, project_root=tmp_path, policy=_policy())

    assert result.status == IntegrationExecutionStatus.MALFORMED_OUTPUT
    assert "invalid UTF-8" in result.to_json()


def test_preexisting_runtime_file_is_never_overwritten(tmp_path: Path) -> None:
    runtime = tmp_path / ".integration-runtime"
    runtime.mkdir()
    existing = runtime / "input.txt"
    existing.write_text("user-owned", encoding="utf-8")
    resolved = _resolved(
        input_mode="file",
        input_path=".integration-runtime/input.txt",
        args_before=["-c", "print('unused')"],
        workspace_write=True,
    )
    request, plan = _plan(resolved, "new", _policy(workspace_write=True))

    result = execute_integration_plan(
        plan,
        request,
        project_root=tmp_path,
        policy=_policy(workspace_write=True),
    )

    assert result.status == IntegrationExecutionStatus.IO_ERROR
    assert existing.read_text(encoding="utf-8") == "user-owned"


def test_protected_path_mutation_is_restored_and_normalized(tmp_path: Path) -> None:
    protected = tmp_path / ".sdai"
    protected.mkdir()
    config = protected / "config.yaml"
    config.write_text("safe: true\n", encoding="utf-8")
    script = "from pathlib import Path; Path('.sdai/config.yaml').write_text('safe: false\\n', encoding='utf-8')"
    resolved = _resolved(
        input_mode="none",
        args_before=["-c", script],
        workspace_write=True,
    )
    policy = _policy(workspace_write=True, protected_paths=(".sdai/**",))
    request, plan = _plan(resolved, "", policy)

    result = execute_integration_plan(plan, request, project_root=tmp_path, policy=policy)

    assert result.status == IntegrationExecutionStatus.POLICY_VIOLATION
    assert result.error is not None and result.error.code == "SDAI-INTEGRATION-EXEC-009"
    assert config.read_text(encoding="utf-8") == "safe: true\n"


def test_request_binding_and_none_input_fail_closed() -> None:
    resolved = _resolved(input_mode="none", args_before=["-c", "print('ok')"])
    nonempty = IntegrationExecutionRequest.create(resolved, "unexpected")
    with pytest.raises(IntegrationExecutionError, match="inputMode 'none'"):
        build_integration_execution_plan(resolved, nonempty, _policy())

    other = _resolved(input_mode="none", args_before=["-c", "print('other')"])
    request = IntegrationExecutionRequest.create(other, "")
    with pytest.raises(IntegrationExecutionError, match="manifest hash"):
        build_integration_execution_plan(resolved, request, _policy())
