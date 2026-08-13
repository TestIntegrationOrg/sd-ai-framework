from __future__ import annotations

from pathlib import Path

from sdai.agent_platform import AgentRuntime, Capability
from sdai.agent_platform.model_routing import RoutingRequest
from sdai.agent_platform.routed_execution import build_routed_invocation
from sdai.scaffold import init_project
from sdai.v05_scaffold import install_v05_scaffold


FEATURE = "ROUTED-EXECUTION-123"


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")


def _workspace(tmp_path: Path) -> Path:
    root = tmp_path / "routed execution Ω"
    root.mkdir()
    init_project(root)
    install_v05_scaffold(root)
    return root


def test_explicit_context_size_is_derived_before_routing(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    _write(
        root / ".sdai" / "agents.yaml",
        """version: 1
profiles:
  undersized:
    provider: provider-a
    capabilities: [coding]
    prompt: auto
    cost_class: economy
    max_context_chars: 1500
  adequate:
    provider: provider-b
    capabilities: [coding]
    prompt: auto
    cost_class: standard
    max_context_chars: 5000
""",
    )
    _write(root / ".sdai" / "routing.yaml", "version: 1\nroutes: {}\n")
    request = RoutingRequest(
        semantic_role="developer",
        capability=Capability.CODING,
        context_chars=0,
        provider_availability={"provider-a": True, "provider-b": True},
    )
    context = "x" * 2000

    routed = build_routed_invocation(
        AgentRuntime(root),
        FEATURE,
        request,
        explicit_context=context,
    )

    assert routed.decision.request.context_chars == len(context)
    assert routed.decision.selected_profile == "adequate"
    undersized = next(item for item in routed.decision.candidates if item.profile == "undersized")
    assert undersized.eligible is False
    assert any(reason.startswith("context-too-large:") for reason in undersized.reasons)


def test_process_organization_policy_governs_decision_and_invocation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = _workspace(tmp_path)
    _write(
        root / ".sdai" / "agents.yaml",
        """version: 1
profiles:
  preferred:
    provider: provider-a
    capabilities: [coding]
    prompt: auto
    routing_priority: 1
  organization-allowed:
    provider: provider-b
    capabilities: [coding]
    prompt: auto
    routing_priority: 50
""",
    )
    _write(root / ".sdai" / "routing.yaml", "version: 1\nroutes: {}\n")
    org = tmp_path / "organization-policy.yaml"
    _write(
        org,
        """version: 1
providers:
  allowed_providers: [provider-b]
""",
    )
    monkeypatch.setenv("SDAI_ORG_POLICY_PATH", str(org.resolve()))
    monkeypatch.delenv("SDAI_USER_POLICY_PATH", raising=False)
    request = RoutingRequest(
        semantic_role="developer",
        capability=Capability.CODING,
        provider_availability={"provider-a": True, "provider-b": True},
    )

    routed = build_routed_invocation(
        AgentRuntime(root),
        FEATURE,
        request,
        explicit_context="current task context",
    )

    assert routed.decision.selected_profile == "organization-allowed"
    assert routed.invocation.profile.name == "organization-allowed"
    assert any(source.startswith("organization:") for source in routed.decision.policy_sources)
