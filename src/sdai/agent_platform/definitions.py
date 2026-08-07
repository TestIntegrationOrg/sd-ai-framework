from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any

import yaml

from sdai.agent_platform.models import Capability, ExecutionMode
from sdai.config import load_yaml
from sdai.path_safety import ensure_within_project


class AgentDefinitionError(RuntimeError):
    pass


_AGENT_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_SECRET_KEY_PARTS = ("token", "secret", "password", "credential", "api_key", "apikey", "private_key")
_PRIVILEGE_KEY_PARTS = (
    "permission",
    "sandbox",
    "tool",
    "mcp",
    "network",
    "shell",
    "command",
    "approval",
    "workspace_write",
    "workspacewrite",
    "full_auto",
    "fullauto",
    "yolo",
    "hook",
)


@dataclass(frozen=True)
class AgentDefinition:
    name: str
    description: str
    capabilities: tuple[Capability, ...]
    skills: tuple[str, ...]
    instructions: str
    path: Path
    profile: str | None = None
    execution_mode: ExecutionMode = ExecutionMode.ADVISORY
    providers: dict[str, dict[str, Any]] | None = None

    def supports(self, capability: Capability) -> bool:
        return capability in self.capabilities


def _validate_name(value: str, label: str = "agent name") -> str:
    value = value.strip()
    if not _AGENT_NAME.fullmatch(value):
        raise AgentDefinitionError(
            f"{label} must be lowercase kebab-case using letters, numbers, and hyphens"
        )
    return value


def _frontmatter(text: str, path: Path) -> tuple[dict[str, Any], str]:
    if not text.startswith("---\n"):
        raise AgentDefinitionError(f"Agent definition '{path}' must start with YAML frontmatter")
    end = text.find("\n---\n", 4)
    if end < 0:
        raise AgentDefinitionError(f"Agent definition '{path}' has unterminated YAML frontmatter")
    raw = yaml.safe_load(text[4:end]) or {}
    if not isinstance(raw, dict):
        raise AgentDefinitionError(f"Agent definition '{path}' frontmatter must be a mapping")
    body = text[end + 5 :].strip()
    if not body:
        raise AgentDefinitionError(f"Agent definition '{path}' must contain instruction text")
    return raw, body


def _capabilities(raw: object, name: str) -> tuple[Capability, ...]:
    if not isinstance(raw, list) or not raw:
        raise AgentDefinitionError(f"Agent '{name}' capabilities must be a non-empty list")
    try:
        return tuple(Capability(str(value)) for value in raw)
    except ValueError as exc:
        raise AgentDefinitionError(f"Agent '{name}' has invalid capability: {exc}") from exc


def _skills(raw: object, name: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(isinstance(value, str) and value.strip() for value in raw):
        raise AgentDefinitionError(f"Agent '{name}' skills must be a string list")
    return tuple(value.strip() for value in raw)


def _contains_key_part(value: object, parts: tuple[str, ...], *, prefix: str = "") -> str | None:
    if isinstance(value, dict):
        for key, child in value.items():
            key_text = str(key).lower().replace("-", "_")
            dotted = f"{prefix}.{key}" if prefix else str(key)
            if any(part in key_text for part in parts):
                return dotted
            nested = _contains_key_part(child, parts, prefix=dotted)
            if nested:
                return nested
    elif isinstance(value, list):
        for index, child in enumerate(value):
            nested = _contains_key_part(child, parts, prefix=f"{prefix}[{index}]")
            if nested:
                return nested
    return None


def load_agent_definition(project_root: Path, name: str) -> AgentDefinition:
    project_root = project_root.resolve()
    name = _validate_name(name)
    path = project_root / ".sdai" / "agents" / f"{name}.agent.md"
    ensure_within_project(project_root, path, label="agent definition path")
    if not path.exists():
        raise AgentDefinitionError(f"Unknown semantic agent '{name}'")
    metadata, instructions = _frontmatter(path.read_text(encoding="utf-8"), path)
    metadata_name = _validate_name(str(metadata.get("name") or name), "frontmatter agent name")
    if metadata_name != name:
        raise AgentDefinitionError(
            f"Agent file '{path.name}' does not match frontmatter name '{metadata_name}'"
        )
    description = str(metadata.get("description") or "").strip()
    if not description:
        raise AgentDefinitionError(f"Agent '{name}' requires a description")
    capabilities = _capabilities(metadata.get("capabilities"), name)
    skills = _skills(metadata.get("skills"), name)
    profile = str(metadata["profile"]).strip() if metadata.get("profile") else None
    try:
        mode = ExecutionMode(str(metadata.get("execution_mode") or ExecutionMode.ADVISORY.value))
    except ValueError as exc:
        raise AgentDefinitionError(f"Agent '{name}' has invalid execution_mode") from exc
    providers_raw = metadata.get("providers") or {}
    if not isinstance(providers_raw, dict):
        raise AgentDefinitionError(f"Agent '{name}' providers must be a mapping")
    providers: dict[str, dict[str, Any]] = {}
    for provider, config in providers_raw.items():
        if config is None:
            config = {}
        if not isinstance(config, dict):
            raise AgentDefinitionError(f"Agent '{name}' provider '{provider}' must be a mapping")
        secret_key = _contains_key_part(config, _SECRET_KEY_PARTS)
        if secret_key:
            raise AgentDefinitionError(
                f"Agent '{name}' provider config contains credential-like key '{secret_key}'. "
                "Keep provider credentials outside agent definition files."
            )
        privilege_key = _contains_key_part(config, _PRIVILEGE_KEY_PARTS)
        if privilege_key:
            raise AgentDefinitionError(
                f"Agent '{name}' provider config contains privilege-affecting key '{privilege_key}'. "
                "Native permissions are derived from execution_mode and cannot be broadened by provider overrides."
            )
        providers[str(provider)] = dict(config)
    return AgentDefinition(
        name=name,
        description=description,
        capabilities=capabilities,
        skills=skills,
        instructions=instructions,
        path=path,
        profile=profile,
        execution_mode=mode,
        providers=providers,
    )


def list_agent_definitions(project_root: Path) -> list[AgentDefinition]:
    project_root = project_root.resolve()
    root = project_root / ".sdai" / "agents"
    ensure_within_project(project_root, root, label="agent definitions directory")
    if not root.exists():
        return []
    definitions: list[AgentDefinition] = []
    for path in sorted(root.glob("*.agent.md")):
        name = path.name[: -len(".agent.md")]
        definitions.append(load_agent_definition(project_root, name))
    return definitions


def load_agent_routes(project_root: Path) -> dict[Capability, str]:
    project_root = project_root.resolve()
    path = project_root / ".sdai" / "agent-routing.yaml"
    ensure_within_project(project_root, path, label="agent routing path")
    if not path.exists():
        return {}
    data = load_yaml(path)
    raw = data.get("routes") or {}
    if not isinstance(raw, dict):
        raise AgentDefinitionError("agent-routing.yaml routes must be a mapping")
    routes: dict[Capability, str] = {}
    for capability, agent in raw.items():
        routes[Capability(str(capability))] = _validate_name(str(agent), "routed agent name")
    return routes


def resolve_agent_definition(
    project_root: Path,
    capability: Capability,
    requested: str | None = None,
) -> AgentDefinition | None:
    name = requested
    if not name:
        name = load_agent_routes(project_root).get(capability)
    if not name:
        return None
    definition = load_agent_definition(project_root, name)
    if not definition.supports(capability):
        raise AgentDefinitionError(
            f"Semantic agent '{definition.name}' does not support '{capability.value}'"
        )
    return definition
