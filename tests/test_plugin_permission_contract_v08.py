from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from sdai.plugin_steps import PluginStepError, load_plugin_manifest, prepare_plugin_step


def _write_plugin(root: Path, permissions: dict[str, object]) -> None:
    path = root / ".sdai" / "plugin-steps" / "contract.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(
            {
                "apiVersion": "sdai/v1",
                "kind": "PluginStep",
                "metadata": {"id": "contract", "version": "1.0.0"},
                "spec": {
                    "publisher": "acme",
                    "executor": "contract-executor",
                    "permissions": permissions,
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def test_absolute_and_parent_filesystem_permissions_are_rejected(tmp_path: Path) -> None:
    for value in ("/tmp", "../secret", "C:/secret", r"src\\secret"):
        _write_plugin(
            tmp_path,
            {
                "filesystem": {"read": [value], "write": []},
                "network": False,
                "environment": [],
                "commands": [],
                "workspace_write": False,
            },
        )
        with pytest.raises(PluginStepError, match="SDAI-PLUGIN-001"):
            load_plugin_manifest(tmp_path, "contract")


def test_manifest_cannot_name_command_path_or_shell_expression(tmp_path: Path) -> None:
    for executable in ("/bin/sh", "cmd.exe /c", "python;whoami"):
        _write_plugin(
            tmp_path,
            {
                "filesystem": {"read": [], "write": []},
                "network": False,
                "environment": [],
                "commands": [executable],
                "workspace_write": False,
            },
        )
        with pytest.raises(PluginStepError, match="SDAI-PLUGIN-001.*commands"):
            load_plugin_manifest(tmp_path, "contract")


def test_policy_is_required_before_custom_publisher_can_prepare(tmp_path: Path) -> None:
    _write_plugin(
        tmp_path,
        {
            "filesystem": {"read": [], "write": []},
            "network": False,
            "environment": [],
            "commands": [],
            "workspace_write": False,
        },
    )
    with pytest.raises(PluginStepError, match="SDAI-PLUGIN-003.*not trusted"):
        prepare_plugin_step(tmp_path, "contract", "contract-step")
