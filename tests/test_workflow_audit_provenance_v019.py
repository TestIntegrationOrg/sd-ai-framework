from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from sdai.agent_platform.runtime import AgentRuntime
from sdai.audited_orchestrator import AuditedOrchestrator
from sdai.audit_ledger import AuditLedger
from sdai.audit_provenance import AuditAction, AuditActor
from sdai.artifacts import write_text
from sdai.policy import load_effective_configuration
from sdai.providers.factory import ProviderFactory
from sdai.scaffold import init_project
from sdai.version_entrypoint import main as sdai_main
from sdai.workflow_audit import WorkflowAuditRecorder
from sdai.workflows import grant_approval, load_workflow


FEATURE = "WF-AUDIT-238"


class _FakeProvider:
    def __init__(self, output: str = "provider-output") -> None:
        self.output = output
        self.calls: list[tuple[str, str]] = []

    def complete(self, *, system: str, prompt: str) -> str:
        self.calls.append((system, prompt))
        return self.output


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    init_project(root)
    write_text(
        root / "specs" / FEATURE / "00-intake.md",
        f"# Feature Intake — {FEATURE}\n\n## Title\nAudit\n\n## Description\nAudit workflow provenance.\n",
    )
    return root


def _workflow(root: Path, name: str, body: str) -> Path:
    return write_text(
        root / ".sdai" / "workflows" / f"{name}.yaml",
        body,
    )


def _binding(event, source: str):
    return next(item for item in event.bindings if item.source == source)


def _provider(monkeypatch: pytest.MonkeyPatch, fake: _FakeProvider) -> None:
    monkeypatch.setattr(
        ProviderFactory,
        "create",
        staticmethod(lambda *args, **kwargs: fake),
    )


def test_deterministic_workflow_binds_authority_sources_state_and_output(tmp_path: Path) -> None:
    root = _project(tmp_path)
    _workflow(
        root,
        "audit-simple",
        """version: 3
name: audit-simple
validation_mode: standard
steps:
  - id: specification
    type: deterministic
    action: specify
""",
    )

    result = AuditedOrchestrator(root).run_workflow(FEATURE, "audit-simple")
    assert [item.status for item in result] == ["completed"]

    events = AuditLedger(root, FEATURE).read()
    assert [item.action.kind for item in events] == [
        "workflow.execution.started",
        "workflow.step.started",
        "workflow.step.completed",
        "workflow.execution.completed",
    ]
    terminal = events[2]
    sources = {item.source for item in terminal.bindings}
    assert ".sdai/workflows/audit-simple.yaml" in sources
    assert ".sdai/constitution.yaml" in sources
    assert ".sdai/config.yaml" in sources
    assert "policy/effective" in sources
    assert "workflow/resolved/audit-simple" in sources
    assert f"specs/{FEATURE}/specification.md" in sources
    assert "workflow/state/after" in sources
    assert AuditLedger(root, FEATURE).verify().event_count == 4


def test_approval_pause_and_local_approval_are_provenance_not_verified_identity(tmp_path: Path) -> None:
    root = _project(tmp_path)
    _workflow(
        root,
        "approval-flow",
        """version: 3
name: approval-flow
validation_mode: light
steps:
  - id: architecture-approval
    type: approval
    gate: architecture
""",
    )
    orchestrator = AuditedOrchestrator(root)

    first = orchestrator.run_workflow(FEATURE, "approval-flow")
    assert [item.status for item in first] == ["paused"]
    events = AuditLedger(root, FEATURE).read()
    assert events[-2].action.kind == "workflow.step.paused"
    assert events[-1].action.kind == "workflow.execution.paused"

    grant_approval(
        orchestrator.context(FEATURE),
        "architecture",
        approved_by="local-architect@example.test",
        role="architect",
    )
    second = orchestrator.run_workflow(FEATURE, "approval-flow")
    assert [item.status for item in second] == ["completed"]

    events = AuditLedger(root, FEATURE).read()
    step_terminal = next(
        item
        for item in reversed(events)
        if item.action.kind == "workflow.step.completed"
        and item.execution.step_id == "architecture-approval"
    )
    assert f"specs/{FEATURE}/approvals/architecture.yaml" in {
        item.source for item in step_terminal.bindings
    }
    exported = AuditLedger(root, FEATURE).export_jsonl()
    assert b"local-architect@example.test" not in exported
    assert b"identityVerified" not in exported
    assert b"authorized" not in exported


