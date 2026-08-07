from pathlib import Path

import pytest

from sdai.agent_platform.definitions import AgentDefinitionError, load_agent_definition
from sdai.artifacts import write_text
from sdai.scaffold import init_project


def _write_agent(root: Path, provider_block: str) -> None:
    write_text(
        root / ".sdai" / "agents" / "unsafe.agent.md",
        f"""---
name: unsafe
description: guardrail test agent
capabilities: [architecture]
execution_mode: advisory
providers:
  claude:
{provider_block}
---
# Unsafe
This definition exists only to test provider override guardrails.
""",
    )


def test_provider_override_cannot_broaden_permission_mode(tmp_path: Path):
    init_project(tmp_path)
    _write_agent(tmp_path, "    permissionMode: bypassPermissions")
    with pytest.raises(AgentDefinitionError, match="privilege-affecting"):
        load_agent_definition(tmp_path, "unsafe")


def test_provider_override_cannot_add_tools_or_network_scope(tmp_path: Path):
    init_project(tmp_path)
    _write_agent(tmp_path, "    allowed_tools: [Bash, Write]")
    with pytest.raises(AgentDefinitionError, match="privilege-affecting"):
        load_agent_definition(tmp_path, "unsafe")
