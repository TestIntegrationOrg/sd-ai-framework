from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path

import pytest

from sdai.agent_platform import (
    AgentRuntime,
    Capability,
    RetryPolicy,
    RoutingRequest,
    execute_with_retry,
    route_model,
)
from sdai.agent_platform.routed_execution import RoutedInvocation, execute_routed_invocation
from sdai.agent_platform.routing_diagnostics import (
    RoutingDiagnosticError,
    load_routing_diagnostic,
    persist_routing_decision,
)
from sdai.audit_ledger import AuditLedger
from sdai.audit_provenance import AuditAction, AuditActor, AuditBinding, AuditExecution
from sdai.diagnostics import DiagnosticsError, build_diagnostics_report
from sdai.providers.factory import ProviderFactory
from sdai.scaffold import init_project
from sdai.version_entrypoint import main as sdai_main


FEATURE = "UNIFIED-DIAGNOSTICS-020"
RUN_ID = "run-258"
TASK_ID = "task-258"
SECRET_OUTPUT = "provider-secret-output-258"
SECRET_ERROR = "provider-secret-error-258"


class _SequencedProvider:
    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0

    def complete(self, *, system: str, prompt: str) -> str:
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return str(outcome)


def _workspace(root: Path) -> Path:
    init_project(root)
    feature = root / "specs" / "changes" / FEATURE
    feature.mkdir(parents=True, exist_ok=True)
    (feature / "requirements.md").write_text(
        "# Requirements\n\n- FR-258: Unified read-only diagnostics.\n",
        encoding="utf-8",
        newline="\n",
    )
    return feature


def _routed_invocation(runtime: AgentRuntime):
    request = RoutingRequest(
        semantic_role="developer",
        capability=Capability.CODING,
    )
    decision = route_model(runtime.project_root, request, environ={})
    assert decision.selected_profile is not None
    invocation = runtime.build_explicit_context_invocation(
        FEATURE,
        Capability.CODING,
        "bounded diagnostic context",
        profile_name=decision.selected_profile,
    )
    invocation = replace(invocation, routing_decision=decision.to_json())
    return decision, invocation


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _terminal_path(feature: Path, attempt_id: str) -> Path:
    files = sorted(
        (feature / ".sdai" / "diagnostics" / "provider" / attempt_id).glob("*.json")
    )
    assert files
    return files[-1]


def _bind_run_task(root: Path, feature: Path, attempt_id: str) -> None:
    terminal = _terminal_path(feature, attempt_id)
    source = terminal.relative_to(root).as_posix()
    digest = "sha256:" + sha256(terminal.read_bytes()).hexdigest()
    AuditLedger(root, FEATURE).append(
        category="execution",
        actor=AuditActor("system", "diagnostics-test"),
        action=AuditAction("diagnostics.correlation", attempt_id),
        execution=AuditExecution(run_id=RUN_ID, task_id=TASK_ID),
        bindings=(AuditBinding("evidence", source, digest),),
        metadata={"purpose": "selector-correlation"},
        occurred_at="2026-08-19T14:00:00.000000Z",
    )


def test_unified_report_correlates_context_routing_retry_provider_and_audit_without_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    feature = _workspace(tmp_path)
    runtime = AgentRuntime(tmp_path)
    decision, invocation = _routed_invocation(runtime)
    persist_routing_decision(tmp_path, FEATURE, decision)
    provider = _SequencedProvider([TimeoutError(SECRET_ERROR), SECRET_OUTPUT])
    monkeypatch.setattr(ProviderFactory, "create", staticmethod(lambda *args, **kwargs: provider))

    result = execute_with_retry(
        runtime,
        invocation,
        policy=RetryPolicy(
            max_attempts=2,
            base_delay_ms=0,
            max_delay_ms=0,
        ),
        sleeper=lambda _: None,
        retry_id_factory=lambda: "diag-retry",
    )
    assert result.output == SECRET_OUTPUT
    assert provider.calls == 2
    _bind_run_task(tmp_path, feature, "diag-retry-a002")

    before = _tree_bytes(tmp_path)
    report = build_diagnostics_report(
        tmp_path,
        FEATURE,
        run_id=RUN_ID,
        task_id=TASK_ID,
    )
    after = _tree_bytes(tmp_path)

    assert before == after
    assert report.exit_code == 0
    body = report.to_dict()
    assert body["status"] == "available"
    assert body["selectors"] == {"runId": RUN_ID, "taskId": TASK_ID}
    assert body["context"]["available"] is True
    assert body["context"]["basis"] == "current-repository-state"
    assert body["context"]["metrics"]["combinedPrompt"]["utf8Bytes"] > 0
    assert body["routing"]["available"] is True
    assert body["routing"]["decisionSha256"] == decision.sha256
    assert body["routing"]["selectedProfile"] == decision.selected_profile
    assert body["routing"]["selectionReason"] == decision.selection_reason
    assert [item["attemptId"] for item in body["providerAttempts"]] == ["diag-retry-a002"]
    attempt = body["providerAttempts"][0]
    assert attempt["status"] == "succeeded"
    assert attempt["routingDecisionSha256"] == decision.sha256
    assert body["retryExecutions"][0]["retryId"] == "diag-retry"
    assert body["retryExecutions"][0]["status"] == "succeeded"
    assert body["retryExecutions"][0]["attempts"] == 2
    assert body["audit"]["selectedCount"] == 1
    assert body["audit"]["events"][0]["execution"]["runId"] == RUN_ID
    serialized = report.to_json()
    assert SECRET_OUTPUT not in serialized
    assert SECRET_ERROR not in serialized
    assert "bounded diagnostic context" not in serialized


