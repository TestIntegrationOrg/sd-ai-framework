from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from sdai.providers.base import Provider


class ProviderExecutionError(RuntimeError):
    pass


@dataclass
class CliProvider(Provider):
    """Safe subprocess adapter for an external agent CLI.

    No shell is used. Arguments are passed directly to subprocess. A command may use
    ``{prompt}`` as an argument placeholder; otherwise the combined prompt is sent on
    stdin and stdin is closed when the child process starts.
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
        result = subprocess.run(
            command,
            cwd=self.cwd,
            input=stdin_payload,
            capture_output=True,
            text=True,
            timeout=self.timeout_seconds,
            check=False,
            env=self.environment,
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
