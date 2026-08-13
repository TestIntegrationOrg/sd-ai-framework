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
    environ: dict[str, str] | None = None,
) -> RoutedInvocation:
    decision = route_model(runtime.project_root, request, mode=mode, environ=environ)
    if decision.selected_profile is None:
        raise ModelRoutingError(
            "SDAI-ROUTING-003: no profile may be selected; "
            f"reason={decision.selection_reason}; decision_sha256={decision.sha256}"
        )
    if explicit_context is None:
        invocation = runtime.build_invocation(
            feature_id,
            request.capability,
            profile_name=decision.selected_profile,
            agent_name=request.semantic_role,
            mode=mode,
        )
    else:
        invocation = runtime.build_explicit_context_invocation(
            feature_id,
            request.capability,
            explicit_context,
            profile_name=decision.selected_profile,
            agent_name=request.semantic_role,
            mode=mode,
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
