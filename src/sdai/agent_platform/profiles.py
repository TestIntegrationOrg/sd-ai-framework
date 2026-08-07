from __future__ import annotations

from pathlib import Path

from sdai.agent_platform.models import AgentProfile, Capability
from sdai.config import load_yaml


class ProfileError(RuntimeError):
    pass


def _capabilities(values: object) -> tuple[Capability, ...]:
    if not isinstance(values, list):
        raise ProfileError("profile capabilities must be a list")
    try:
        return tuple(Capability(str(value)) for value in values)
    except ValueError as exc:
        raise ProfileError(str(exc)) from exc


def load_profiles(project_root: Path) -> dict[str, AgentProfile]:
    path = project_root / ".sdai" / "agents.yaml"
    data = load_yaml(path)
    raw_profiles = data.get("profiles") or {}
    if not isinstance(raw_profiles, dict):
        raise ProfileError("agents.yaml profiles must be a mapping")

    profiles: dict[str, AgentProfile] = {}
    for name, raw in raw_profiles.items():
        if not isinstance(raw, dict):
            raise ProfileError(f"profile '{name}' must be a mapping")
        profiles[str(name)] = AgentProfile(
            name=str(name),
            provider=str(raw.get("provider") or name),
            capabilities=_capabilities(raw.get("capabilities") or []),
            prompt=str(raw.get("prompt") or "general.md"),
            skills=tuple(str(v) for v in (raw.get("skills") or [])),
            enabled=bool(raw.get("enabled", True)),
            model=str(raw["model"]) if raw.get("model") else None,
            timeout_seconds=int(raw.get("timeout_seconds", 600)),
            extra_args=tuple(str(v) for v in (raw.get("extra_args") or [])),
            command=tuple(str(v) for v in (raw.get("command") or [])),
            workspace_write_args=tuple(str(v) for v in (raw.get("workspace_write_args") or [])),
        )
    return profiles


def load_routes(project_root: Path) -> dict[Capability, str]:
    path = project_root / ".sdai" / "routing.yaml"
    data = load_yaml(path)
    raw = data.get("routes") or {}
    if not isinstance(raw, dict):
        raise ProfileError("routing.yaml routes must be a mapping")
    routes: dict[Capability, str] = {}
    for capability, profile in raw.items():
        routes[Capability(str(capability))] = str(profile)
    return routes


def resolve_profile(
    project_root: Path,
    capability: Capability,
    requested: str | None = None,
) -> AgentProfile:
    profiles = load_profiles(project_root)
    name = requested
    if not name:
        routes = load_routes(project_root)
        name = routes.get(capability)
    if not name:
        raise ProfileError(f"No agent route configured for capability '{capability.value}'")
    if name not in profiles:
        raise ProfileError(f"Unknown agent profile '{name}'")
    profile = profiles[name]
    if not profile.enabled:
        raise ProfileError(f"Agent profile '{name}' is disabled")
    if not profile.supports(capability):
        raise ProfileError(f"Agent profile '{name}' does not support '{capability.value}'")
    return profile
