from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from sdai.providers.base import Provider


class ProviderExecutionError(RuntimeError):
    pass


_BASE_ENVIRONMENT = (
    "PATH",
    "HOME",
    "USERPROFILE",
    "TMP",
    "TEMP",
    "TMPDIR",
    "LANG",
    "LC_ALL",
    "TERM",
    "COMSPEC",
    "SYSTEMROOT",
    "WINDIR",
    "PATHEXT",
    "APPDATA",
    "LOCALAPPDATA",
    "XDG_CONFIG_HOME",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "NO_PROXY",
    "SSL_CERT_FILE",
    "REQUESTS_CA_BUNDLE",
)

_PROVIDER_AUTH_ENVIRONMENT: dict[str, tuple[str, ...]] = {
    "codex": ("OPENAI_API_KEY", "OPENAI_BASE_URL", "CODEX_HOME"),
    "copilot": ("GH_TOKEN", "GITHUB_TOKEN", "GH_HOST"),
    "claude": ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "CLAUDE_CONFIG_DIR"),
    "gemini": (
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GOOGLE_CLOUD_PROJECT",
    ),
}


def build_provider_environment(
    provider: str,
    *,
    profile_allowlist: tuple[str, ...] = (),
    policy_allowlist: frozenset[str] | None = None,
) -> dict[str, str]:
    """Build a minimal subprocess environment instead of inheriting all employee secrets."""
    base = set(_BASE_ENVIRONMENT)
    requested = set(_PROVIDER_AUTH_ENVIRONMENT.get(provider, ())) | set(profile_allowlist)
    if policy_allowlist is not None:
        requested.intersection_update(policy_allowlist)
    names = base | requested
    return {name: value for name in names if (value := os.environ.get(name)) is not None}


@dataclass
class CliProvider(Provider):
    """Safe subprocess adapter for an external agent CLI.

    No shell is used. Arguments are passed directly to subprocess. A command may use
    ``{prompt}`` as an argument placeholder; otherwise the combined prompt is sent on
    stdin. If no explicit environment is supplied, only the minimal process environment
    is inherited rather than the caller's full secret-bearing environment.
    """

    command: list[str]
    cwd: Path
    timeout_seconds: int = 600
    provider_name: str = "command"
    environment: dict[str, str] | None = None
    _last_command: list[str] = field(default_factory=list, init=False, repr=False)

    def _combined_prompt(self, system: str, prompt: str) -> str:
        return f"SYSTEM\n{system.strip()}\n\nTASK\n{prompt.strip()}\n"

    def _build_command(self, payload: str) -> tuple[list[str], str | None]:
        has_placeholder = any("{prompt}" in value for value in self.command)
        command = [value.replace("{prompt}", payload) for value in self.command]
        return command, None if has_placeholder else payload

    def complete(self, *, system: str, prompt: str) -> str:
        if not self.command:
            raise ProviderExecutionError("Provider command is empty")
        payload = self._combined_prompt(system, prompt)
        command, stdin_payload = self._build_command(payload)
        self._last_command = command
        environment = self.environment
        if environment is None:
            environment = build_provider_environment(self.provider_name)
        result = subprocess.run(
            command,
            cwd=self.cwd,
            input=stdin_payload,
            capture_output=True,
            text=True,
            timeout=self.timeout_seconds,
            check=False,
            env=environment,
        )
        if result.returncode != 0:
            stderr = result.stderr.strip()
            raise ProviderExecutionError(
                f"{self.provider_name} failed with exit code {result.returncode}: {stderr}"
            )
        output = result.stdout.strip()
        if not output:
            raise ProviderExecutionError(f"{self.provider_name} returned no output")
        return output

    def availability(self) -> tuple[bool, str]:
        executable = self.command[0] if self.command else ""
        if not executable:
            return False, "no executable configured"
        resolved = shutil.which(executable)
        if resolved:
            return True, resolved
        return False, f"'{executable}' not found on PATH"
