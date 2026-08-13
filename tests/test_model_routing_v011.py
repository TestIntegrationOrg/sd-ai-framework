from __future__ import annotations

from pathlib import Path

from sdai.agent_platform import Capability
from sdai.agent_platform.model_routing import RoutingRequest, route_model
from sdai.scaffold import init_project


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")


def _profiles(root: Path, body: str, routes: str = "routes: {}\n") -> None:
    _write(root / ".sdai" / "agents.yaml", "version: 1\nprofiles:\n" + body)
    _write(root / ".sdai" / "routing.yaml", "version: 1\n" + routes)


def test_same_inputs_produce_same_json_and_cost_breaks_equal_rank(tmp_path: Path) -> None:
    root = tmp_path / "routing café"
    root.mkdir()
    init_project(root)
    _profiles(
        root,
        """  economy:
    provider: provider-a
    model: model-a
    capabilities: [coding]
    prompt: auto
    cost_class: economy
    routing_priority: 10
    technologies: [java, café]
  premium:
    provider: provider-b
    model: model-b
    capabilities: [coding]
    prompt: auto
    cost_class: premium
    routing_priority: 10
    technologies: [java, café]
""",
    )
    request = RoutingRequest(
        semantic_role="developer",
        capability=Capability.CODING,
        risk="standard",
        complexity="medium",
        affected_technologies=("java", "café"),
        context_chars=1200,
        max_cost_class="premium",
        provider_availability={"provider-a": True, "provider-b": True},
    )
    first = route_model(root, request, environ={})
    second = route_model(root, request, environ={})
    assert first.to_json() == second.to_json()
    assert first.sha256 == second.sha256
    assert first.selected_profile == "economy"
    assert "café" in first.to_json()


def test_equal_suitability_prefers_lower_cost_before_default_route(tmp_path: Path) -> None:
    root = tmp_path / "routing default cost"
    root.mkdir()
    init_project(root)
    _profiles(
        root,
        """  economy:
    provider: provider-a
    capabilities: [coding]
    prompt: auto
    cost_class: economy
    routing_priority: 10
  premium-default:
    provider: provider-b
    capabilities: [coding]
    prompt: auto
    cost_class: premium
    routing_priority: 10
""",
        routes="routes:\n  coding: premium-default\n",
    )
    request = RoutingRequest(
        semantic_role="developer",
        capability=Capability.CODING,
        provider_availability={"provider-a": True, "provider-b": True},
    )

    decision = route_model(root, request, environ={})

    assert decision.default_profile == "premium-default"
    assert decision.selected_profile == "economy"


def test_explicit_forbidden_profile_has_no_fallback(tmp_path: Path) -> None:
    root = tmp_path / "routing policy"
    root.mkdir()
    init_project(root)
    _profiles(
        root,
        """  requested:
    provider: provider-a
    model: model-a
    capabilities: [coding]
    prompt: auto
  allowed:
    provider: provider-b
    model: model-b
    capabilities: [coding]
    prompt: auto
""",
    )
    _write(root / ".sdai" / "policy.yaml", "version: 1\nproviders:\n  allowed_providers: [provider-a, provider-b]\n")
    org = tmp_path / "org-policy.yaml"
    _write(org, "version: 1\nproviders:\n  allowed_providers: [provider-b]\n")
    user = tmp_path / "user-policy.yaml"
    _write(user, "version: 1\nproviders:\n  allowed_providers: [provider-a, provider-b]\n")
    request = RoutingRequest(
        semantic_role="developer",
        capability=Capability.CODING,
        requested_profile="requested",
        provider_availability={"provider-a": True, "provider-b": True},
    )
    decision = route_model(
        root,
        request,
        environ={"SDAI_ORG_POLICY_PATH": str(org), "SDAI_USER_POLICY_PATH": str(user)},
    )
    assert decision.selected_profile is None
    assert decision.selection_reason == "explicit-request-not-eligible-no-fallback"
    by_name = {item.profile: item for item in decision.candidates}
    assert by_name["requested"].eligible is False
    assert any(reason.startswith("policy-rejected:") for reason in by_name["requested"].reasons)
    assert by_name["allowed"].eligible is True


def test_final_review_escalation_filters_standard_tier(tmp_path: Path) -> None:
    root = tmp_path / "routing final review"
    root.mkdir()
    init_project(root)
    _profiles(
        root,
        """  standard-tier:
    provider: provider-a
    capabilities: [review]
    prompt: auto
    cost_class: economy
    routing_tier: standard
    routing_priority: 1
  advanced-tier:
    provider: provider-b
    capabilities: [review]
    prompt: auto
    cost_class: premium
    routing_tier: advanced
    routing_priority: 50
""",
    )
    request = RoutingRequest(
        semantic_role="code-reviewer",
        capability=Capability.REVIEW,
        risk="standard",
        complexity="medium",
        stage="final-review",
        provider_availability={"provider-a": True, "provider-b": True},
    )
    decision = route_model(root, request, environ={})
    assert decision.selected_profile == "advanced-tier"
    standard = next(item for item in decision.candidates if item.profile == "standard-tier")
    assert standard.eligible is False
    assert "advanced-routing-tier-required" in standard.reasons


def test_availability_context_and_technology_filter_before_rank(tmp_path: Path) -> None:
    root = tmp_path / "routing filters"
    root.mkdir()
    init_project(root)
    _profiles(
        root,
        """  unavailable:
    provider: provider-a
    model: model-a
    capabilities: [coding]
    prompt: auto
    cost_class: economy
    technologies: [java]
  too-small:
    provider: provider-b
    capabilities: [coding]
    prompt: auto
    max_context_chars: 1500
    technologies: [java]
  wrong-tech:
    provider: provider-c
    capabilities: [coding]
    prompt: auto
    technologies: [python]
  selected:
    provider: provider-d
    capabilities: [coding]
    prompt: auto
    cost_class: premium
    max_context_chars: 10000
    technologies: [java]
""",
    )
    request = RoutingRequest(
        semantic_role="developer",
        capability=Capability.CODING,
        affected_technologies=("java",),
        context_chars=2000,
        provider_availability={"model-a": False, "provider-b": True, "provider-c": True, "provider-d": True},
    )
    decision = route_model(root, request, environ={})
    by_name = {item.profile: item for item in decision.candidates}
    assert decision.selected_profile == "selected"
    assert "provider-unavailable" in by_name["unavailable"].reasons
    assert any(reason.startswith("context-too-large:") for reason in by_name["too-small"].reasons)
    assert any(reason.startswith("technology-not-supported:") for reason in by_name["wrong-tech"].reasons)