def test_manual_workspace_write_rejection_is_audited_without_provider_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _project(tmp_path)
    _workflow(
        root,
        "write-flow",
        """version: 3
name: write-flow
validation_mode: light
steps:
  - id: human-gate
    type: approval
    gate: architecture
  - id: implementation
    type: agent
    capability: coding
    mode: workspace-write
""",
    )
    fake = _FakeProvider()
    _provider(monkeypatch, fake)
    orchestrator = AuditedOrchestrator(root)

    with pytest.raises(RuntimeError, match="unsatisfied prior approval"):
        orchestrator.run_manual_step(FEATURE, "write-flow", "implementation")

    assert fake.calls == []
    events = AuditLedger(root, FEATURE).read()
    assert [item.action.kind for item in events] == [
        "workflow.step.started",
        "workflow.step.failed",
    ]
    assert events[-1].metadata["failureType"] == "RuntimeError"
    assert b"unsatisfied prior approval" not in AuditLedger(root, FEATURE).export_jsonl()


def test_agent_workflow_step_references_existing_ai_provenance_without_duplicate_provider_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _project(tmp_path)
    _workflow(
        root,
        "ai-flow",
        """version: 3
name: ai-flow
validation_mode: light
steps:
  - id: implementation-review
    type: agent
    capability: coding
    mode: advisory
""",
    )
    fake = _FakeProvider("HASH_ONLY_OUTPUT_MARKER")
    _provider(monkeypatch, fake)

    result = AuditedOrchestrator(root, agent_runtime=AgentRuntime(root)).run_workflow(
        FEATURE,
        "ai-flow",
    )
    assert [item.status for item in result] == ["completed"]
    assert len(fake.calls) == 1

    events = AuditLedger(root, FEATURE).read()
    assert sum(item.action.kind == "agent.execution.started" for item in events) == 1
    assert sum(item.action.kind == "agent.execution.succeeded" for item in events) == 1
    step_terminal = next(
        item for item in events if item.action.kind == "workflow.step.completed"
    )
    linked = [
        item for item in step_terminal.bindings if item.source.startswith("workflow/agent-event/")
    ]
    assert len(linked) == 2
    assert b"HASH_ONLY_OUTPUT_MARKER" not in AuditLedger(root, FEATURE).export_jsonl()


def test_parallel_agent_audit_serializes_concurrent_writers_and_preserves_child_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _project(tmp_path)
    _workflow(
        root,
        "parallel-ai",
        """version: 3
name: parallel-ai
validation_mode: light
steps:
  - id: reviewers
    type: parallel
    steps:
      - id: coding-review
        type: agent
        capability: coding
        mode: advisory
      - id: security-review
        type: agent
        capability: security
        mode: advisory
""",
    )
    fake = _FakeProvider()
    _provider(monkeypatch, fake)

    result = AuditedOrchestrator(root, agent_runtime=AgentRuntime(root)).run_workflow(
        FEATURE,
        "parallel-ai",
    )
    assert [item.status for item in result] == ["completed"]
    assert len(fake.calls) == 2

    ledger = AuditLedger(root, FEATURE)
    events = ledger.read()
    assert ledger.verify().event_count == len(events)
    assert sum(item.action.kind == "agent.execution.started" for item in events) == 2
    assert sum(item.action.kind == "agent.execution.succeeded" for item in events) == 2
    parent = next(
        item
        for item in events
        if item.action.kind == "workflow.step.completed" and item.execution.step_id == "reviewers"
    )
    assert [item["stepId"] for item in parent.metadata["children"]] == [
        "coding-review",
        "security-review",
    ]


def test_audit_ledger_supports_concurrent_system_appends(tmp_path: Path) -> None:
    root = _project(tmp_path)
    ledger = AuditLedger(root, FEATURE)

    def append(index: int) -> None:
        ledger.append(
            category="system",
            actor=AuditActor("system", "concurrency-test"),
            action=AuditAction("test.concurrent.append", f"item:{index}"),
            metadata={"index": index},
        )

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(append, range(24)))

    snapshot = ledger.verify()
    assert snapshot.event_count == 24
    assert [event.sequence for event in ledger.read()] == list(range(1, 25))


