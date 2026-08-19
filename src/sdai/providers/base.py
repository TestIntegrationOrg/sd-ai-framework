from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderCapabilities:
    """Observable provider capabilities used by diagnostics/reliability layers.

    These flags describe execution mechanics only. They do not grant provider
    authority or change semantic agent routing/policy decisions.
    """

    streaming: bool = False
    heartbeat: bool = False
    cancellation: bool = False
    first_output_timing: bool = False

    def as_dict(self) -> dict[str, bool]:
        return {
            "streaming": self.streaming,
            "heartbeat": self.heartbeat,
            "cancellation": self.cancellation,
            "firstOutputTiming": self.first_output_timing,
        }


class Provider(ABC):
    """Execution boundary for LLM and coding-agent integrations."""

    @abstractmethod
    def complete(self, *, system: str, prompt: str) -> str:
        raise NotImplementedError

    def availability(self) -> tuple[bool, str]:
        return True, "available"

    def diagnostic_capabilities(self) -> ProviderCapabilities:
        """Return execution-observability capabilities without invoking the provider."""
        return ProviderCapabilities()


__all__ = ["Provider", "ProviderCapabilities"]