def test_diagnostics_cli_is_read_only_and_never_invokes_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _workspace(tmp_path)
    runtime = AgentRuntime(tmp_path)
    decision, invocation = _routed_invocation(runtime)
    provider = _SequencedProvider(["ok"])
    monkeypatch.setattr(ProviderFactory, "create", staticmethod(lambda *args, **kwargs: provider))
    execute_routed_invocation(runtime, RoutedInvocation(decision, invocation))
    assert provider.calls == 1
    before = _tree_bytes(tmp_path)

    def must_not_execute(*args, **kwargs):
        pytest.fail("sdai diagnostics must not construct or execute a provider")

    monkeypatch.setattr(ProviderFactory, "create", staticmethod(must_not_execute))
    code = sdai_main(["diagnostics", FEATURE, "--json", "--path", str(tmp_path)])
    captured = capsys.readouterr()
    after = _tree_bytes(tmp_path)

    assert code == 0
    assert before == after
    payload = json.loads(captured.out)
    assert payload["apiVersion"] == "sdai.diagnostics/v1"
    assert payload["routing"]["available"] is True
    assert payload["providerAttempts"][0]["status"] == "succeeded"
    assert not captured.err


def test_historical_routing_hash_is_reported_without_fabricated_reason(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _workspace(tmp_path)
    runtime = AgentRuntime(tmp_path)
    decision, invocation = _routed_invocation(runtime)
    provider = _SequencedProvider(["ok"])
    monkeypatch.setattr(ProviderFactory, "create", staticmethod(lambda *args, **kwargs: provider))
    # Execute the invocation directly to model a pre-0.20.8 routed attempt: provider
    # diagnostics contain only the routing hash and no persisted decision document.
    runtime.execute_invocation(invocation)

    report = build_diagnostics_report(tmp_path, FEATURE)

    assert report.exit_code == 0
    assert report.body["status"] == "partial"
    assert report.body["routing"] == {
        "available": False,
        "decisionSha256": decision.sha256,
        "reason": "historical-routing-document-not-persisted",
    }
    assert "routing-document-hash-only" in report.body["partialReasons"]


def test_no_execution_data_returns_no_data_without_creating_files(tmp_path: Path) -> None:
    _workspace(tmp_path)
    before = _tree_bytes(tmp_path)

    report = build_diagnostics_report(tmp_path, FEATURE)

    assert report.exit_code == 3
    assert report.body["status"] == "no-data"
    assert report.body["providerAttempts"] == []
    assert report.body["retryExecutions"] == []
    assert _tree_bytes(tmp_path) == before


def test_tampered_provider_diagnostic_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    feature = _workspace(tmp_path)
    runtime = AgentRuntime(tmp_path)
    decision, invocation = _routed_invocation(runtime)
    provider = _SequencedProvider(["ok"])
    monkeypatch.setattr(ProviderFactory, "create", staticmethod(lambda *args, **kwargs: provider))
    execute_routed_invocation(runtime, RoutedInvocation(decision, invocation))
    terminal = _terminal_path(feature, next((feature / ".sdai" / "diagnostics" / "provider").iterdir()).name)
    payload = json.loads(terminal.read_text(encoding="utf-8"))
    payload["status"] = "failed"
    terminal.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")

    with pytest.raises(DiagnosticsError, match="SHA-256 verification"):
        build_diagnostics_report(tmp_path, FEATURE)


def test_tampered_routing_document_fails_closed_before_it_can_explain_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    feature = _workspace(tmp_path)
    runtime = AgentRuntime(tmp_path)
    decision, invocation = _routed_invocation(runtime)
    provider = _SequencedProvider(["ok"])
    monkeypatch.setattr(ProviderFactory, "create", staticmethod(lambda *args, **kwargs: provider))
    execute_routed_invocation(runtime, RoutedInvocation(decision, invocation))
    routing_file = feature / ".sdai" / "diagnostics" / "routing" / f"{decision.sha256.removeprefix('sha256:')}.json"
    payload = json.loads(routing_file.read_text(encoding="utf-8"))
    payload["decision"]["selection_reason"] = "tampered"
    routing_file.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(RoutingDiagnosticError, match="document SHA-256"):
        build_diagnostics_report(tmp_path, FEATURE)


def test_routing_diagnostic_persistence_is_idempotent_and_conflict_fails_before_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    feature = _workspace(tmp_path)
    runtime = AgentRuntime(tmp_path)
    decision, invocation = _routed_invocation(runtime)
    provider = _SequencedProvider(["ok"])
    monkeypatch.setattr(ProviderFactory, "create", staticmethod(lambda *args, **kwargs: provider))

    first = persist_routing_decision(tmp_path, FEATURE, decision)
    second = persist_routing_decision(tmp_path, FEATURE, decision)
    assert first == second
    assert load_routing_diagnostic(tmp_path, FEATURE, decision.sha256) is not None
    assert provider.calls == 0

    assert first is not None
    first.write_text("{}\n", encoding="utf-8")
    with pytest.raises(RoutingDiagnosticError, match="conflicts"):
        execute_routed_invocation(runtime, RoutedInvocation(decision, invocation))
    assert provider.calls == 0
