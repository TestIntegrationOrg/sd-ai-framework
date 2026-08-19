from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from sdai.providers.base import (
    CancellationToken,
    Provider,
    ProviderCancelledError,
    ProviderCapabilities,
    ProviderProgress,
    ProviderProgressObserver,
)


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


def _terminate_process(
    process: subprocess.Popen[bytes],
    *,
    grace_seconds: float,
) -> tuple[bytes, bytes]:
    if process.poll() is None:
        process.terminate()
        try:
            return process.communicate(timeout=grace_seconds)
        except subprocess.TimeoutExpired:
            process.kill()
    return process.communicate()


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
    heartbeat_seconds: float = 1.0
    termination_grace_seconds: float = 2.0
    _last_command: list[str] = field(default_factory=list, init=False, repr=False)

    def _combined_prompt(self, system: str, prompt: str) -> str:
        return f"SYSTEM\n{system.strip()}\n\nTASK\n{prompt.strip()}\n"

    def _build_command(self, payload: str) -> tuple[list[str], str | None]:
        has_placeholder = any("{prompt}" in value for value in self.command)
        command = [value.replace("{prompt}", payload) for value in self.command]
        return command, None if has_placeholder else payload

    def _environment(self) -> dict[str, str]:
        if self.environment is not None:
            return self.environment
        return build_provider_environment(self.provider_name)

    def _result(self, *, stdout_bytes: bytes, stderr_bytes: bytes, returncode: int) -> str:
        stdout = _decode_provider_output(
            stdout_bytes, provider=self.provider_name, stream="stdout"
        )
        stderr = _decode_provider_output(
            stderr_bytes, provider=self.provider_name, stream="stderr"
        )
        if returncode != 0:
            raise ProviderExecutionError(
                f"{self.provider_name} failed with exit code {returncode}: "
                f"{stderr.strip()}"
            )
        output = stdout.removeprefix("\ufeff").strip()
        if not output:
            raise ProviderExecutionError(f"{self.provider_name} returned no output")
        return output

    def complete(self, *, system: str, prompt: str) -> str:
        if not self.command:
            raise ProviderExecutionError("Provider command is empty")
        payload = self._combined_prompt(system, prompt)
        command, stdin_payload = self._build_command(payload)
        self._last_command = command
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
            env=self._environment(),
        )
        return self._result(
            stdout_bytes=result.stdout or b"",
            stderr_bytes=result.stderr or b"",
            returncode=result.returncode,
        )

    def diagnostic_capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            streaming=False,
            heartbeat=True,
            cancellation=True,
            first_output_timing=False,
        )

    def complete_observed(
        self,
        *,
        system: str,
        prompt: str,
        cancellation: CancellationToken | None = None,
        observer: ProviderProgressObserver | None = None,
    ) -> str:
        if not self.command:
            raise ProviderExecutionError("Provider command is empty")
        if self.heartbeat_seconds <= 0:
            raise ValueError("heartbeat_seconds must be greater than zero")
        if self.termination_grace_seconds <= 0:
            raise ValueError("termination_grace_seconds must be greater than zero")
        if cancellation is not None:
            cancellation.raise_if_cancelled()

        payload = self._combined_prompt(system, prompt)
        command, stdin_payload = self._build_command(payload)
        self._last_command = command
        stdin_bytes = (
            stdin_payload.encode("utf-8", errors="strict")
            if stdin_payload is not None
            else None
        )
        process = subprocess.Popen(
            command,
            cwd=self.cwd,
            stdin=subprocess.PIPE if stdin_bytes is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self._environment(),
        )
        started = time.monotonic()
        first_communicate = True
        try:
            while True:
                if cancellation is not None and cancellation.cancelled:
                    _terminate_process(
                        process,
                        grace_seconds=self.termination_grace_seconds,
                    )
                    raise ProviderCancelledError("provider execution cancelled")

                elapsed = time.monotonic() - started
                remaining = float(self.timeout_seconds) - elapsed
                if remaining <= 0:
                    _terminate_process(
                        process,
                        grace_seconds=self.termination_grace_seconds,
                    )
                    raise subprocess.TimeoutExpired(command, self.timeout_seconds)
                wait = min(self.heartbeat_seconds, remaining)
                try:
                    stdout_bytes, stderr_bytes = process.communicate(
                        input=stdin_bytes if first_communicate else None,
                        timeout=wait,
                    )
                    break
                except subprocess.TimeoutExpired:
                    first_communicate = False
                    if cancellation is not None and cancellation.cancelled:
                        _terminate_process(
                            process,
                            grace_seconds=self.termination_grace_seconds,
                        )
                        raise ProviderCancelledError("provider execution cancelled")
                    if observer is not None:
                        try:
                            observer(ProviderProgress("heartbeat"))
                        except BaseException:
                            _terminate_process(
                                process,
                                grace_seconds=self.termination_grace_seconds,
                            )
                            raise
        except BaseException:
            if process.poll() is None:
                _terminate_process(
                    process,
                    grace_seconds=self.termination_grace_seconds,
                )
            raise

        if cancellation is not None:
            cancellation.raise_if_cancelled()
        return self._result(
            stdout_bytes=stdout_bytes or b"",
            stderr_bytes=stderr_bytes or b"",
            returncode=process.returncode,
        )

    def availability(self) -> tuple[bool, str]:
        executable = self.command[0] if self.command else ""
        if not executable:
            return False, "no executable configured"
        resolved = shutil.which(executable)
        if resolved:
            return True, resolved
        return False, f"'{executable}' not found on PATH"