def test_sensitive_workflow_values_are_not_persisted_or_hash_bound_in_resolved_projection(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path)
    path = _workflow(
        root,
        "sensitive-flow",
        """version: 6
name: sensitive-flow
validation_mode: standard
inputs:
  signing_secret:
    type: string
    sensitive: true
    default: FIRST_SECRET_VALUE
steps:
  - id: specification
    type: deterministic
    action: specify
""",
    )
    policy = load_effective_configuration(root)
    first_definition = load_workflow(root, "sensitive-flow")
    first = WorkflowAuditRecorder.optional_for(root, FEATURE, policy)
    assert first is not None
    first_provenance = first.prepare(first_definition)
    first_resolved = next(
        item for item in first_provenance.bindings if item.source == "workflow/resolved/sensitive-flow"
    )

    path.write_text(
        path.read_text(encoding="utf-8").replace("FIRST_SECRET_VALUE", "SECOND_SECRET_VALUE"),
        encoding="utf-8",
        newline="\n",
    )
    second_definition = load_workflow(root, "sensitive-flow")
    second_provenance = first.prepare(second_definition)
    second_resolved = next(
        item for item in second_provenance.bindings if item.source == "workflow/resolved/sensitive-flow"
    )
    assert first_resolved.sha256 == second_resolved.sha256

    AuditedOrchestrator(root).run_workflow(FEATURE, "sensitive-flow")
    exported = AuditLedger(root, FEATURE).export_jsonl()
    assert b"FIRST_SECRET_VALUE" not in exported
    assert b"SECOND_SECRET_VALUE" not in exported


def test_effective_policy_hash_is_semantic_while_source_hash_tracks_reordering(tmp_path: Path) -> None:
    root = _project(tmp_path)
    _workflow(
        root,
        "policy-flow",
        """version: 3
name: policy-flow
validation_mode: light
steps:
  - id: specification
    type: deterministic
    action: specify
""",
    )
    policy_path = write_text(
        root / ".sdai" / "policy.yaml",
        """version: 1
providers:
  allowed_profiles: [codex, claude]
capabilities: {}
execution:
  workspace_write: true
  require_prior_approval_for_workspace_write: false
  allow_force_approval_bypass: true
  protected_paths: []
skills:
  required: {}
architecture_validation:
  required: {}
  allow_waivers: true
""",
    )
    definition = load_workflow(root, "policy-flow")
    first = WorkflowAuditRecorder.optional_for(
        root,
        FEATURE,
        load_effective_configuration(root),
    )
    assert first is not None
    first_provenance = first.prepare(definition)
    first_effective = _binding_from(first_provenance.bindings, "policy/effective")
    first_source = _binding_from(first_provenance.bindings, ".sdai/policy.yaml")

    policy_path.write_text(
        policy_path.read_text(encoding="utf-8").replace(
            "allowed_profiles: [codex, claude]",
            "allowed_profiles: [claude, codex]",
        ),
        encoding="utf-8",
        newline="\n",
    )
    second = WorkflowAuditRecorder.optional_for(
        root,
        FEATURE,
        load_effective_configuration(root),
    )
    assert second is not None
    second_provenance = second.prepare(definition)
    second_effective = _binding_from(second_provenance.bindings, "policy/effective")
    second_source = _binding_from(second_provenance.bindings, ".sdai/policy.yaml")

    assert first_effective.sha256 == second_effective.sha256
    assert first_source.sha256 != second_source.sha256


def _binding_from(bindings, source: str):
    return next(item for item in bindings if item.source == source)


def test_versioned_entrypoint_routes_legacy_run_through_audited_orchestrator(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = _project(tmp_path)
    _workflow(
        root,
        "cli-audit",
        """version: 3
name: cli-audit
validation_mode: standard
steps:
  - id: specification
    type: deterministic
    action: specify
""",
    )

    assert sdai_main(
        ["run", FEATURE, "--workflow", "cli-audit", "--path", str(root)]
    ) == 0
    capsys.readouterr()
    assert AuditLedger(root, FEATURE).verify().event_count == 4
