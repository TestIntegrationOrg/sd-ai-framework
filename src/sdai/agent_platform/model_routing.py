from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from sdai.agent_platform.models import AgentProfile, Capability, ExecutionMode
from sdai.agent_platform.profiles import load_profiles, load_routes
from sdai.policy import PolicyError, load_effective_configuration


MODEL_ROUTING_API_VERSION = "sdai.model-routing/v1"


class ModelRoutingError(RuntimeError):
    pass


_RISKS = frozenset({"trivial", "standard", "critical", "regulated"})
_COMPLEXITIES = frozenset({"low", "medium", "high", "extreme"})
_COST_RANK = {"economy": 0, "standard": 1, "premium": 2}
_FINAL_REVIEW_STAGES = frozenset({"final-review", "final-change-review"})


def _fail(code: str, message: str) -> ModelRoutingError:
    return ModelRoutingError(f"{code}: {message}")


def _canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise _fail("SDAI-ROUTING-001", f"routing value is not canonical JSON: {exc}") from exc


def _text(value: str | None, *, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise _fail("SDAI-ROUTING-001", f"{label} must be non-empty text or null")
    return value.strip()


@dataclass(frozen=True)
class RoutingRequest:
    semantic_role: str
    capability: Capability
    risk: str = "standard"
    complexity: str = "medium"
    affected_technologies: tuple[str, ...] = ()
    context_chars: int = 0
    max_cost_class: str = "premium"
    requested_profile: str | None = None
    requested_provider: str | None = None
    requested_model: str | None = None
    provider_availability: Mapping[str, bool] | None = None
    stage: str | None = None

    def __post_init__(self) -> None:
        semantic_role = _text(self.semantic_role, label="semantic_role")
        assert semantic_role is not None
        object.__setattr__(self, "semantic_role", semantic_role)
        try:
            capability = self.capability if isinstance(self.capability, Capability) else Capability(self.capability)
        except ValueError as exc:
            raise _fail("SDAI-ROUTING-001", f"unsupported capability: {self.capability!r}") from exc
        object.__setattr__(self, "capability", capability)
        risk = str(self.risk).strip().lower()
        complexity = str(self.complexity).strip().lower()
        cost = str(self.max_cost_class).strip().lower()
        if risk not in _RISKS:
            raise _fail("SDAI-ROUTING-001", f"unsupported risk: {self.risk!r}")
        if complexity not in _COMPLEXITIES:
            raise _fail("SDAI-ROUTING-001", f"unsupported complexity: {self.complexity!r}")
        if cost not in _COST_RANK:
            raise _fail("SDAI-ROUTING-001", f"unsupported max_cost_class: {self.max_cost_class!r}")
        object.__setattr__(self, "risk", risk)
        object.__setattr__(self, "complexity", complexity)
        object.__setattr__(self, "max_cost_class", cost)
        if not isinstance(self.context_chars, int) or isinstance(self.context_chars, bool) or self.context_chars < 0:
            raise _fail("SDAI-ROUTING-001", "context_chars must be a non-negative integer")
        technologies: list[str] = []
        for raw in self.affected_technologies:
            if not isinstance(raw, str) or not raw.strip() or "\x00" in raw:
                raise _fail("SDAI-ROUTING-001", "affected_technologies must contain non-empty text")
            value = raw.strip().casefold()
            if value not in technologies:
                technologies.append(value)
        object.__setattr__(self, "affected_technologies", tuple(sorted(technologies)))
        for field_name in ("requested_profile", "requested_provider", "requested_model", "stage"):
            object.__setattr__(self, field_name, _text(getattr(self, field_name), label=field_name))
        availability = self.provider_availability or {}
        if not isinstance(availability, Mapping):
            raise _fail("SDAI-ROUTING-001", "provider_availability must be a mapping")
        normalized: dict[str, bool] = {}
        for key, value in availability.items():
            if not isinstance(key, str) or not key.strip() or not isinstance(value, bool):
                raise _fail("SDAI-ROUTING-001", "provider_availability must map non-empty names to booleans")
            normalized[key.strip()] = value
        object.__setattr__(self, "provider_availability", MappingProxyType(dict(sorted(normalized.items()))))

    @property
    def has_explicit_request(self) -> bool:
        return any((self.requested_profile, self.requested_provider, self.requested_model))

    def as_dict(self) -> dict[str, object]:
        return {
            "semantic_role": self.semantic_role,
            "capability": self.capability.value,
            "risk": self.risk,
            "complexity": self.complexity,
            "affected_technologies": list(self.affected_technologies),
            "context_chars": self.context_chars,
            "max_cost_class": self.max_cost_class,
            "requested_profile": self.requested_profile,
            "requested_provider": self.requested_provider,
            "requested_model": self.requested_model,
            "provider_availability": dict(self.provider_availability or {}),
            "stage": self.stage,
        }


@dataclass(frozen=True)
class RoutingCandidate:
    profile: str
    provider: str
    model: str | None
    eligible: bool
    reasons: tuple[str, ...]
    rank: tuple[object, ...] | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "profile": self.profile,
            "provider": self.provider,
            "model": self.model,
            "eligible": self.eligible,
            "reasons": list(self.reasons),
            "rank": list(self.rank) if self.rank is not None else None,
        }


