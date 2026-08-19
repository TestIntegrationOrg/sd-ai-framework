from __future__ import annotations

from pathlib import Path

from sdai.agent_platform import (
    Capability,
    ProviderHealthSignal,
    RoutingRequest,
    route_model,
)
from sdai.scaffold import init_project


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")


def _profiles(root: Path, body: str, routes: str = "routes: {}\n") -> None:
    _write(root / ".sdai" / "agents.yaml", "version: 1\nprofiles:\n" + body)
    _write(root / ".sdai" / "routing.yaml", "version: 1\n" + routes)


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "routing optimization"
    root.mkdir()
    init_project(root)
    _profiles(
        root,
        """  economy:
    provider: provider-economy
    model: economy-model
    capabilities: [coding]
    prompt: auto
    cost_class: economy
    routing_priority: 100
    routing_tier: advanced
  premium:
    provider: provider-premium
    model: premium-model
    capabilities: [coding]
    prompt: auto
    cost_class: premium
    routing_priority: 1
    routing_tier: advanced
  standard-tier:
    provider: provider-standard
    model: standard-model
    capabilities: [coding]
    prompt: auto
    cost_class: economy
    routing_priority: 0
    routing_tier: standard
""",
        routes="routes:\n  coding: premium\n",
    )
    return root


def _health(state: str, latency: int | None) -> ProviderHealthSignal:
    return ProviderHealthSignal(
        state=state,
        samples=4,
        successes=3 if state != "unavailable" else 0,
        failures=1 if state != "unavailable" else 4,
        p50_total_ns=latency,
        latest_status="succeeded" if state == "healthy" else "failed",
        source_sha256="sha256:" + "a" * 64,
    )


def test_legacy_default_request_keeps_historical_document_shape_and_selection(tmp_path: Path) -> None:
    root = _root(tmp_path)
    request = RoutingRequest(
        semantic_role="developer",
        capability=Capability.CODING,
        provider_availability={
            "provider-economy": True,
            "provider-premium": True,
            "provider-standard": True,
        },
    )

    decision = route_model(root, request, environ={})

    assert decision.selected_profile == "standard-tier"
    assert decision.selection_reason == "deterministic-eligible-rank"
    request_payload = decision.as_dict()["request"]
    assert "optimization" not in request_payload
    assert "fallback_profiles" not in request_payload
    assert "provider_health" not in request_payload
    assert all("health_state" not in candidate.as_dict() for candidate in decision.candidates)


def test_cost_optimization_prefers_lower_cost_inside_hard_eligibility(tmp_path: Path) -> None:
    root = _root(tmp_path)
    request = RoutingRequest(
        semantic_role="developer",
        capability=Capability.CODING,
        optimization="cost",
        provider_availability={"provider-economy": True, "provider-premium": True},
        provider_health={
            "economy": _health("healthy", 1_000),
            "premium": _health("healthy", 10),
            "standard-tier": ProviderHealthSignal(state="unavailable", samples=2, failures=2, latest_status="failed"),
        },
    )

    decision = route_model(root, request, environ={})

    assert decision.selected_profile == "economy"
    assert decision.selection_reason == "optimized-eligible-rank:cost"


def test_latency_optimization_prefers_faster_healthy_provider(tmp_path: Path) -> None:
    root = _root(tmp_path)
    request = RoutingRequest(
        semantic_role="developer",
        capability=Capability.CODING,
        optimization="latency",
        provider_availability={"provider-economy": True, "provider-premium": True},
        provider_health={
            "economy": _health("healthy", 10_000),
            "premium": _health("healthy", 100),
            "standard-tier": ProviderHealthSignal(state="unavailable", samples=2, failures=2, latest_status="failed"),
        },
    )

    decision = route_model(root, request, environ={})

    assert decision.selected_profile == "premium"
    premium = next(item for item in decision.candidates if item.profile == "premium")
    assert premium.health_state == "healthy"
    assert premium.observed_latency_ns == 100


def test_unavailable_health_signal_rejects_candidate_before_optimization(tmp_path: Path) -> None:
    root = _root(tmp_path)
    request = RoutingRequest(
        semantic_role="developer",
        capability=Capability.CODING,
        optimization="latency",
        provider_health={
            "premium": ProviderHealthSignal(state="unavailable", samples=2, failures=2, latest_status="failed"),
            "economy": _health("healthy", 1_000),
            "standard-tier": ProviderHealthSignal(state="unavailable", samples=2, failures=2, latest_status="failed"),
        },
    )

    decision = route_model(root, request, environ={})

    assert decision.selected_profile == "economy"
    premium = next(item for item in decision.candidates if item.profile == "premium")
    assert premium.eligible is False
    assert "provider-health-unavailable" in premium.reasons


def test_fallback_order_precedes_cost_latency_ranking_for_eligible_profiles(tmp_path: Path) -> None:
    root = _root(tmp_path)
    request = RoutingRequest(
        semantic_role="developer",
        capability=Capability.CODING,
        optimization="cost",
        fallback_profiles=("premium", "economy"),
        provider_health={
            "premium": _health("healthy", 1_000),
            "economy": _health("healthy", 10),
            "standard-tier": ProviderHealthSignal(state="unavailable", samples=2, failures=2, latest_status="failed"),
        },
    )

    decision = route_model(root, request, environ={})

    assert decision.selected_profile == "premium"


def test_explicit_request_remains_authoritative_over_fallback_when_eligible(tmp_path: Path) -> None:
    root = _root(tmp_path)
    request = RoutingRequest(
        semantic_role="developer",
        capability=Capability.CODING,
        requested_profile="premium",
        optimization="cost",
        fallback_profiles=("economy",),
        provider_health={
            "premium": _health("degraded", 10_000),
            "economy": _health("healthy", 10),
        },
    )

    decision = route_model(root, request, environ={})

    assert decision.selected_profile == "premium"
    assert decision.selection_reason == "explicit-request-selected"


def test_critical_risk_hard_constraint_cannot_be_weakened_by_low_cost_or_latency(tmp_path: Path) -> None:
    root = _root(tmp_path)
    request = RoutingRequest(
        semantic_role="developer",
        capability=Capability.CODING,
        risk="critical",
        optimization="latency",
        provider_health={
            "standard-tier": _health("healthy", 1),
            "premium": _health("healthy", 1_000),
            "economy": _health("healthy", 2_000),
        },
    )

    decision = route_model(root, request, environ={})

    assert decision.selected_profile in {"premium", "economy"}
    standard = next(item for item in decision.candidates if item.profile == "standard-tier")
    assert standard.eligible is False
    assert "advanced-routing-tier-required" in standard.reasons


def test_routing_with_same_health_snapshot_is_byte_deterministic(tmp_path: Path) -> None:
    root = _root(tmp_path)
    health = {
        "premium": _health("healthy", 100),
        "economy": _health("healthy", 200),
    }
    request = RoutingRequest(
        semantic_role="developer",
        capability=Capability.CODING,
        optimization="latency",
        provider_health=health,
    )

    first = route_model(root, request, environ={})
    second = route_model(root, request, environ={})

    assert first.to_json() == second.to_json()
    assert first.sha256 == second.sha256
