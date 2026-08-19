from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from sdai.agent_platform.audit import AgentAuditError, AgentAuditRecorder
from sdai.agent_platform.model_routing import RoutingRequest
from sdai.agent_platform.models import Capability, ExecutionMode
from sdai.agent_platform.routed_execution import build_routed_invocation
from sdai.agent_platform.runtime import AgentRuntime
from sdai.audit_ledger import AuditLedger
from sdai.providers.factory import ProviderFactory
from sdai.scaffold import init_project


FEATURE = "AI-AUDIT-237"


class _FakeProvider:
    def __init__(self, *, output: str = "PROVIDER_OUTPUT", error: BaseException | None = None) -> None:
        self.output = output
        self.error = error
        self.calls: list[tuple[str, str]] = []

    def complete(self, *, system: str, prompt: str) -> str:
        self.calls.append((system, prompt))
        if self.error is not None:
            raise self.error
        return self.output


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")
    return path


def _project(tmp_path: Path, *, feature_workspace: bool = True) -> Path:
    root = tmp_path / "project"
    init_project(root)
    if feature_workspace:
        _write(
            root / "specs" / FEATURE / "00-intake.md",
            """# Feature Intake — AI-AUDIT-237

## Title
AI audit provenance

## Description
RAW_CONTEXT_MARKER must only be hash-bound in audit evidence.
""",
        )
    _write(
        root / ".sdai" / "agents" / "audit-agent.agent.md",
        """---
name: audit-agent
description: Audit provenance test semantic agent
capabilities: [coding]
skills: [secure-coding]
profile: codex
execution_mode: advisory
---
Implement the requested change without weakening governance.
""",
    )
    return root


def _provider(monkeypatch: pytest.MonkeyPatch, fake: _FakeProvider) -> None:
    monkeypatch.setattr(
        ProviderFactory,
        "create",
        staticmethod(lambda *args, **kwargs: fake),
    )


def _binding(event, source: str):
    return next(item for item in event.bindings if item.source == source)


def test_agent_runtime_records_hash_only_start_and_success_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _project(tmp_path)
    fake = _FakeProvider(output="RAW_OUTPUT_MARKER")
    _provider(monkeypatch, fake)
    runtime = AgentRuntime(root)
    invocation = runtime.build_invocation(
        FEATURE,
        Capability.CODING,
        agent_name="audit-agent",
        mode=ExecutionMode.ADVISORY,
    )

    result = runtime.execute_invocation(invocation)

    assert result.output == "RAW_OUTPUT_MARKER"
    assert len(fake.calls) == 1
    events = AuditLedger(root, FEATURE).read()
    assert [item.action.kind for item in events] == [
        "agent.execution.started",
        "agent.execution.succeeded",
    ]
    start, success = events
    assert start.actor.kind == "ai"
    assert start.actor.subject == "agent:audit-agent"
    assert start.actor.semantic_role == "audit-agent"
    assert start.actor.provider == invocation.profile.provider
    assert start.actor.model == invocation.profile.model
    assert start.metadata["profile"] == invocation.profile.name
    assert start.metadata["capability"] == "coding"
    assert start.metadata["executionMode"] == "advisory"
    assert "secure-coding" in start.metadata["skills"]

    expected_sources = {
        ".sdai/agents.yaml",
        ".sdai/prompts/developer.md",
        ".sdai/agents/audit-agent.agent.md",
        ".sdai/skills/secure-coding/SKILL.md",
        ".sdai/skills/secure-coding/skill.yaml",
        "agent-invocation/system",
        "agent-invocation/prompt",
    }
    assert expected_sources <= {item.source for item in start.bindings}
    assert "agent-invocation/output" not in {item.source for item in start.bindings}
    assert _binding(success, "agent-invocation/output").sha256.startswith("sha256:")
    assert _binding(success, "agent-execution/start-event").sha256 == start.sha256

    ledger_bytes = AuditLedger(root, FEATURE).export_jsonl()
    assert b"RAW_CONTEXT_MARKER" not in ledger_bytes
    assert b"RAW_OUTPUT_MARKER" not in ledger_bytes
    assert invocation.prompt.encode("utf-8") not in ledger_bytes
    assert invocation.system.encode("utf-8") not in ledger_bytes


def test_provider_failure_records_safe_terminal_event_without_exception_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _project(tmp_path)
    fake = _FakeProvider(error=RuntimeError("token=DO_NOT_PERSIST provider body"))
    _provider(monkeypatch, fake)
    runtime = AgentRuntime(root)
    invocation = runtime.build_invocation(FEATURE, Capability.CODING, agent_name="audit-agent")

    with pytest.raises(RuntimeError, match="DO_NOT_PERSIST"):
        runtime.execute_invocation(invocation)

    assert len(fake.calls) == 1
    events = AuditLedger(root, FEATURE).read()
    assert [item.action.kind for item in events] == [
        "agent.execution.started",
        "agent.execution.failed",
    ]
    failure = events[-1]
    assert failure.metadata["status"] == "failed"
    assert failure.metadata["failureType"] == "RuntimeError"
    exported = AuditLedger(root, FEATURE).export_jsonl()
    assert b"DO_NOT_PERSIST" not in exported
    assert b"provider body" not in exported


