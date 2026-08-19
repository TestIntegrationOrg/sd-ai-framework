from __future__ import annotations

import os
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from queue import SimpleQueue
from typing import BinaryIO

from sdai.providers.base import Provider, ProviderCapabilities
from sdai.providers.control import (
    ProviderCancellationToken,
    ProviderCancelledError,
    ProviderProgressCallback,
    ProviderProgressEvent,
)


class ProviderExecutionError(RuntimeError):
    pass


class ProviderStartupError(ProviderExecutionError):
    """Raised when the provider process cannot be created safely."""

    def __init__(self, provider: str, reason_code: str) -> None:
        self.provider = provider
        self.reason_code = reason_code
        super().__init__(f"{provider} process startup failed ({reason_code})")


class ProviderEncodingError(ProviderExecutionError):
    """Raised when provider stdout/stderr is not strict UTF-8."""

    def __init__(self, provider: str, stream: str, offset: int, preview: str) -> None:
        self.provider = provider
        self.stream = stream
        self.offset = offset
        self.preview = preview
        super().__init__(
            f"{provider} returned invalid UTF-8 on {stream} at byte {offset}; "
            f"offending-byte preview: {preview}. Configure the provider to emit UTF-8."
        )


class ProviderOutputLimitError(ProviderExecutionError):
    """Raised after a stream was fully drained but exceeded its bounded capture."""

    def __init__(self, provider: str, stream: str, limit_bytes: int, observed_bytes: int) -> None:
        self.provider = provider
        self.stream = stream
        self.limit_bytes = limit_bytes
        self.observed_bytes = observed_bytes
        super().__init__(
            f"{provider} {stream} exceeded the configured capture limit "
            f"({observed_bytes}>{limit_bytes} bytes)"
        )


def _escaped_byte_preview(data: bytes, start: int, end: int) -> str:
    return "".join(
        chr(value) if 0x20 <= value <= 0x7E and value != 0x5C else f"\\x{value:02x}"
        for value in data[start:end]
    )


def _decode_provider_output(data: bytes, *, provider: str, stream: str) -> str:
    try:
        return data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        # Expose only the offending bytes. Adjacent provider output may contain
        # sensitive content and is not needed to diagnose the encoding boundary.
        preview = _escaped_byte_preview(data, exc.start, exc.end)
        raise ProviderEncodingError(provider, stream, exc.start, preview) from exc


def _text_preview(value: str, *, max_chars: int = 4_096) -> str:
    text = value.strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "...[truncated by SDAI]"


# These variables are required for normal process startup/runtime behavior and are
# not used for provider credential discovery or network routing. They remain present
# even when enterprise environment policy is fail-closed.
_PROCESS_ENVIRONMENT = (
    "PATH",
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
)

