from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import os
from pathlib import Path
from typing import Any, Mapping, TYPE_CHECKING

if TYPE_CHECKING:
    from sdai.agent_platform.models import AgentProfile, Capability, ExecutionMode
from sdai.config import load_yaml
from sdai.path_safety import ensure_within_project


class PolicyError(RuntimeError):
    pass


class OperatingMode(StrEnum):
    INDIVIDUAL = "individual"
    ENTERPRISE = "enterprise"


_CAPABILITIES = {
    "requirements", "architecture", "planning", "coding",
    "review", "testing", "security", "documentation",
}


CORE_PROTECTED_PATHS: tuple[str, ...] = (
    ".sdai/**",
    ".agents/**",
    ".codex/agents/**",
    ".claude/agents/**",
    ".claude/skills/**",
    ".gemini/agents/**",
    ".github/agents/**",
    ".github/workflows/**",
    ".github/CODEOWNERS",
    "CODEOWNERS",
    ".git/config",
    ".git/HEAD",
    ".git/index",
    ".git/hooks/**",
    ".git/refs/**",
    "specs/**",
)


@dataclass(frozen=True)
class PolicyLayer:
    source: str
    allowed_profiles: frozenset[str] | None = None
    allowed_providers: frozenset[str] | None = None
    allowed_models: dict[str, frozenset[str]] | None = None
    capability_profiles: dict[str, frozenset[str]] | None = None
    capability_providers: dict[str, frozenset[str]] | None = None
    workspace_write: bool | None = None
    require_prior_approval_for_workspace_write: bool | None = None
    allow_force_approval_bypass: bool | None = None
    protected_paths: tuple[str, ...] = ()
    environment_allowlist: frozenset[str] | None = None
    required_skills: dict[str, tuple[str, ...]] | None = None


@dataclass(frozen=True)
class EffectiveConfiguration:
    operating_mode: OperatingMode
    sources: tuple[str, ...]
    allowed_profiles: frozenset[str] | None
    allowed_providers: frozenset[str] | None
    allowed_models: dict[str, frozenset[str]]
    capability_profiles: dict[str, frozenset[str]]
    capability_providers: dict[str, frozenset[str]]
    workspace_write: bool
    require_prior_approval_for_workspace_write: bool
    allow_force_approval_bypass: bool
    protected_paths: tuple[str, ...]
    environment_allowlist: frozenset[str] | None
    required_skills_map: dict[str, tuple[str, ...]]

    def assert_base_profile_allowed(
        self,
        profile: "AgentProfile",
        mode: "ExecutionMode",
    ) -> None:
        if self.allowed_profiles is not None and profile.name not in self.allowed_profiles:
            raise PolicyError(
                f"Profile '{profile.name}' is not permitted by the effective SD-AI policy"
            )
        if self.allowed_providers is not None and profile.provider not in self.allowed_providers:
            raise PolicyError(
                f"Provider '{profile.provider}' is not permitted by the effective SD-AI policy"
            )

        # Both provider-level and profile-level model rules apply. A lower layer cannot
        # use a profile-specific rule to escape an organization provider model allowlist.
        model_rules = [
            rule
            for rule in (
                self.allowed_models.get(profile.provider),
                self.allowed_models.get(profile.name),
            )
            if rule is not None
        ]
        if model_rules:
            approved = set(model_rules[0])
            for rule in model_rules[1:]:
                approved.intersection_update(rule)
            if not profile.model:
                raise PolicyError(
                    f"Profile '{profile.name}' must pin an approved model because policy restricts models"
                )
            if profile.model not in approved:
                raise PolicyError(
                    f"Model '{profile.model}' is not permitted for profile '{profile.name}'"
                )

        if mode.value == "workspace-write" and not self.workspace_write:
            raise PolicyError("workspace-write execution is disabled by the effective SD-AI policy")

    def assert_profile_allowed(
        self,
        profile: "AgentProfile",
        capability: "Capability",
        mode: "ExecutionMode",
    ) -> None:
        self.assert_base_profile_allowed(profile, mode)
        capability_name = capability.value
        capability_profiles = self.capability_profiles.get(capability_name)
        if capability_profiles is not None and profile.name not in capability_profiles:
            raise PolicyError(
                f"Profile '{profile.name}' is not permitted for capability '{capability_name}'"
            )
        capability_providers = self.capability_providers.get(capability_name)
        if capability_providers is not None and profile.provider not in capability_providers:
            raise PolicyError(
                f"Provider '{profile.provider}' is not permitted for capability '{capability_name}'"
            )

    def required_skills(self, capability: "Capability") -> tuple[str, ...]:
        return self.required_skills_map.get(capability.value, ())


