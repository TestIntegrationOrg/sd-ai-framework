from __future__ import annotations

import math
import os
from pathlib import Path

import pytest
import yaml

from sdai.extensions.manifests import ExtensionKind, parse_extension_manifest
from sdai.plugin_steps import (
    PluginExecutorRegistry,
    PluginResult,
    PluginStepError,
    execute_plugin_step,
    prepare_plugin_step,
)


def _plugin(
    root: Path,
    *,
    write_paths: tuple[str, ...] = (),
    commands: tuple[str, ...] = (),
    workspace_write: bool = False,
) -> None:
    path = root / ".sdai" / "plugin-steps" / "sample.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(
            {
                "apiVersion": "sdai/v1",
                "kind": "PluginStep",
                "metadata": {"id": "sample", "version": "1.0.0"},
                "spec": {
                    "publisher": "acme",
                    "executor": "sample-executor",
                    "permissions": {
                        "filesystem": {"read": [], "write": list(write_paths)},
                        "network": False,
                        "environment": [],
                        "commands": list(commands),
                        "workspace_write": workspace_write,
                    },
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _policy(
    root: Path,
    *,
    write_paths: tuple[str, ...] = (),
    commands: tuple[str, ...] = (),
    workspace_write: bool = False,
) -> None:
    path = root / ".sdai" / "plugin-policy.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "allowed_plugins": ["sample"],
                "trusted_publishers": ["acme"],
                "permissions": {
                    "filesystem": {"read": [], "write": list(write_paths)},
                    "network": False,
                    "environment": [],
                    "commands": list(commands),
                    "workspace_write": workspace_write,
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _registry(executor) -> PluginExecutorRegistry:
    registry = PluginExecutorRegistry()
    registry.register("sample-executor", executor)
    return registry


def test_plugin_step_is_a_shared_sdai_v1_extension_kind() -> None:
    manifest = parse_extension_manifest(
        {
            "apiVersion": "sdai/v1",
            "kind": "PluginStep",
            "metadata": {"id": "sample", "version": "1.0.0"},
            "spec": {},
        }
    )
    assert manifest.kind is ExtensionKind.PLUGIN_STEP


def test_write_rejects_symlink_ancestor_alias_to_protected_specs(tmp_path: Path) -> None:
    _plugin(tmp_path, write_paths=("generated",), workspace_write=True)
    _policy(tmp_path, write_paths=("generated",), workspace_write=True)
    specs = tmp_path / "specs"
    specs.mkdir()
    alias = tmp_path / "generated"
    try:
        os.symlink(specs, alias, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")

    class Writer:
        def execute(self, plan, services):
            services.write_text("generated/tamper.txt", "tamper")
            return PluginResult("passed", "unexpected")

    with pytest.raises(PluginStepError, match="SDAI-PLUGIN-005.*symlink"):
        execute_plugin_step(
            tmp_path,
            "sample",
            "write",
            registry=_registry(Writer()),
        )
    assert not (specs / "tamper.txt").exists()


@pytest.mark.parametrize("relative", [".GIT/config", ".SDAI/policy.yaml", "SPECS/x.md"])
def test_protected_paths_are_case_insensitive_on_every_platform(
    tmp_path: Path,
    relative: str,
) -> None:
    _plugin(tmp_path, write_paths=(".",), workspace_write=True)
    _policy(tmp_path, write_paths=(".",), workspace_write=True)

    class Writer:
        def execute(self, plan, services):
            services.write_text(relative, "tamper")
            return PluginResult("passed", "unexpected")

    with pytest.raises(PluginStepError, match="SDAI-PLUGIN-005.*protected path"):
        execute_plugin_step(
            tmp_path,
            "sample",
            "write",
            registry=_registry(Writer()),
        )


@pytest.mark.parametrize(
    "relative",
    ["CODEOWNERS", ".github/CODEOWNERS", "docs/CODEOWNERS"],
)
def test_all_supported_codeowners_locations_are_protected(
    tmp_path: Path,
    relative: str,
) -> None:
    _plugin(tmp_path, write_paths=(".",), workspace_write=True)
    _policy(tmp_path, write_paths=(".",), workspace_write=True)

    class Writer:
        def execute(self, plan, services):
            services.write_text(relative, "tamper")
            return PluginResult("passed", "unexpected")

    with pytest.raises(PluginStepError, match="SDAI-PLUGIN-005.*protected path"):
        execute_plugin_step(
            tmp_path,
            "sample",
            "write",
            registry=_registry(Writer()),
        )


def test_command_execution_never_falls_back_to_host_or_workspace_path(tmp_path: Path) -> None:
    _plugin(tmp_path, commands=("python",))
    _policy(tmp_path, commands=("python",))
    workspace_bin = tmp_path / "bin"
    workspace_bin.mkdir()

    class Runner:
        def execute(self, plan, services):
            services.run_argv("python", ["--version"])
            return PluginResult("passed", "unexpected")

    with pytest.raises(
        PluginStepError,
        match="SDAI-PLUGIN-005.*SDAI_PLUGIN_TRUSTED_COMMAND_PATH",
    ):
        execute_plugin_step(
            tmp_path,
            "sample",
            "command",
            registry=_registry(Runner()),
            environ={"PATH": str(workspace_bin)},
        )


def test_trusted_command_path_cannot_be_workspace_controlled(tmp_path: Path) -> None:
    _plugin(tmp_path, commands=("python",))
    _policy(tmp_path, commands=("python",))
    workspace_bin = tmp_path / "bin"
    workspace_bin.mkdir()

    class Runner:
        def execute(self, plan, services):
            services.run_argv("python", ["--version"])
            return PluginResult("passed", "unexpected")

    with pytest.raises(PluginStepError, match="workspace-controlled directory"):
        execute_plugin_step(
            tmp_path,
            "sample",
            "command",
            registry=_registry(Runner()),
            environ={"SDAI_PLUGIN_TRUSTED_COMMAND_PATH": str(workspace_bin)},
        )


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_plugin_inputs_reject_non_finite_json_numbers(tmp_path: Path, value: float) -> None:
    _plugin(tmp_path)
    _policy(tmp_path)
    with pytest.raises(PluginStepError, match="SDAI-PLUGIN-001.*non-finite"):
        prepare_plugin_step(tmp_path, "sample", "scan", {"value": value})
