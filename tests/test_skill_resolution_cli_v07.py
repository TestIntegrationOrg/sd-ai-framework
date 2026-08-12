from __future__ import annotations

import json
from pathlib import Path

import yaml

from sdai.entrypoint import main as sdai_main
from sdai.extensions.scaffolding import create_extension_scaffold
from sdai.skill_resolution import load_skill_metadata


def _init(root: Path) -> None:
    config = root / ".sdai" / "config.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "operating_mode": "individual",
                "policy": {"repository": ".sdai/policy.yaml"},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _agent(root: Path) -> None:
    path = root / ".sdai" / "agents" / "developer.agent.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        """---
name: developer
description: Provider-neutral implementation role.
capabilities: [coding]
skills: []
execution_mode: advisory
providers: {}
---

Implement approved changes without changing canonical requirements.
""",
        encoding="utf-8",
    )


def _java(root: Path, version: str = "17") -> None:
    (root / "pom.xml").write_text(
        f"<project><properties><java.version>{version}</java.version></properties></project>\n",
        encoding="utf-8",
    )


def test_create_skill_generates_resolver_ready_metadata(tmp_path: Path) -> None:
    _init(tmp_path)

    result = create_extension_scaffold(tmp_path, "skill", "java-engineering")
    sidecar = result.paths[1]
    payload = yaml.safe_load(sidecar.read_text(encoding="utf-8"))
    metadata = load_skill_metadata(tmp_path, "java-engineering")

    assert payload == {
        "version": 1,
        "capabilities": [],
        "compatible_agents": [],
        "requires": [],
        "compatibility": {},
        "selection": {
            "auto": False,
            "roles": [],
            "capabilities": [],
            "task_keywords": [],
            "domains": [],
        },
    }
    assert metadata.name == "java-engineering"
    assert metadata.selection.auto is False
    assert metadata.compatibility == {}


def test_skill_resolve_cli_json_selects_compatible_auto_skill(tmp_path: Path, capsys) -> None:
    _init(tmp_path)
    _agent(tmp_path)
    create_extension_scaffold(tmp_path, "skill", "java-engineering")
    sidecar = tmp_path / ".agents" / "skills" / "java-engineering" / "sdai.yaml"
    sidecar.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "capabilities": ["coding"],
                "compatible_agents": ["developer"],
                "requires": [],
                "compatibility": {"languages": {"java": ">=17,<22"}},
                "selection": {
                    "auto": True,
                    "roles": ["developer"],
                    "capabilities": ["coding"],
                    "task_keywords": ["implementation"],
                    "domains": ["services"],
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    _java(tmp_path)

    exit_code = sdai_main(
        [
            "skill",
            "resolve",
            "--agent",
            "developer",
            "--capability",
            "coding",
            "--task",
            "service implementation",
            "--domain",
            "services",
            "--json",
            "--path",
            str(tmp_path),
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["version"] == 1
    assert payload["agent"] == "developer"
    assert payload["capability"] == "coding"
    assert payload["selected"] == ["java-engineering"]
    decision = next(item for item in payload["decisions"] if item["name"] == "java-engineering")
    assert decision["selected"] is True
    assert decision["origins"] == ["auto:java-engineering"]
    assert any("java 17" in reason for reason in decision["reasons"])
    assert payload["technology"]["technologies"]["languages"][0]["name"] == "java"


def test_skill_resolve_cli_returns_actionable_error_for_incompatible_requested_skill(
    tmp_path: Path,
    capsys,
) -> None:
    _init(tmp_path)
    _agent(tmp_path)
    create_extension_scaffold(tmp_path, "skill", "java-21-only")
    sidecar = tmp_path / ".agents" / "skills" / "java-21-only" / "sdai.yaml"
    sidecar.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "capabilities": ["coding"],
                "compatible_agents": ["developer"],
                "requires": [],
                "compatibility": {"languages": {"java": ">=21"}},
                "selection": {"auto": False},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    _java(tmp_path, "17")

    exit_code = sdai_main(
        [
            "skill",
            "resolve",
            "--agent",
            "developer",
            "--capability",
            "coding",
            "--skill",
            "java-21-only",
            "--path",
            str(tmp_path),
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 1
    assert captured.out == ""
    assert "SDAI-SKILL-003" in captured.err
    assert "java-21-only" in captured.err
    assert "java version 17" in captured.err
    assert ">=21" in captured.err