def _optional_bool(mapping: dict[str, Any], key: str, *, label: str) -> bool | None:
    if key not in mapping:
        return None
    value = mapping[key]
    if not isinstance(value, bool):
        raise PolicyError(f"{label} must be true or false")
    return value


def _optional_string_set(value: object, *, label: str) -> frozenset[str] | None:
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise PolicyError(f"{label} must be a string list")
    return frozenset(item.strip() for item in value)


def _model_rules(value: object, *, source: str) -> dict[str, frozenset[str]]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise PolicyError(f"{source}: providers.allowed_models must be a mapping")
    rules: dict[str, frozenset[str]] = {}
    for key, models in value.items():
        parsed = _optional_string_set(models, label=f"{source}: allowed models for {key}")
        assert parsed is not None
        rules[str(key)] = parsed
    return rules


def _capability_rules(
    value: object,
    *,
    source: str,
) -> tuple[dict[str, frozenset[str]], dict[str, frozenset[str]]]:
    if value is None:
        return {}, {}
    if not isinstance(value, dict):
        raise PolicyError(f"{source}: capabilities must be a mapping")
    profiles: dict[str, frozenset[str]] = {}
    providers: dict[str, frozenset[str]] = {}
    for capability_name, raw in value.items():
        capability = str(capability_name)
        if capability not in _CAPABILITIES:
            raise PolicyError(f"{source}: unknown capability '{capability_name}'")
        if not isinstance(raw, dict):
            raise PolicyError(f"{source}: capability '{capability}' must be a mapping")
        _reject_unknown(
            raw,
            {"allowed_profiles", "allowed_providers"},
            label=f"{source}: capability '{capability}'",
        )
        allowed_profiles = _optional_string_set(
            raw.get("allowed_profiles"),
            label=f"{source}: {capability}.allowed_profiles",
        )
        allowed_providers = _optional_string_set(
            raw.get("allowed_providers"),
            label=f"{source}: {capability}.allowed_providers",
        )
        if allowed_profiles is not None:
            profiles[capability] = allowed_profiles
        if allowed_providers is not None:
            providers[capability] = allowed_providers
    return profiles, providers


def _required_skills(value: object, *, source: str) -> dict[str, tuple[str, ...]]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise PolicyError(f"{source}: skills.required must be a mapping")
    result: dict[str, tuple[str, ...]] = {}
    for capability_name, names in value.items():
        capability = str(capability_name)
        if capability not in _CAPABILITIES:
            raise PolicyError(f"{source}: unknown capability '{capability_name}'")
        parsed = _optional_string_set(names, label=f"{source}: required skills for {capability}")
        assert parsed is not None
        result[capability] = tuple(sorted(parsed))
    return result


def _reject_unknown(mapping: dict[str, Any], allowed: set[str], *, label: str) -> None:
    unknown = sorted(set(mapping) - allowed)
    if unknown:
        raise PolicyError(f"{label} contains unsupported key(s): {', '.join(unknown)}")