def test_start_audit_failure_prevents_provider_creation_and_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _project(tmp_path)
    fake = _FakeProvider()
    factory_calls: list[object] = []

    def create(*args, **kwargs):
        factory_calls.append(object())
        return fake

    monkeypatch.setattr(ProviderFactory, "create", staticmethod(create))

    def fail_start(self, invocation, provenance):
        raise AgentAuditError("SDAI-AGENT-AUDIT-TEST: forced start failure")

    monkeypatch.setattr(AgentAuditRecorder, "started", fail_start)
    invocation = AgentRuntime(root).build_invocation(
        FEATURE,
        Capability.CODING,
        agent_name="audit-agent",
    )

    with pytest.raises(AgentAuditError, match="forced start failure"):
        AgentRuntime(root).execute_invocation(invocation)

    assert factory_calls == []
    assert fake.calls == []
    assert AuditLedger(root, FEATURE).verify().event_count == 0


def test_terminal_audit_failure_never_retries_provider_and_leaves_start_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _project(tmp_path)
    fake = _FakeProvider(output="one-call-only")
    _provider(monkeypatch, fake)

    def fail_success(self, invocation, provenance, *, output, started_event):
        raise AgentAuditError("SDAI-AGENT-AUDIT-TEST: forced terminal failure")

    monkeypatch.setattr(AgentAuditRecorder, "succeeded", fail_success)
    runtime = AgentRuntime(root)
    invocation = runtime.build_invocation(FEATURE, Capability.CODING, agent_name="audit-agent")

    with pytest.raises(AgentAuditError, match="forced terminal failure"):
        runtime.execute_invocation(invocation)

    assert len(fake.calls) == 1
    events = AuditLedger(root, FEATURE).read()
    assert len(events) == 1
    assert events[0].action.kind == "agent.execution.started"


def test_routed_execution_binds_existing_verified_routing_decision_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _project(tmp_path)
    fake = _FakeProvider()
    _provider(monkeypatch, fake)
    runtime = AgentRuntime(root)
    request = RoutingRequest(
        semantic_role="audit-agent",
        capability=Capability.CODING,
        requested_profile="codex",
        provider_availability={"codex": True},
    )
    routed = build_routed_invocation(
        runtime,
        FEATURE,
        request,
        explicit_context="ROUTED_CONTEXT_MARKER",
    )

    runtime.execute_invocation(routed.invocation)

    start = AuditLedger(root, FEATURE).read()[0]
    routing = _binding(start, "model-routing/decision")
    assert routing.sha256 == routed.decision.sha256
    assert start.metadata["routingDecisionSha256"] == routed.decision.sha256
    assert b"ROUTED_CONTEXT_MARKER" not in AuditLedger(root, FEATURE).export_jsonl()


def test_tampered_routing_decision_fails_before_provider_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _project(tmp_path)
    fake = _FakeProvider()
    _provider(monkeypatch, fake)
    runtime = AgentRuntime(root)
    request = RoutingRequest(
        semantic_role="audit-agent",
        capability=Capability.CODING,
        requested_profile="codex",
    )
    routed = build_routed_invocation(
        runtime,
        FEATURE,
        request,
        explicit_context="context",
    )
    tampered = replace(
        routed.invocation,
        routing_decision=routed.invocation.routing_decision.replace(
            '"selected_profile":"codex"',
            '"selected_profile":"claude"',
        ),
    )

    with pytest.raises(AgentAuditError, match="routing decision"):
        runtime.execute_invocation(tampered)

    assert fake.calls == []
    assert AuditLedger(root, FEATURE).verify().event_count == 0


def test_agent_source_mutation_changes_provenance_hash_deterministically(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _project(tmp_path)
    fake = _FakeProvider()
    _provider(monkeypatch, fake)
    runtime = AgentRuntime(root)

    first_invocation = runtime.build_invocation(FEATURE, Capability.CODING, agent_name="audit-agent")
    runtime.execute_invocation(first_invocation)
    first_start = AuditLedger(root, FEATURE).read()[0]
    first_hash = _binding(first_start, ".sdai/agents/audit-agent.agent.md").sha256

    agent_path = root / ".sdai" / "agents" / "audit-agent.agent.md"
    agent_path.write_text(
        agent_path.read_text(encoding="utf-8").replace(
            "Implement the requested change without weakening governance.",
            "Implement the requested change and preserve every governance boundary.",
        ),
        encoding="utf-8",
        newline="\n",
    )
    second_invocation = runtime.build_invocation(FEATURE, Capability.CODING, agent_name="audit-agent")
    runtime.execute_invocation(second_invocation)
    second_start = AuditLedger(root, FEATURE).read()[2]
    second_hash = _binding(second_start, ".sdai/agents/audit-agent.agent.md").sha256

    assert first_hash != second_hash
    assert len(fake.calls) == 2


def test_no_feature_workspace_preserves_non_feature_runtime_behavior(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _project(tmp_path, feature_workspace=False)
    fake = _FakeProvider(output="no-feature-output")
    _provider(monkeypatch, fake)
    runtime = AgentRuntime(root)
    invocation = runtime.build_explicit_context_invocation(
        FEATURE,
        Capability.CODING,
        "bounded explicit context",
        agent_name="audit-agent",
    )

    result = runtime.execute_invocation(invocation)

    assert result.output == "no-feature-output"
    assert len(fake.calls) == 1
    assert not (root / "specs" / FEATURE).exists()
    assert not (root / "specs" / "changes" / FEATURE).exists()


def test_unrelated_malformed_pack_state_does_not_block_non_pack_agent_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _project(tmp_path)
    _write(root / ".sdai" / "packs" / "install-state.json", "{malformed pack state")
    fake = _FakeProvider(output="ok")
    _provider(monkeypatch, fake)
    runtime = AgentRuntime(root)
    invocation = runtime.build_invocation(FEATURE, Capability.CODING, agent_name="audit-agent")

    result = runtime.execute_invocation(invocation)

    assert result.output == "ok"
    assert len(fake.calls) == 1
    assert AuditLedger(root, FEATURE).verify().event_count == 2
