from __future__ import annotations

from typing import Sequence

from sdai.completion_policy import (
    CompletionPolicyContribution,
    CompletionPolicyLayer,
    CompletionPolicyResolution,
    CompletionRisk,
    CompletionScope,
    builtin_completion_policy,
    validate_completion_contract,
)


def _contracts(values: Sequence[str], *, label: str) -> tuple[str, ...]:
    normalized = tuple(
        sorted(
            validate_completion_contract(item, label=label)
            for item in values
        )
    )
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"{label} contains duplicate contracts")
    return normalized


def resolve_layered_completion_policy(
    risk: CompletionRisk | str,
    scope: CompletionScope | str,
    *,
    organization_required: Sequence[str] = (),
    repository_required: Sequence[str] = (),
    user_required: Sequence[str] = (),
    additional_required: Sequence[str] = (),
) -> CompletionPolicyResolution:
    base = builtin_completion_policy(risk, scope)
    contributions = list(base.contributions)
    layers = (
        (
            CompletionPolicyLayer.ORG,
            "organization",
            "organization-policy",
            organization_required,
        ),
        (
            CompletionPolicyLayer.REPO,
            "repository",
            "repository-policy",
            repository_required,
        ),
        (
            CompletionPolicyLayer.USER,
            "user",
            "user-policy",
            user_required,
        ),
        (
            CompletionPolicyLayer.USER,
            "caller",
            "additional-required",
            additional_required,
        ),
    )
    for layer, source, policy_id, values in layers:
        contracts = _contracts(values, label=f"{source} required evidence")
        if contracts:
            contributions.append(
                CompletionPolicyContribution(
                    layer,
                    source,
                    policy_id,
                    contracts,
                )
            )
    required = tuple(
        sorted(
            {
                contract
                for contribution in contributions
                for contract in contribution.contracts
            }
        )
    )
    return CompletionPolicyResolution(
        base.risk,
        base.scope,
        required,
        tuple(contributions),
    )


__all__ = ["resolve_layered_completion_policy"]
