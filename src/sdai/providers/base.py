from __future__ import annotations

from abc import ABC, abstractmethod


class Provider(ABC):
    """Provider boundary for future LLM/coding-agent integrations."""

    @abstractmethod
    def complete(self, *, system: str, prompt: str) -> str:
        raise NotImplementedError
