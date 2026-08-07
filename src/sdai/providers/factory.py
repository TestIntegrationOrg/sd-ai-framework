from __future__ import annotations

from importlib.metadata import entry_points
from pathlib import Path
from typing import Callable

from sdai.agent_platform.models import AgentProfile, ExecutionMode
from sdai.providers.base import Provider
from sdai.providers.cli import CliProvider
from sdai.providers.named import claude_provider, codex_provider, copilot_provider, gemini_provider


class ProviderFactoryError(RuntimeError):
    pass


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


class ProviderFactory:
    @staticmethod
    def create(profile: AgentProfile, *, mode: ExecutionMode, cwd: Path) -> Provider:
        effective_args = profile.extra_args
        if mode == ExecutionMode.WORKSPACE_WRITE:
            effective_args = effective_args + profile.workspace_write_args

        common = dict(
            cwd=cwd,
            model=profile.model,
            timeout_seconds=profile.timeout_seconds,
            extra_args=effective_args,
            mode=mode,
        )
        if profile.provider == "codex":
            return codex_provider(**common)
        if profile.provider == "copilot":
            return copilot_provider(**common)
        if profile.provider == "claude":
            return claude_provider(**common)
        if profile.provider == "gemini":
            return gemini_provider(**common)
        if profile.provider in {"command", "local", "custom"}:
            if not profile.command:
                raise ProviderFactoryError(
                    f"Profile '{profile.name}' uses provider '{profile.provider}' but has no command"
                )
            return CliProvider(
                list(profile.command) + list(effective_args),
                cwd=cwd,
                timeout_seconds=profile.timeout_seconds,
                provider_name=profile.provider,
            )

        plugin = _plugin_factory(profile.provider)
        if plugin:
            provider = plugin(profile=profile, mode=mode, cwd=cwd)
            if not isinstance(provider, Provider):
                raise ProviderFactoryError(
                    f"Provider plugin '{profile.provider}' did not return a Provider"
                )
            return provider
        raise ProviderFactoryError(f"Unsupported provider '{profile.provider}'")
