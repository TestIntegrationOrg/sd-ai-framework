from __future__ import annotations

from importlib.metadata import entry_points
from pathlib import Path
from typing import Callable

from sdai.agent_platform.models import AgentProfile, ExecutionMode
from sdai.policy import EffectiveConfiguration, load_effective_configuration
from sdai.providers.base import Provider
from sdai.providers.cli import CliProvider, build_provider_environment
from sdai.providers.named import claude_provider, codex_provider, copilot_provider, gemini_provider


class ProviderFactoryError(RuntimeError):
    pass


_PRIVILEGE_ARG_PARTS = (
    "sandbox",
    "permission",
    "approval",
    "allow-tool",
    "allow_tool",
    "deny-tool",
    "deny_tool",
    "available-tools",
    "available_tools",
    "dangerously",
    "full-auto",
    "full_auto",
    "yolo",
    "auto-edit",
    "auto_edit",
    "mcp",
    "network",
    "shell",
)


def _plugin_factory(name: str) -> Callable[..., Provider] | None:
    try:
        matches = entry_points(group="sdai.providers")
    except TypeError:  # pragma: no cover - compatibility with older importlib metadata
        matches = entry_points().get("sdai.providers", [])
    for entry in matches:
        if entry.name == name:
            loaded = entry.load()
            if not callable(loaded):
                raise ProviderFactoryError(f"Provider plugin '{name}' is not callable")
            return loaded
    return None


def _validate_adapter_args(provider: str, args: tuple[str, ...]) -> None:
    if provider not in {"codex", "copilot", "claude", "gemini"}:
        return
    for value in args:
        lowered = value.lower().replace("_", "-")
        if any(part.replace("_", "-") in lowered for part in _PRIVILEGE_ARG_PARTS):
            raise ProviderFactoryError(
                f"Profile argument '{value}' may change {provider} permissions/sandbox behavior. "
                "Built-in adapter security flags are controlled by SD-AI, not profile extra_args."
            )


class ProviderFactory:
    @staticmethod
    def create(
        profile: AgentProfile,
        *,
        mode: ExecutionMode,
        cwd: Path,
        policy: EffectiveConfiguration | None = None,
    ) -> Provider:
        effective_policy = policy or load_effective_configuration(cwd)
        try:
            effective_policy.assert_base_profile_allowed(profile, mode)
        except RuntimeError as exc:
            raise ProviderFactoryError(str(exc)) from exc

        effective_args = profile.extra_args
        if mode == ExecutionMode.WORKSPACE_WRITE:
            effective_args = effective_args + profile.workspace_write_args
        _validate_adapter_args(profile.provider, effective_args)

        environment = build_provider_environment(
            profile.provider,
            profile_allowlist=profile.environment_allowlist,
            policy_allowlist=effective_policy.environment_allowlist,
        )

        common = dict(
            cwd=cwd,
            model=profile.model,
            timeout_seconds=profile.timeout_seconds,
            extra_args=effective_args,
            mode=mode,
        )
        if profile.provider == "codex":
            provider = codex_provider(**common)
        elif profile.provider == "copilot":
            provider = copilot_provider(**common)
        elif profile.provider == "claude":
            provider = claude_provider(**common)
        elif profile.provider == "gemini":
            provider = gemini_provider(**common)
        elif profile.provider in {"command", "local", "custom"}:
            if not profile.command:
                raise ProviderFactoryError(
                    f"Profile '{profile.name}' uses provider '{profile.provider}' but has no command"
                )
            return CliProvider(
                list(profile.command) + list(effective_args),
                cwd=cwd,
                timeout_seconds=profile.timeout_seconds,
                provider_name=profile.provider,
                environment=environment,
            )
        else:
            plugin = _plugin_factory(profile.provider)
            if plugin:
                provider = plugin(profile=profile, mode=mode, cwd=cwd)
                if not isinstance(provider, Provider):
                    raise ProviderFactoryError(
                        f"Provider plugin '{profile.provider}' did not return a Provider"
                    )
                return provider
            raise ProviderFactoryError(f"Unsupported provider '{profile.provider}'")

        if isinstance(provider, CliProvider):
            provider.environment = environment
        return provider
