from __future__ import annotations

import os
from pathlib import Path
import sys

import pytest
import yaml

from sdai.plugin_steps import (
    PluginExecutorRegistry,
    PluginFinding,
    PluginResult,
    PluginStepError,
    execute_plugin_step,
    load_plugin_manifest,
    prepare_plugin_step,
)


class _Executor:
    def __init__(
        self,
        *,
        run_command: bool = False,
        write: bool = False,
        executable: str = "python",
    ) -> None:
        self.run_command = run_command
        self.write = write
        self.executable = executable
        self.called = False

    def execute(self, plan, services):
        self.called = True
        content = (
            services.read_text("src/input.txt")
            if plan.permissions.filesystem_read
            else ""
        )
        command_output = ""
        if self.run_command:
            completed = services.run_argv(
                self.executable,
                ["-c", "print('plugin-ok')"],
            )
            assert completed.returncode == 0
            command_output = completed.stdout.strip()
        if self.write:
            services.write_text("generated/plugin.txt", "plugin output\n")
        return PluginResult(
            status="passed",
            summary="completed",
            findings=(PluginFinding("PLUGIN-TEST", "info", "ok"),),
            data={"input": content, "command": command_output},
        )


def _plugin(
    root: Path,
    *,
    plugin_id: str = "sample",
    publisher: str = "acme",
    executor: str = "sample-executor",
    permissions: dict[str, object] | None = None,
) -> Path:
    path = root / ".sdai" / "plugin-steps" / f"{plugin_id}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(
            {
                "apiVersion": "sdai/v1",
                "kind": "PluginStep",
                "metadata": {
                    "id": plugin_id,
                    "version": "1.0.0",
                    "description": "test plugin",
                },
                "spec": {
                    "publisher": publisher,
                    "executor": executor,
                    "permissions": permissions
                    or {
                        "filesystem": {"read": [], "write": []},
                        "network": False,
                        "environment": [],
                        "commands": [],
                        "workspace_write": False,
                    },
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return path


def _policy(
    root: Path,
    *,
    trusted: tuple[str, ...] = ("acme",),
    allowed: tuple[str, ...] = ("sample",),
    denied: tuple[str, ...] = (),
    workspace_write: bool = False,
    read_paths: tuple[str, ...] = (),
    write_paths: tuple[str, ...] = (),
    commands: tuple[str, ...] = (),
    environment: tuple[str, ...] = (),
) -> Path:
    path = root / ".sdai" / "plugin-policy.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "allowed_plugins": list(allowed),
                "denied_plugins": list(denied),
                "trusted_publishers": list(trusted),
                "permissions": {
                    "filesystem": {
                        "read": list(read_paths),
                        "write": list(write_paths),
                    },
                    "network": False,
                    "environment": list(environment),
                    "commands": list(commands),
                    "workspace_write": workspace_write,
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return path


def test_manifest_is_versioned_strict_and_requires_permissions(tmp_path: Path) -> None:
    path = _plugin(tmp_path)
    manifest = load_plugin_manifest(tmp_path, "sample")
    assert manifest.publisher == "acme"
    assert manifest.executor == "sample-executor"
    assert manifest.source == ".sdai/plugin-steps/sample.yaml"

    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    payload["spec"].pop("permissions")
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    with pytest.raises(PluginStepError, match="SDAI-PLUGIN-001.*permissions"):
        load_plugin_manifest(tmp_path, "sample")


def test_network_permission_fails_closed_in_cross_platform_v1(tmp_path: Path) -> None:
    _plugin(
        tmp_path,
        permissions={
            "filesystem": {"read": [], "write": []},
            "network": True,
            "environment": [],
            "commands": [],
            "workspace_write": False,
        },
    )
    with pytest.raises(PluginStepError, match="SDAI-PLUGIN-004.*network permission"):
        load_plugin_manifest(tmp_path, "sample")


def test_custom_publisher_requires_explicit_trust_policy(tmp_path: Path) -> None:
    _plugin(tmp_path)
    with pytest.raises(
        PluginStepError,
        match="SDAI-PLUGIN-003.*publisher 'acme'.*not trusted",
    ):
        prepare_plugin_step(tmp_path, "sample", "scan")


def test_org_deny_cannot_be_weakened_by_repo_allow(tmp_path: Path) -> None:
    _plugin(tmp_path)
    _policy(tmp_path)
    org = tmp_path / "org-plugin-policy.yaml"
    org.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "allowed_plugins": ["sample"],
                "denied_plugins": ["sample"],
                "trusted_publishers": ["acme"],
                "permissions": {
                    "filesystem": {"read": [], "write": []},
                    "network": False,
                    "environment": [],
                    "commands": [],
                    "workspace_write": False,
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(PluginStepError, match="SDAI-PLUGIN-003.*denied"):
        prepare_plugin_step(
            tmp_path,
            "sample",
            "scan",
            environ={"SDAI_ORG_PLUGIN_POLICY_PATH": str(org.resolve())},
        )


def test_lower_policy_cannot_broaden_org_workspace_write_or_commands(
    tmp_path: Path,
) -> None:
    _plugin(
        tmp_path,
        permissions={
            "filesystem": {"read": ["src"], "write": ["generated"]},
            "network": False,
            "environment": [],
            "commands": ["python"],
            "workspace_write": True,
        },
    )
    _policy(
        tmp_path,
        workspace_write=True,
        read_paths=("src",),
        write_paths=("generated",),
        commands=("python",),
    )
    org = tmp_path / "org.yaml"
    org.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "allowed_plugins": ["sample"],
                "trusted_publishers": ["acme"],
                "permissions": {
                    "filesystem": {"read": ["src"], "write": []},
                    "network": False,
                    "environment": [],
                    "commands": [],
                    "workspace_write": False,
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(
        PluginStepError,
        match="SDAI-PLUGIN-003.*workspace_write",
    ):
        prepare_plugin_step(
            tmp_path,
            "sample",
            "scan",
            environ={"SDAI_ORG_PLUGIN_POLICY_PATH": str(org.resolve())},
        )


def test_executor_must_be_registered_by_trusted_installed_code(tmp_path: Path) -> None:
    _plugin(tmp_path)
    _policy(tmp_path)
    registry = PluginExecutorRegistry()
    with pytest.raises(PluginStepError, match="SDAI-PLUGIN-006.*not registered"):
        execute_plugin_step(
            tmp_path,
            "sample",
            "scan",
            registry=registry,
        )


def test_registered_executor_uses_permission_checked_services(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "input.txt").write_text(
        "café Δ\n",
        encoding="utf-8",
    )
    real_python = Path(sys.executable).resolve()
    executable = real_python.name
    _plugin(
        tmp_path,
        permissions={
            "filesystem": {"read": ["src"], "write": ["generated"]},
            "network": False,
            "environment": [],
            "commands": [executable],
            "workspace_write": True,
        },
    )
    _policy(
        tmp_path,
        workspace_write=True,
        read_paths=("src",),
        write_paths=("generated",),
        commands=(executable,),
    )
    executor = _Executor(run_command=True, write=True, executable=executable)
    registry = PluginExecutorRegistry()
    registry.register("sample-executor", executor)

    execution_env = {
        "SDAI_PLUGIN_TRUSTED_COMMAND_PATH": str(real_python.parent),
    }
    for name in ("SYSTEMROOT", "WINDIR"):
        if name in os.environ:
            execution_env[name] = os.environ[name]

    plan, result = execute_plugin_step(
        tmp_path,
        "sample",
        "scan",
        {"mode": "safe"},
        registry=registry,
        environ=execution_env,
    )

    assert executor.called is True
    assert plan.inputs == {"mode": "safe"}
    assert result is not None and result.status == "passed"
    assert result.data is not None
    assert result.data["input"] == "café Δ\n"
    assert result.data["command"] == "plugin-ok"
    assert (
        tmp_path / "generated" / "plugin.txt"
    ).read_text(encoding="utf-8") == "plugin output\n"


def test_framework_services_never_write_protected_source_of_truth(tmp_path: Path) -> None:
    _plugin(
        tmp_path,
        permissions={
            "filesystem": {"read": [], "write": ["."]},
            "network": False,
            "environment": [],
            "commands": [],
            "workspace_write": True,
        },
    )
    _policy(tmp_path, workspace_write=True, write_paths=(".",))

    class Writer:
        def execute(self, plan, services):
            services.write_text("specs/changes/X/requirements.md", "tamper")
            return PluginResult("passed", "unexpected")

    registry = PluginExecutorRegistry()
    registry.register("sample-executor", Writer())
    with pytest.raises(PluginStepError, match="SDAI-PLUGIN-005.*protected path"):
        execute_plugin_step(tmp_path, "sample", "write", registry=registry)


def test_safe_argv_has_no_shell_string_or_runtime_template_interpolation(
    tmp_path: Path,
) -> None:
    _plugin(
        tmp_path,
        permissions={
            "filesystem": {"read": [], "write": []},
            "network": False,
            "environment": [],
            "commands": ["python"],
            "workspace_write": False,
        },
    )
    _policy(tmp_path, commands=("python",))

    class UnsafeArg:
        def execute(self, plan, services):
            services.run_argv("python", ["-c", "print('${{ danger }}')"])
            return PluginResult("passed", "unexpected")

    registry = PluginExecutorRegistry()
    registry.register("sample-executor", UnsafeArg())
    with pytest.raises(PluginStepError, match="SDAI-PLUGIN-005.*template"):
        execute_plugin_step(tmp_path, "sample", "argv", registry=registry)


def test_dry_run_validates_manifest_and_policy_without_executor(tmp_path: Path) -> None:
    _plugin(tmp_path)
    _policy(tmp_path)
    plan, result = execute_plugin_step(
        tmp_path,
        "sample",
        "scan",
        dry_run=True,
    )
    assert plan.plugin.id == "sample"
    assert result is None


def test_plugin_inputs_are_json_only_and_reject_template_syntax(tmp_path: Path) -> None:
    _plugin(tmp_path)
    _policy(tmp_path)
    with pytest.raises(PluginStepError, match="SDAI-PLUGIN-001.*template"):
        prepare_plugin_step(
            tmp_path,
            "sample",
            "scan",
            {"target": "${{ shell }}"},
        )
