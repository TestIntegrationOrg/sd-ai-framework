from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from sdai.providers.base import Provider, ProviderCapabilities
from sdai.providers.control import (
    ProviderCancellationToken,
    ProviderCancelledError,
    ProviderProgressCallback,
    ProviderProgressEvent,
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


def _terminate_process(process: subprocess.Popen[bytes], *, grace_seconds: float = 1.0) -> None:
    """Terminate one provider process boundary without shell interpolation."""
    if process.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
        process.wait(timeout=grace_seconds)
        return
    except (ProcessLookupError, OSError, subprocess.TimeoutExpired):
        pass
    if process.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except (ProcessLookupError, OSError):
        pass
    try:
        process.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        pass


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
    heartbeat_interval_seconds: float = 5.0
    poll_interval_seconds: float = 0.25
    _last_command: list[str] = field(default_factory=list, init=False, repr=False)

    def _combined_prompt(self, system: str, prompt: str) -> str:
        return f"SYSTEM\n{system.strip()}\n\nTASK\n{prompt.strip()}\n"

    def _build_command(self, payload: str) -> tuple[list[str], str | None]:
        has_placeholder = any("{prompt}" in value for value in self.command)
        command = [value.replace("{prompt}", payload) for value in self.command]
        return command, None if has_placeholder else payload

    def diagnostic_capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            streaming=False,
            heartbeat=True,
            cancellation=True,
            first_output_timing=True,
        )

    def complete(self, *, system: str, prompt: str) -> str:
        return self.complete_observable(
            system=system,
            prompt=prompt,
            cancellation=ProviderCancellationToken(),
            progress=lambda event: None,
        )

    def complete_observable(
        self,
        *,
        system: str,
        prompt: str,
        cancellation: ProviderCancellationToken,
        progress: ProviderProgressCallback,
    ) -> str:
        if not self.command:
            raise ProviderExecutionError("Provider command is empty")
        if self.poll_interval_seconds <= 0 or self.heartbeat_interval_seconds <= 0:
            raise ProviderExecutionError("Provider poll/heartbeat intervals must be positive")
        cancellation.raise_if_cancelled()
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
        popen_kwargs: dict[str, object] = {}
        if os.name == "posix":
            popen_kwargs["start_new_session"] = True
        elif hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        process = subprocess.Popen(
            command,
            cwd=self.cwd,
            stdin=subprocess.PIPE if stdin_bytes is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            **popen_kwargs,
        )
        started = time.monotonic()
        next_heartbeat = started + self.heartbeat_interval_seconds
        first_output_reported = False
        first_communicate = True
        stdout_bytes = b""
        stderr_bytes = b""
        try:
            while True:
                if cancellation.cancelled:
                    _terminate_process(process)
                    try:
                        process.communicate(timeout=1.0)
                    except subprocess.TimeoutExpired:
                        pass
                    raise ProviderCancelledError()
                elapsed = time.monotonic() - started
                if elapsed >= self.timeout_seconds:
                    _terminate_process(process)
                    try:
                        process.communicate(timeout=1.0)
                    except subprocess.TimeoutExpired:
                        pass
                    raise subprocess.TimeoutExpired(command, self.timeout_seconds)
                remaining = max(0.001, self.timeout_seconds - elapsed)
                wait_for = min(self.poll_interval_seconds, remaining)
                try:
                    stdout_bytes, stderr_bytes = process.communicate(
                        input=stdin_bytes if first_communicate else None,
                        timeout=wait_for,
                    )
                    if stdout_bytes and not first_output_reported:
                        progress(ProviderProgressEvent("first-output", "stdout-observed"))
                        first_output_reported = True
                    break
                except subprocess.TimeoutExpired as exc:
                    first_communicate = False
                    partial = exc.output or b""
                    if partial and not first_output_reported:
                        progress(ProviderProgressEvent("first-output", "stdout-observed"))
                        first_output_reported = True
                    now = time.monotonic()
                    if now >= next_heartbeat:
                        progress(ProviderProgressEvent("heartbeat", "subprocess-running"))
                        while next_heartbeat <= now:
                            next_heartbeat += self.heartbeat_interval_seconds
        except BaseException:
            if process.poll() is None:
                _terminate_process(process)
            raise

        stdout = _decode_provider_output(
            stdout_bytes or b"", provider=self.provider_name, stream="stdout"
        )
        stderr = _decode_provider_output(
            stderr_bytes or b"", provider=self.provider_name, stream="stderr"
        )
        if process.returncode != 0:
            raise ProviderExecutionError(
                f"{self.provider_name} failed with exit code {process.returncode}: "
                f"{stderr.strip()}"
            )
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
