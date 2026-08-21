from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, Protocol


class NestedExecutionSupport(StrEnum):
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"


class ReadinessState(StrEnum):
    READY = "ready"
    DEGRADED = "degraded"
    BLOCKED = "blocked"
    UNKNOWN = "unknown"


UsageMeasurement = Literal[
    "provider-reported",
    "locally-counted",
    "estimated",
    "unavailable",
]


@dataclass(frozen=True)
class ProviderUsage:
    """Truthful token usage for one provider attempt.

    Missing provider telemetry is represented by ``None`` and ``unavailable``;
    it is never silently converted to zero.
    """

    input_tokens: int | None = None
    cached_input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    total_tokens: int | None = None
    measurement: UsageMeasurement = "unavailable"
    complete: bool = False
    unavailable_reason: str | None = "provider-did-not-report-usage"

    def __post_init__(self) -> None:
        for name in (
            "input_tokens",
            "cached_input_tokens",
            "output_tokens",
            "reasoning_tokens",
            "total_tokens",
        ):
            value = getattr(self, name)
            if value is not None and (
                not isinstance(value, int) or isinstance(value, bool) or value < 0
            ):
                raise ValueError(f"{name} must be a non-negative integer or null")
        if self.measurement not in {
            "provider-reported",
            "locally-counted",
            "estimated",
            "unavailable",
        }:
            raise ValueError("unsupported usage measurement")
        if self.measurement == "unavailable" and not self.unavailable_reason:
            raise ValueError("unavailable usage requires unavailable_reason")

    @classmethod
    def unavailable(cls, reason: str = "provider-did-not-report-usage") -> "ProviderUsage":
        return cls(unavailable_reason=reason)

    def as_dict(self) -> dict[str, object]:
        return {
            "inputTokens": self.input_tokens,
            "cachedInputTokens": self.cached_input_tokens,
            "outputTokens": self.output_tokens,
            "reasoningTokens": self.reasoning_tokens,
            "totalTokens": self.total_tokens,
            "measurement": self.measurement,
            "complete": self.complete,
            "unavailableReason": self.unavailable_reason,
        }


@dataclass(frozen=True)
class ProviderResult:
    content: str
    usage: ProviderUsage = ProviderUsage()
    model: str | None = None
    request_id: str | None = None
    finish_reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.content, str) or not self.content.strip():
            raise ValueError("provider result content must be non-empty text")


def normalize_provider_result(value: str | ProviderResult) -> ProviderResult:
    if isinstance(value, ProviderResult):
        return value
    if isinstance(value, str):
        return ProviderResult(value, ProviderUsage.unavailable("legacy-string-provider"))
    raise TypeError("provider must return str or ProviderResult")


@dataclass(frozen=True)
class ProviderReadiness:
    state: ReadinessState
    reason_code: str
    detail: str
    provider: str | None = None
    profile: str | None = None
    nested_execution: NestedExecutionSupport = NestedExecutionSupport.UNKNOWN
    max_nested_depth: int | None = None
    host_reused: bool = False

    @property
    def runnable(self) -> bool:
        return self.state in {ReadinessState.READY, ReadinessState.DEGRADED}

    def as_dict(self) -> dict[str, object]:
        return {
            "state": self.state.value,
            "reasonCode": self.reason_code,
            "detail": self.detail,
            "provider": self.provider,
            "profile": self.profile,
            "nestedExecution": self.nested_execution.value,
            "maxNestedDepth": self.max_nested_depth,
            "hostReused": self.host_reused,
        }


@dataclass(frozen=True)
class HostProviderContext:
    provider: str
    profile: str | None
    model: str | None
    capabilities: frozenset[str]
    execution_modes: frozenset[str]
    invocation_id: str
    invocation_chain: tuple[str, ...] = ()
    max_nested_depth: int | None = None


class HostProviderBridge(Protocol):
    context: HostProviderContext

    def complete(self, *, system: str, prompt: str) -> str | ProviderResult: ...


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
    nested_execution: NestedExecutionSupport = NestedExecutionSupport.UNKNOWN
    usage_reporting: bool = False
    max_nested_depth: int | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "streaming": self.streaming,
            "heartbeat": self.heartbeat,
            "cancellation": self.cancellation,
            "firstOutputTiming": self.first_output_timing,
        }


class Provider(ABC):
    """Execution boundary for LLM and coding-agent integrations."""

    @abstractmethod
    def complete(self, *, system: str, prompt: str) -> str | ProviderResult:
        raise NotImplementedError

    def availability(self) -> tuple[bool, str]:
        return True, "available"

    def readiness(self) -> ProviderReadiness:
        available, detail = self.availability()
        capabilities = self.diagnostic_capabilities()
        return ProviderReadiness(
            ReadinessState.READY if available else ReadinessState.BLOCKED,
            "provider-ready" if available else "provider-unavailable",
            detail,
            nested_execution=capabilities.nested_execution,
            max_nested_depth=capabilities.max_nested_depth,
        )

    def diagnostic_capabilities(self) -> ProviderCapabilities:
        """Return execution-observability capabilities without invoking the provider."""
        return ProviderCapabilities()


__all__ = [
    "HostProviderBridge",
    "HostProviderContext",
    "NestedExecutionSupport",
    "Provider",
    "ProviderCapabilities",
    "ProviderReadiness",
    "ProviderResult",
    "ProviderUsage",
    "ReadinessState",
    "normalize_provider_result",
]