# These variables can influence provider credential discovery, user configuration,
# proxy routing, or trust roots. Treat them as policy-controlled rather than as an
# unconditional process baseline.
_POLICY_GATED_ENVIRONMENT = (
    "HOME",
    "USERPROFILE",
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
    """Build a minimal, policy-bounded provider subprocess environment.

    Individual mode preserves normal provider credential/config discovery when no
    effective environment restriction is configured. Once policy supplies an allowlist
    (including an empty enterprise fail-closed allowlist), credential discovery,
    network/trust configuration, provider authentication variables, and profile-requested
    variables are restricted to that allowlist. Only the process-runtime baseline is
    unconditional.
    """
    requested = (
        set(_POLICY_GATED_ENVIRONMENT)
        | set(_PROVIDER_AUTH_ENVIRONMENT.get(provider, ()))
        | set(profile_allowlist)
    )
    if policy_allowlist is not None:
        requested.intersection_update(policy_allowlist)
    names = set(_PROCESS_ENVIRONMENT) | requested
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
class _BoundedByteCapture:
    limit_bytes: int
    _data: bytearray = field(default_factory=bytearray, init=False, repr=False)
    _observed_bytes: int = field(default=0, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def append(self, chunk: bytes) -> None:
        if not chunk:
            return
        with self._lock:
            self._observed_bytes += len(chunk)
            remaining = max(0, self.limit_bytes - len(self._data))
            if remaining:
                self._data.extend(chunk[:remaining])

    def snapshot(self) -> tuple[bytes, int, bool]:
        with self._lock:
            return bytes(self._data), self._observed_bytes, self._observed_bytes > self.limit_bytes


def _drain_stream(
    stream: BinaryIO,
    capture: _BoundedByteCapture,
    *,
    chunk_bytes: int,
    first_output: threading.Event | None,
    errors: SimpleQueue[BaseException],
) -> None:
    try:
        while True:
            chunk = stream.read(chunk_bytes)
            if not chunk:
                break
            capture.append(chunk)
            if first_output is not None:
                first_output.set()
    except BaseException as exc:  # propagated by the monitoring thread after join
        errors.put(exc)
    finally:
        try:
            stream.close()
        except OSError:
            pass


def _write_stdin(
    stream: BinaryIO,
    payload: bytes,
    errors: SimpleQueue[BaseException],
) -> None:
    try:
        stream.write(payload)
        stream.flush()
    except BrokenPipeError:
        # The process may fail/exit before consuming all stdin. Its exit status and
        # stderr remain the authoritative execution outcome.
        pass
    except BaseException as exc:
        errors.put(exc)
    finally:
        try:
            stream.close()
        except OSError:
            pass


def _raise_thread_error(errors: SimpleQueue[BaseException], provider: str) -> None:
    if errors.empty():
        return
    error = errors.get()
    if isinstance(error, OSError):
        raise ProviderExecutionError(f"{provider} subprocess pipe I/O failed") from error
    raise error


@dataclass
class CliProvider(Provider):
    """Safe UTF-8/binary subprocess adapter for an external agent CLI.

    No shell is used. Arguments are passed directly to subprocess. A command may use
    ``{prompt}`` as an argument placeholder; otherwise the combined prompt is encoded
    explicitly as UTF-8 and sent on binary stdin. Stdout/stderr are continuously drained
    into bounded captures so large provider output cannot deadlock or consume unbounded
    memory. If no explicit environment is supplied, only the minimal process environment
    is inherited rather than the caller's full secret-bearing environment.
    """

    command: list[str]
    cwd: Path
    timeout_seconds: int = 600
    provider_name: str = "command"
    environment: dict[str, str] | None = None
    heartbeat_interval_seconds: float = 5.0
    poll_interval_seconds: float = 0.25
    max_stdout_bytes: int = 4 * 1024 * 1024
    max_stderr_bytes: int = 256 * 1024
    io_chunk_bytes: int = 64 * 1024
    _last_command: list[str] = field(default_factory=list, init=False, repr=False)

    def _combined_prompt(self, system: str, prompt: str) -> str:
        return f"SYSTEM\n{system.strip()}\n\nTASK\n{prompt.strip()}\n"

    def _build_command(self, payload: str) -> tuple[list[str], str | None]:
        has_placeholder = any("{prompt}" in value for value in self.command)
        command = [value.replace("{prompt}", payload) for value in self.command]
        return command, None if has_placeholder else payload

    def _validate_limits(self) -> None:
        if self.poll_interval_seconds <= 0 or self.heartbeat_interval_seconds <= 0:
            raise ProviderExecutionError("Provider poll/heartbeat intervals must be positive")
        if not 1 <= self.max_stdout_bytes <= 64 * 1024 * 1024:
            raise ProviderExecutionError("max_stdout_bytes must be between 1 and 67108864")
        if not 1 <= self.max_stderr_bytes <= 16 * 1024 * 1024:
            raise ProviderExecutionError("max_stderr_bytes must be between 1 and 16777216")
        if not 1 <= self.io_chunk_bytes <= 1024 * 1024:
            raise ProviderExecutionError("io_chunk_bytes must be between 1 and 1048576")

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
        self._validate_limits()
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
        try:
            process = subprocess.Popen(
                command,
                cwd=self.cwd,
                stdin=subprocess.PIPE if stdin_bytes is not None else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
                shell=False,
                bufsize=0,
                **popen_kwargs,
            )
        except FileNotFoundError as exc:
            raise ProviderStartupError(self.provider_name, "executable-not-found") from exc
        except PermissionError as exc:
            raise ProviderStartupError(self.provider_name, "permission-denied") from exc
        except OSError as exc:
            raise ProviderStartupError(self.provider_name, "process-start-failed") from exc

        assert process.stdout is not None
        assert process.stderr is not None
        stdout_capture = _BoundedByteCapture(self.max_stdout_bytes)
        stderr_capture = _BoundedByteCapture(self.max_stderr_bytes)
        first_output_signal = threading.Event()
        thread_errors: SimpleQueue[BaseException] = SimpleQueue()
        stdout_thread = threading.Thread(
            target=_drain_stream,
            args=(process.stdout, stdout_capture),
            kwargs={
                "chunk_bytes": self.io_chunk_bytes,
                "first_output": first_output_signal,
                "errors": thread_errors,
            },
            name=f"sdai-{self.provider_name}-stdout",
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=_drain_stream,
            args=(process.stderr, stderr_capture),
            kwargs={
                "chunk_bytes": self.io_chunk_bytes,
                "first_output": None,
                "errors": thread_errors,
            },
            name=f"sdai-{self.provider_name}-stderr",
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()
        stdin_thread: threading.Thread | None = None
        if stdin_bytes is not None:
            assert process.stdin is not None
            stdin_thread = threading.Thread(
                target=_write_stdin,
                args=(process.stdin, stdin_bytes, thread_errors),
                name=f"sdai-{self.provider_name}-stdin",
                daemon=True,
            )
            stdin_thread.start()

        started = time.monotonic()
        next_heartbeat = started + self.heartbeat_interval_seconds
        first_output_reported = False
        timeout_error: subprocess.TimeoutExpired | None = None
        cancelled = False
        try:
            while process.poll() is None:
                if not thread_errors.empty():
                    _terminate_process(process)
                    break
                if first_output_signal.is_set() and not first_output_reported:
                    progress(ProviderProgressEvent("first-output", "stdout-observed"))
                    first_output_reported = True
                if cancellation.cancelled:
                    cancelled = True
                    _terminate_process(process)
                    break
                now = time.monotonic()
                elapsed = now - started
                if elapsed >= self.timeout_seconds:
                    timeout_error = subprocess.TimeoutExpired(command, self.timeout_seconds)
                    _terminate_process(process)
                    break
                if now >= next_heartbeat:
                    progress(ProviderProgressEvent("heartbeat", "subprocess-running"))
                    while next_heartbeat <= now:
                        next_heartbeat += self.heartbeat_interval_seconds
                time.sleep(min(self.poll_interval_seconds, max(0.001, self.timeout_seconds - elapsed)))
        except BaseException:
            if process.poll() is None:
                _terminate_process(process)
            raise
        finally:
            try:
                process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                _terminate_process(process, grace_seconds=0.25)
            for thread in (stdin_thread, stdout_thread, stderr_thread):
                if thread is not None:
                    thread.join(timeout=2.0)

        if stdout_thread.is_alive() or stderr_thread.is_alive() or (
            stdin_thread is not None and stdin_thread.is_alive()
        ):
            raise ProviderExecutionError(f"{self.provider_name} subprocess pipe did not close cleanly")
        if first_output_signal.is_set() and not first_output_reported:
            progress(ProviderProgressEvent("first-output", "stdout-observed"))
        # Cancellation/timeout are the primary terminal causes. Process termination may
        # cause a secondary writer/reader OSError on Windows; do not let that artifact
        # overwrite the requested cancellation or configured timeout outcome.
        if cancelled:
            raise ProviderCancelledError()
        if timeout_error is not None:
            raise timeout_error
        _raise_thread_error(thread_errors, self.provider_name)

        stdout_bytes, stdout_observed, stdout_truncated = stdout_capture.snapshot()
        stderr_bytes, stderr_observed, stderr_truncated = stderr_capture.snapshot()
        if stdout_truncated:
            raise ProviderOutputLimitError(
                self.provider_name,
                "stdout",
                self.max_stdout_bytes,
                stdout_observed,
            )
        if stderr_truncated:
            raise ProviderOutputLimitError(
                self.provider_name,
                "stderr",
                self.max_stderr_bytes,
                stderr_observed,
            )
        stdout = _decode_provider_output(
            stdout_bytes, provider=self.provider_name, stream="stdout"
        )
        stderr = _decode_provider_output(
            stderr_bytes, provider=self.provider_name, stream="stderr"
        )
        if process.returncode != 0:
            raise ProviderExecutionError(
                f"{self.provider_name} failed with exit code {process.returncode}: "
                f"{_text_preview(stderr)}"
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