def _load_layer(path: Path, source: str) -> PolicyLayer:
    data = load_yaml(path)
    if data.get("version", 1) != 1:
        raise PolicyError(f"{source}: policy version must be 1")
    _reject_unknown(
        data,
        {"version", "providers", "capabilities", "execution", "skills"},
        label=source,
    )
    providers = data.get("providers") or {}
    execution = data.get("execution") or {}
    skills = data.get("skills") or {}
    if not isinstance(providers, dict) or not isinstance(execution, dict) or not isinstance(skills, dict):
        raise PolicyError(f"{source}: providers, execution, and skills must be mappings")
    _reject_unknown(
        providers,
        {"allowed_profiles", "allowed_providers", "allowed_models"},
        label=f"{source}: providers",
    )
    _reject_unknown(
        execution,
        {
            "workspace_write",
            "require_prior_approval_for_workspace_write",
            "allow_force_approval_bypass",
            "protected_paths",
            "environment_allowlist",
        },
        label=f"{source}: execution",
    )
    _reject_unknown(skills, {"required"}, label=f"{source}: skills")

    capability_profiles, capability_providers = _capability_rules(
        data.get("capabilities"), source=source
    )

    protected = execution.get("protected_paths") or []
    if not isinstance(protected, list) or not all(isinstance(item, str) and item.strip() for item in protected):
        raise PolicyError(f"{source}: execution.protected_paths must be a string list")
    for pattern in protected:
        candidate = Path(pattern.strip())
        if candidate.is_absolute() or ".." in candidate.parts:
            raise PolicyError(
                f"{source}: protected path patterns must be relative to the repository"
            )

    return PolicyLayer(
        source=source,
        allowed_profiles=_optional_string_set(
            providers.get("allowed_profiles"), label=f"{source}: providers.allowed_profiles"
        ),
        allowed_providers=_optional_string_set(
            providers.get("allowed_providers"), label=f"{source}: providers.allowed_providers"
        ),
        allowed_models=_model_rules(providers.get("allowed_models"), source=source),
        capability_profiles=capability_profiles,
        capability_providers=capability_providers,
        workspace_write=_optional_bool(
            execution, "workspace_write", label=f"{source}: execution.workspace_write"
        ),
        require_prior_approval_for_workspace_write=_optional_bool(
            execution,
            "require_prior_approval_for_workspace_write",
            label=f"{source}: execution.require_prior_approval_for_workspace_write",
        ),
        allow_force_approval_bypass=_optional_bool(
            execution,
            "allow_force_approval_bypass",
            label=f"{source}: execution.allow_force_approval_bypass",
        ),
        protected_paths=tuple(item.strip() for item in protected),
        environment_allowlist=_optional_string_set(
            execution.get("environment_allowlist"),
            label=f"{source}: execution.environment_allowlist",
        ),
        required_skills=_required_skills(skills.get("required"), source=source),
    )


def _intersect(values: list[frozenset[str] | None]) -> frozenset[str] | None:
    constrained = [value for value in values if value is not None]
    if not constrained:
        return None
    result = set(constrained[0])
    for value in constrained[1:]:
        result.intersection_update(value)
    return frozenset(result)


def _merge_keyed_sets(layers: list[PolicyLayer], attr: str) -> dict[Any, frozenset[str]]:
    keys: set[Any] = set()
    for layer in layers:
        keys.update((getattr(layer, attr) or {}).keys())
    result: dict[Any, frozenset[str]] = {}
    for key in keys:
        values = [
            mapping[key]
            for layer in layers
            if (mapping := getattr(layer, attr) or {}) and key in mapping
        ]
        merged = _intersect(values)
        if merged is not None:
            result[key] = merged
    return result


def _merge_required_skills(layers: list[PolicyLayer]) -> dict[str, tuple[str, ...]]:
    result: dict[str, list[str]] = {}
    for layer in layers:
        for capability, names in (layer.required_skills or {}).items():
            bucket = result.setdefault(capability, [])
            for name in names:
                if name not in bucket:
                    bucket.append(name)
    return {key: tuple(value) for key, value in result.items()}


