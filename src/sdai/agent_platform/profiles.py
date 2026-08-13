from __future__ import annotations

from pathlib import Path
import re

from sdai.agent_platform.models import AgentProfile, Capability
from sdai.config import load_yaml
from sdai.path_safety import ensure_within_project


class ProfileError(RuntimeError):
    pass


_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_COST_CLASSES = frozenset({"economy", "standard", "premium"})
_ROUTING_TIERS = frozenset({"standard", "advanced"})
_RISKS = ("trivial", "standard", "critical", "regulated")
_COMPLEXITIES = ("low", "medium", "high", "extreme")


def _capabilities(values: object) -> tuple[Capability, ...]:
    if not isinstance(values, list):
        raise ProfileError("profile capabilities must be a list")
    try:
        return tuple(Capability(str(value)) for value in values)
    except ValueError as exc:
        raise ProfileError(str(exc)) from exc


def _environment_allowlist(values: object, profile: str) -> tuple[str, ...]:
    if values is None:
        return ()
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        raise ProfileError(f"profile '{profile}' environment_allowlist must be a string list")
    result: list[str] = []
    for value in values:
        name = value.strip()
        if not _ENV_NAME.fullmatch(name):
            raise ProfileError(f"profile '{profile}' has invalid environment variable name '{value}'")
        if name not in result:
            result.append(name)
    return tuple(result)


def _enum_values(values: object, allowed: tuple[str, ...], *, label: str, profile: str) -> tuple[str, ...]:
    if values is None:
        return allowed
    if not isinstance(values, list) or not values:
        raise ProfileError(f"profile '{profile}' {label} must be a non-empty list")
    result: list[str] = []
    for raw in values:
        value = str(raw).strip().lower()
        if value not in allowed:
            raise ProfileError(f"profile '{profile}' has unsupported {label} value '{raw}'")
        if value not in result:
            result.append(value)
    return tuple(result)


def _technologies(values: object, profile: str) -> tuple[str, ...]:
    if values is None:
        return ("*",)
    if not isinstance(values, list) or not values:
        raise ProfileError(f"profile '{profile}' technologies must be a non-empty list")
    result: list[str] = []
    for raw in values:
        if not isinstance(raw, str) or not raw.strip():
            raise ProfileError(f"profile '{profile}' technologies must contain non-empty strings")
        value = raw.strip().casefold()
        if value not in result:
            result.append(value)
    return tuple(result)


def _routing_metadata(raw: dict[object, object], profile: str) -> dict[str, object]:
    cost_class = str(raw.get("cost_class") or "standard").strip().lower()
    if cost_class not in _COST_CLASSES:
        raise ProfileError(f"profile '{profile}' cost_class must be economy, standard, or premium")
    routing_tier = str(raw.get("routing_tier") or "advanced").strip().lower()
    if routing_tier not in _ROUTING_TIERS:
        raise ProfileError(f"profile '{profile}' routing_tier must be standard or advanced")
    max_context = raw.get("max_context_chars", 1_000_000)
    priority = raw.get("routing_priority", 100)
    if not isinstance(max_context, int) or isinstance(max_context, bool) or not 1_000 <= max_context <= 10_000_000:
        raise ProfileError(f"profile '{profile}' max_context_chars must be between 1000 and 10000000")
    if not isinstance(priority, int) or isinstance(priority, bool) or not 0 <= priority <= 1_000_000:
        raise ProfileError(f"profile '{profile}' routing_priority must be between 0 and 1000000")
    return {
        "cost_class": cost_class,
        "routing_tier": routing_tier,
        "risk_levels": _enum_values(raw.get("risk_levels"), _RISKS, label="risk_levels", profile=profile),
        "complexity_levels": _enum_values(raw.get("complexity_levels"), _COMPLEXITIES, label="complexity_levels", profile=profile),
        "technologies": _technologies(raw.get("technologies"), profile),
        "max_context_chars": max_context,
        "routing_priority": priority,
    }


def load_profiles(project_root: Path) -> dict[str, AgentProfile]:
    project_root = project_root.resolve()
    path = ensure_within_project(
        project_root, project_root / ".sdai" / "agents.yaml", label="agent profiles path"
    )
    data = load_yaml(path)
    raw_profiles = data.get("profiles") or {}
    if not isinstance(raw_profiles, dict):
        raise ProfileError("agents.yaml profiles must be a mapping")

    profiles: dict[str, AgentProfile] = {}
    for name, raw in raw_profiles.items():
        if not isinstance(raw, dict):
            raise ProfileError(f"profile '{name}' must be a mapping")
        profile_name = str(name)
        routing = _routing_metadata(raw, profile_name)
        profiles[profile_name] = AgentProfile(
            name=profile_name,
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
            environment_allowlist=_environment_allowlist(raw.get("environment_allowlist"), profile_name),
            **routing,
        )
    return profiles


def load_routes(project_root: Path) -> dict[Capability, str]:
    project_root = project_root.resolve()
    path = ensure_within_project(
        project_root, project_root / ".sdai" / "routing.yaml", label="agent routing path"
    )
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
