from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
import threading
from typing import Callable


class ProviderCancelledError(RuntimeError):
    """Raised when a governed provider attempt is cooperatively cancelled."""


class CancellationToken:
    """Thread-safe cooperative cancellation signal shared with provider adapters."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise ProviderCancelledError("provider execution cancelled")


@dataclass(frozen=True)
class ProviderProgress:
    """Privacy-safe progress notification; never carries raw provider output."""

    kind: str


ProviderProgressObserver = Callable[[ProviderProgress], None]


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

    def complete_observed(
        self,
        *,
        system: str,
        prompt: str,
        cancellation: CancellationToken | None = None,
        observer: ProviderProgressObserver | None = None,
    ) -> str:
        """Compatibility-safe observed execution path.

        Existing providers need not implement it. The default performs only
        pre/post cancellation checks around the historical ``complete`` call and
        emits no fabricated progress events.
        """
        del observer
        if cancellation is not None:
            cancellation.raise_if_cancelled()
        output = self.complete(system=system, prompt=prompt)
        if cancellation is not None:
            cancellation.raise_if_cancelled()
        return output

    def availability(self) -> tuple[bool, str]:
        return True, "available"

    def diagnostic_capabilities(self) -> ProviderCapabilities:
        """Return execution-observability capabilities without invoking the provider."""
        return ProviderCapabilities()


__all__ = [
    "CancellationToken",
    "Provider",
    "ProviderCancelledError",
    "ProviderCapabilities",
    "ProviderProgress",
    "ProviderProgressObserver",
]