@dataclass(frozen=True)
class RoutingDecision:
    request: RoutingRequest
    policy_sources: tuple[str, ...]
    default_profile: str | None
    selected_profile: str | None
    selection_reason: str
    candidates: tuple[RoutingCandidate, ...]

    def body_dict(self) -> dict[str, object]:
        return {
            "apiVersion": MODEL_ROUTING_API_VERSION,
            "request": self.request.as_dict(),
            "policy_sources": list(self.policy_sources),
            "default_profile": self.default_profile,
            "selected_profile": self.selected_profile,
            "selection_reason": self.selection_reason,
            "candidates": [item.as_dict() for item in self.candidates],
        }

    @property
    def sha256(self) -> str:
        return "sha256:" + sha256(_canonical_bytes(self.body_dict())).hexdigest()

    def as_dict(self) -> dict[str, object]:
        result = self.body_dict()
        result["sha256"] = self.sha256
        return result

    def to_json(self) -> str:
        return _canonical_bytes(self.as_dict()).decode("utf-8")


def _availability(request: RoutingRequest, profile: AgentProfile) -> bool | None:
    availability = request.provider_availability or {}
    if profile.name in availability:
        return availability[profile.name]
    if profile.provider in availability:
        return availability[profile.provider]
    return None


def _requires_advanced(request: RoutingRequest) -> bool:
    return (
        request.risk in {"critical", "regulated"}
        or request.complexity in {"high", "extreme"}
        or request.capability is Capability.SECURITY
        or (request.stage or "").casefold() in _FINAL_REVIEW_STAGES
    )


