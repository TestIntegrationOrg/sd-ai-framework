from __future__ import annotations

import subprocess
from dataclasses import dataclass

from sdai.providers.base import Provider


@dataclass
class CommandProvider(Provider):
    """Run any external agent CLI that accepts a prompt on stdin and returns text on stdout.

    This adapter intentionally avoids coupling SD-AI core to a specific model vendor.
    """

    command: list[str]
    timeout_seconds: int = 300

    def complete(self, *, system: str, prompt: str) -> str:
        payload = f"SYSTEM\n{system}\n\nUSER\n{prompt}\n"
        result = subprocess.run(
            self.command,
            input=payload,
            capture_output=True,
            text=True,
            timeout=self.timeout_seconds,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"Provider command failed with code {result.returncode}: {result.stderr.strip()}"
            )
        output = result.stdout.strip()
        if not output:
            raise RuntimeError("Provider command returned no output")
        return output
