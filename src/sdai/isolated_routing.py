from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Mapping

from sdai.agent_platform import AgentRuntime
from sdai.agent_platform.model_routing import ModelRoutingError, RoutingDecision, RoutingRequest, route_model
from sdai.isolated_tasks import IsolatedTaskContract, PreparedIsolatedInvocation, build_isolated_invocation


@dataclass(frozen=True)
class RoutedIsolatedInvocation:
    decision: RoutingDecision
    prepared: PreparedIsolatedInvocation


def routing_request_for_isolated_contract(
    contract: IsolatedTaskContract,
    *,
    risk: str,
    complexity: str,
    affected_technologies: tuple[str, ...] = (),
    max_cost_class: str = "premium",
    requested_profile: str | None = None,
    requested_provider: str | None = None,
    requested_model: str | None = None,
    provider_availability: Mapping[str, bool] | None = None,
) -> RoutingRequest:
    return RoutingRequest(
        semantic_role=contract.semantic_agent,
        capability=contract.capability,
        risk=risk,
        complexity=complexity,
        affected_technologies=affected_technologies,
        context_chars=len(contract.prompt_context()),
        max_cost_class=max_cost_class,
        requested_profile=requested_profile,
        requested_provider=requested_provider,
        requested_model=requested_model,
        provider_availability=provider_availability,
        stage=contract.stage.value,
    )


def build_routed_isolated_invocation(
    runtime: AgentRuntime,
    contract: IsolatedTaskContract,
    request: RoutingRequest,
) -> RoutedIsolatedInvocation:
    if request.semantic_role != contract.semantic_agent:
        raise ModelRoutingError("SDAI-ROUTING-005: routing semantic role differs from isolated contract")
    if request.capability is not contract.capability:
        raise ModelRoutingError("SDAI-ROUTING-005: routing capability differs from isolated contract")
    if request.stage != contract.stage.value:
        raise ModelRoutingError("SDAI-ROUTING-005: routing stage differs from isolated contract")
    actual_context = len(contract.prompt_context())
    if request.context_chars != actual_context:
        raise ModelRoutingError(
            "SDAI-ROUTING-005: routing context size differs from durable isolated context; "
            f"request={request.context_chars} actual={actual_context}"
        )
    decision = route_model(
        runtime.project_root,
        request,
        mode=contract.mode,
    )
    if decision.selected_profile is None:
        raise ModelRoutingError(
            "SDAI-ROUTING-006: isolated invocation has no eligible profile; "
            f"reason={decision.selection_reason}; decision_sha256={decision.sha256}"
        )
    prepared = build_isolated_invocation(
        runtime,
        contract,
        profile_name=decision.selected_profile,
    )
    if prepared.record.semantic_agent != contract.semantic_agent:
        raise ModelRoutingError("SDAI-ROUTING-006: routing changed provider-neutral semantic agent identity")
    invocation = replace(prepared.invocation, routing_decision=decision.to_json())
    routed_prepared = PreparedIsolatedInvocation(contract, invocation, prepared.record)
    return RoutedIsolatedInvocation(decision, routed_prepared)


__all__ = [
    "RoutedIsolatedInvocation",
    "build_routed_isolated_invocation",
    "routing_request_for_isolated_contract",
]
