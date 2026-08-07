from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sdai.providers.cli import CliProvider


@dataclass
class CommandProvider(CliProvider):
    """Backward-compatible alias for a custom command-based provider."""

    command: list[str]
    cwd: Path = Path(".")
    timeout_seconds: int = 300
    provider_name: str = "command"
