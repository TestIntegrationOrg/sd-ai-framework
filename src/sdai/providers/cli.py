from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from sdai.providers.base import Provider


class ProviderExecutionError(RuntimeError):
    pass


def _escaped_byte_preview(data: bytes, start: int, end: int) -> str:
    return "".join(
        chr(value) if 0x20 <= value <= 0x7E and value != 0x5C else f"\\x{value:02x}"
        for value in data[start:end]
    )


def _decode_provider_output(data: bytes, *, provider: str, stream: str) -> str:
    try:
        return data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        preview = _escaped_byte_preview(data, exc.start, exc.end)
        raise ProviderExecutionError(
            f"{provider} returned invalid UTF-8 on {stream} at byte {exc.start}; "
            f"offending-byte preview: {preview}. Configure the provider to emit UTF-8."
        ) from exc


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
    "codex": ("OPENAI_API_KEY", "CODEX_HOME"),
    "copilot": ("GH_TOKEN", "GITHUB_TOKEN", "GH_HOST"),
    "claude": ("ANTHROPIC_API_KEY", "CLAUDE_CONFIG_DIR"),
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
        stdin_bytes = (
            stdin_payload.encode("utf-8", errors="strict")
            if stdin_payload is not None
            else None
        )
        result = subprocess.run(
            command,
            cwd=self.cwd,
            input=stdin_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=self.timeout_seconds,
            check=False,
            env=environment,
        )
        stdout = _decode_provider_output(
            result.stdout or b"", provider=self.provider_name, stream="stdout"
        )
        stderr = _decode_provider_output(
            result.stderr or b"", provider=self.provider_name, stream="stderr"
        )
        if result.returncode != 0:
            raise ProviderExecutionError(
                f"{self.provider_name} failed with exit code {result.returncode}: "
                f"{stderr.strip()}"
            )
        # Remove only a leading UTF-8 BOM before the existing whitespace normalization.
        output = stdout.removeprefix("\ufeff").strip()
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