def _external_policy_path(value: str, *, label: str, project_root: Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise PolicyError(f"{label} must be an absolute path")
    resolved = path.resolve()
    if not resolved.is_file():
        raise PolicyError(f"{label} does not exist or is not a file: {resolved}")
    try:
        resolved.relative_to(project_root.resolve())
    except ValueError:
        return resolved
    raise PolicyError(f"{label} must be managed outside the project repository")


def load_effective_configuration(
    project_root: Path,
    *,
    environ: Mapping[str, str] | None = None,
) -> EffectiveConfiguration:
    project_root = project_root.resolve()
    env = dict(os.environ if environ is None else environ)

    config_path = ensure_within_project(
        project_root, project_root / ".sdai" / "config.yaml", label="SD-AI config path"
    )
    config = load_yaml(config_path)
    mode_value = env.get("SDAI_OPERATING_MODE", "").strip() or str(
        config.get("operating_mode") or "individual"
    )
    try:
        configured_mode = OperatingMode(mode_value)
    except ValueError as exc:
        raise PolicyError(
            "operating_mode/SDAI_OPERATING_MODE must be 'individual' or 'enterprise'"
        ) from exc

    policy_config = config.get("policy") or {}
    if not isinstance(policy_config, dict):
        raise PolicyError("config.yaml policy must be a mapping")

    # Organization policy discovery is intentionally not repo-configurable. A repo
    # cannot redirect the company-managed environment variable to bypass enterprise policy.
    org_env_name = "SDAI_ORG_POLICY_PATH"
    user_env_name = "SDAI_USER_POLICY_PATH"
    repo_relative = str(policy_config.get("repository") or ".sdai/policy.yaml")

    layers: list[PolicyLayer] = []
    org_value = env.get(org_env_name, "").strip()
    if org_value:
        org_path = _external_policy_path(
            org_value, label=org_env_name, project_root=project_root
        )
        layers.append(_load_layer(org_path, f"organization:{org_path}"))
    elif configured_mode == OperatingMode.ENTERPRISE:
        raise PolicyError(
            f"Enterprise mode requires a company-managed organization policy via {org_env_name}"
        )

    repo_candidate = Path(repo_relative)
    if repo_candidate.is_absolute() or ".." in repo_candidate.parts:
        raise PolicyError("policy.repository must be a relative path inside the project")
    repo_path = ensure_within_project(
        project_root, project_root / repo_candidate, label="repository policy path"
    )
    if repo_path.exists():
        layers.append(_load_layer(repo_path, f"repository:{repo_candidate.as_posix()}"))

    user_value = env.get(user_env_name, "").strip()
    if user_value:
        user_path = Path(user_value).expanduser().resolve()
        if not user_path.is_file():
            raise PolicyError(f"{user_env_name} does not exist or is not a file: {user_path}")
        layers.append(_load_layer(user_path, f"user:{user_path}"))

    effective_mode = OperatingMode.ENTERPRISE if org_value else configured_mode

    allowed_profiles = _intersect([layer.allowed_profiles for layer in layers])
    allowed_providers = _intersect([layer.allowed_providers for layer in layers])
    allowed_models = _merge_keyed_sets(layers, "allowed_models")
    capability_profiles = _merge_keyed_sets(layers, "capability_profiles")
    capability_providers = _merge_keyed_sets(layers, "capability_providers")

    workspace_write = all(layer.workspace_write is not False for layer in layers)
    require_approval = any(
        layer.require_prior_approval_for_workspace_write is True for layer in layers
    )
    allow_force_bypass = all(
        layer.allow_force_approval_bypass is not False for layer in layers
    )

    protected: list[str] = list(CORE_PROTECTED_PATHS)
    for layer in layers:
        for pattern in layer.protected_paths:
            if pattern not in protected:
                protected.append(pattern)

    environment_allowlist = _intersect(
        [layer.environment_allowlist for layer in layers]
    )
    # Enterprise subprocesses fail closed: provider credential/environment variables
    # must be named by company policy. Native CLI credential stores remain available.
    if effective_mode == OperatingMode.ENTERPRISE and environment_allowlist is None:
        environment_allowlist = frozenset()

    return EffectiveConfiguration(
        operating_mode=effective_mode,
        sources=tuple(layer.source for layer in layers),
        allowed_profiles=allowed_profiles,
        allowed_providers=allowed_providers,
        allowed_models=allowed_models,
        capability_profiles=capability_profiles,
        capability_providers=capability_providers,
        workspace_write=workspace_write,
        require_prior_approval_for_workspace_write=require_approval,
        allow_force_approval_bypass=allow_force_bypass,
        protected_paths=tuple(protected),
        environment_allowlist=environment_allowlist,
        required_skills_map=_merge_required_skills(layers),
    )


def scaffold_repository_policy() -> str:
    return """version: 1
providers: {}
capabilities: {}
execution:
  workspace_write: true
  require_prior_approval_for_workspace_write: false
  allow_force_approval_bypass: true
  protected_paths: []
skills:
  required: {}
"""
