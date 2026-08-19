from __future__ import annotations

from dataclasses import dataclass
from threading import Event
from typing import Callable, Literal, Protocol


class ProviderCancelledError(RuntimeError):
    """Raised when one governed provider attempt is cooperatively cancelled."""

    def __init__(self, reason: str = "cancelled-by-request") -> None:
        self.reason = reason
        super().__init__(reason)


class ProviderCancellationToken:
    """Thread-safe cooperative cancellation signal owned by the caller/runtime."""

    def __init__(self) -> None:
        self._event = Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise ProviderCancelledError()


@dataclass(frozen=True)
class ProviderProgressEvent:
    """Metadata-only provider progress signal; it never carries model output."""

    kind: Literal["first-output", "heartbeat"]
    reason: str

    def __post_init__(self) -> None:
        if self.kind not in {"first-output", "heartbeat"}:
            raise ValueError(f"unsupported provider progress kind: {self.kind}")
        if not self.reason or len(self.reason) > 128:
            raise ValueError("provider progress reason must contain 1..128 characters")
        if any(ord(ch) < 0x20 or ord(ch) > 0x7E for ch in self.reason):
            raise ValueError("provider progress reason must be printable ASCII")


ProviderProgressCallback = Callable[[ProviderProgressEvent], None]


class ObservableProvider(Protocol):
    def complete_observable(
        self,
        *,
        system: str,
        prompt: str,
        cancellation: ProviderCancellationToken,
        progress: ProviderProgressCallback,
    ) -> str: ...


__all__ = [
    "ObservableProvider",
    "ProviderCancellationToken",
    "ProviderCancelledError",
    "ProviderProgressCallback",
    "ProviderProgressEvent",
]
