from __future__ import annotations

from abc import ABC, abstractmethod


class Provider(ABC):
    """Execution boundary for LLM and coding-agent integrations."""

    @abstractmethod
    def complete(self, *, system: str, prompt: str) -> str:
        raise NotImplementedError

    def availability(self) -> tuple[bool, str]:
        return True, "available"