def _candidate(
    profile: AgentProfile,
    request: RoutingRequest,
    *,
    policy,
    mode: ExecutionMode,
    default_profile: str | None,
) -> RoutingCandidate:
    rejected: list[str] = []
    allowed: list[str] = []
    if not profile.enabled:
        rejected.append("profile-disabled")
    else:
        allowed.append("profile-enabled")
    if not profile.supports(request.capability):
        rejected.append(f"capability-not-supported:{request.capability.value}")
    else:
        allowed.append(f"capability-supported:{request.capability.value}")
    try:
        policy.assert_profile_allowed(profile, request.capability, mode)
    except PolicyError as exc:
        rejected.append(f"policy-rejected:{exc}")
    else:
        allowed.append("effective-policy-allowed")
    if request.risk not in profile.risk_levels:
        rejected.append(f"risk-not-supported:{request.risk}")
    else:
        allowed.append(f"risk-supported:{request.risk}")
    if request.complexity not in profile.complexity_levels:
        rejected.append(f"complexity-not-supported:{request.complexity}")
    else:
        allowed.append(f"complexity-supported:{request.complexity}")
    if request.context_chars > profile.max_context_chars:
        rejected.append(f"context-too-large:{request.context_chars}>{profile.max_context_chars}")
    else:
        allowed.append(f"context-within-limit:{profile.max_context_chars}")
    if _COST_RANK[profile.cost_class] > _COST_RANK[request.max_cost_class]:
        rejected.append(f"cost-class-exceeds-request:{profile.cost_class}>{request.max_cost_class}")
    else:
        allowed.append(f"cost-class-allowed:{profile.cost_class}")
    if request.affected_technologies:
        supported = set(profile.technologies)
        missing = [item for item in request.affected_technologies if "*" not in supported and item not in supported]
        if missing:
            rejected.append("technology-not-supported:" + ",".join(missing))
        else:
            allowed.append("technology-supported:" + ",".join(request.affected_technologies))
    else:
        allowed.append("technology-unconstrained")
    if _requires_advanced(request) and profile.routing_tier != "advanced":
        rejected.append("advanced-routing-tier-required")
    else:
        allowed.append(f"routing-tier-allowed:{profile.routing_tier}")
    availability = _availability(request, profile)
    if availability is False:
        rejected.append("provider-unavailable")
    elif availability is True:
        allowed.append("provider-available")
    else:
        allowed.append("provider-availability-unreported")
    eligible = not rejected
    reasons = tuple(rejected if rejected else allowed)
    rank: tuple[object, ...] | None = None
    if eligible:
        rank = (
            profile.routing_priority,
            0 if profile.name == default_profile else 1,
            _COST_RANK[profile.cost_class],
            profile.provider,
            profile.model or "",
            profile.name,
        )
    return RoutingCandidate(
        profile=profile.name,
        provider=profile.provider,
        model=profile.model,
        eligible=eligible,
        reasons=reasons,
        rank=rank,
    )


def _explicit_match(candidate: RoutingCandidate, request: RoutingRequest) -> bool:
    if request.requested_profile is not None and candidate.profile != request.requested_profile:
        return False
    if request.requested_provider is not None and candidate.provider != request.requested_provider:
        return False
    if request.requested_model is not None and candidate.model != request.requested_model:
        return False
    return True


def route_model(
    project_root: Path,
    request: RoutingRequest,
    *,
    mode: ExecutionMode = ExecutionMode.ADVISORY,
    environ: Mapping[str, str] | None = None,
) -> RoutingDecision:
    """Return a deterministic, provider-neutral routing explanation for one invocation."""
    if not isinstance(request, RoutingRequest):
        raise _fail("SDAI-ROUTING-002", "request must be a RoutingRequest")
    root = project_root.resolve()
    profiles = load_profiles(root)
    routes = load_routes(root)
    policy = load_effective_configuration(root, environ=environ)
    default_profile = routes.get(request.capability)
    candidates = tuple(
        _candidate(
            profiles[name],
            request,
            policy=policy,
            mode=mode,
            default_profile=default_profile,
        )
        for name in sorted(profiles)
    )
    eligible = [item for item in candidates if item.eligible]
    selected: RoutingCandidate | None = None
    if request.has_explicit_request:
        matching = [item for item in eligible if _explicit_match(item, request)]
        if matching:
            selected = sorted(matching, key=lambda item: item.rank or ())[0]
            reason = "explicit-request-selected"
        else:
            reason = "explicit-request-not-eligible-no-fallback"
    elif eligible:
        selected = sorted(eligible, key=lambda item: item.rank or ())[0]
        reason = "deterministic-eligible-rank"
    else:
        reason = "no-eligible-profile"
    return RoutingDecision(
        request=request,
        policy_sources=policy.sources,
        default_profile=default_profile,
        selected_profile=selected.profile if selected is not None else None,
        selection_reason=reason,
        candidates=candidates,
    )


__all__ = [
    "MODEL_ROUTING_API_VERSION",
    "ModelRoutingError",
    "RoutingCandidate",
    "RoutingDecision",
    "RoutingRequest",
    "route_model",
]
