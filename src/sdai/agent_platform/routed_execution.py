from __future__ import annotations

from dataclasses import dataclass, replace

from sdai.agent_platform.model_routing import ModelRoutingError, RoutingDecision, RoutingRequest, route_model
from sdai.agent_platform.models import AgentExecutionResult, AgentInvocation, ExecutionMode
from sdai.agent_platform.runtime import AgentRuntime


@dataclass(frozen=True)
class RoutedInvocation:
    decision: RoutingDecision
    invocation: AgentInvocation


def build_routed_invocation(
    runtime: AgentRuntime,
    feature_id: str,
    request: RoutingRequest,
    *,
    mode: ExecutionMode = ExecutionMode.ADVISORY,
    explicit_context: str | None = None,
) -> RoutedInvocation:
    effective_request = request
    if explicit_context is not None:
        if not isinstance(explicit_context, str) or not explicit_context.strip():
            raise ValueError("explicit agent context must be non-empty text")
        effective_request = replace(request, context_chars=len(explicit_context))
    decision = route_model(runtime.project_root, effective_request, mode=mode)
    if decision.selected_profile is None:
        raise ModelRoutingError(
            "SDAI-ROUTING-003: no profile may be selected; "
            f"reason={decision.selection_reason}; decision_sha256={decision.sha256}"
        )
    if explicit_context is None:
        invocation = runtime.build_invocation(
            feature_id,
            effective_request.capability,
            profile_name=decision.selected_profile,
            agent_name=effective_request.semantic_role,
            mode=mode,
        )
    else:
        invocation = runtime.build_explicit_context_invocation(
            feature_id,
            effective_request.capability,
            explicit_context,
            profile_name=decision.selected_profile,
            agent_name=effective_request.semantic_role,
            mode=mode,
        )
    if invocation.agent_name != effective_request.semantic_role:
        raise ModelRoutingError(
            "SDAI-ROUTING-003: runtime did not preserve the requested semantic agent identity"
        )
    invocation = replace(invocation, routing_decision=decision.to_json())
    return RoutedInvocation(decision, invocation)


def execute_routed_invocation(
    runtime: AgentRuntime,
    routed: RoutedInvocation,
) -> AgentExecutionResult:
    if not isinstance(routed, RoutedInvocation):
        raise TypeError("routed must be a RoutedInvocation")
    if routed.decision.selected_profile != routed.invocation.profile.name:
        raise ModelRoutingError("SDAI-ROUTING-004: routed invocation profile differs from routing decision")
    return runtime.execute_invocation(routed.invocation)


__all__ = [
    "RoutedInvocation",
    "build_routed_invocation",
    "execute_routed_invocation",
]
