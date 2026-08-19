from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import sys
from threading import Timer

import pytest

from sdai.agent_platform import (
    AgentRuntime,
    Capability,
    ProviderCancellationToken,
    ProviderCancelledError,
    ProviderProgressEvent,
    RetryPolicy,
    RoutingRequest,
    execute_with_retry,
    route_model,
)
from sdai.agent_platform.audit import AgentAuditError, AgentAuditRecorder
from sdai.agent_platform.routing_diagnostics import (
    persist_routing_decision,
    routing_decision_document_sha256,
)
from sdai.context_explain import build_context_explanation
from sdai.diagnostics import build_diagnostics_report
from sdai.providers.base import ProviderCapabilities
from sdai.providers.cli import CliProvider
from sdai.providers.factory import ProviderFactory
from sdai.scaffold import init_project, upgrade_project
from sdai.version_entrypoint import main as sdai_main


FEATURE = "V020-RELEASE-GATE"
SECRET_CONTEXT = "context-secret-must-not-escape-v020"
SECRET_OUTPUT = "provider-secret-output-must-not-escape-v020"
SECRET_ERROR = "provider-secret-error-must-not-escape-v020"


class _ObservableRetryProvider:
    def __init__(self) -> None:
        self.calls = 0

    def diagnostic_capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            streaming=True,
            heartbeat=True,
            cancellation=True,
            first_output_timing=True,
        )

    def complete_observable(self, *, system, prompt, cancellation, progress) -> str:
        self.calls += 1
        cancellation.raise_if_cancelled()
        progress(ProviderProgressEvent("first-output", "release-provider-output-observed"))
        progress(ProviderProgressEvent("heartbeat", "release-provider-running"))
        if self.calls == 1:
            raise TimeoutError(SECRET_ERROR)
        return SECRET_OUTPUT


class _SingleCallProvider:
    def __init__(self) -> None:
        self.calls = 0

    def complete(self, *, system: str, prompt: str) -> str:
        self.calls += 1
        return SECRET_OUTPUT


def _project(tmp_path: Path) -> tuple[Path, Path]:
    # Unicode root/path handling is part of the 0.20 cross-platform contract.
    root = tmp_path / "sdai-v020-工程-é"
    init_project(root)
    # Exercise the normal upgrade path as part of the release journey. A current
    # scaffold may have nothing to add, which is still a successful upgrade check.
    upgrade_project(root)
    feature = root / "specs" / "changes" / FEATURE
    feature.mkdir(parents=True, exist_ok=True)
    (feature / "requirements.md").write_text(
        "# Requirements\n\n"
        f"- FR-259: Release-gate context must be minimal and private. marker={SECRET_CONTEXT}\n",
        encoding="utf-8",
        newline="\n",
    )
    architecture = feature / "architecture" / "architecture.md"
    architecture.parent.mkdir(parents=True, exist_ok=True)
    architecture.write_text(
        "# Architecture\n\nADR-259 keeps provider evidence deterministic and read-only.\n",
        encoding="utf-8",
        newline="\n",
    )
    source = root / "src" / "release_gate_worker.py"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(
        "# FR-259\n\ndef release_gate() -> None:\n    pass\n",
        encoding="utf-8",
        newline="\n",
    )
    return root, feature


def _routed_invocation(runtime: AgentRuntime, *, context_chars: int):
    request = RoutingRequest(
        semantic_role="developer",
        capability=Capability.CODING,
        risk="standard",
        complexity="high",
        context_chars=context_chars,
        max_cost_class="premium",
        optimization="cost",
    )
    first = route_model(runtime.project_root, request, environ={})
    second = route_model(runtime.project_root, request, environ={})
    assert first.to_json() == second.to_json()
    assert first.sha256 == second.sha256
    assert first.selected_profile is not None
    selected = [item for item in first.candidates if item.profile == first.selected_profile]
    assert len(selected) == 1
    assert selected[0].eligible is True

    invocation = runtime.build_explicit_context_invocation(
        FEATURE,
        Capability.CODING,
        "bounded release-gate execution context",
        profile_name=first.selected_profile,
    )
    return first, replace(invocation, routing_decision=first.to_json())


def _provider_events(feature: Path, attempt_id: str) -> list[dict[str, object]]:
    directory = feature / ".sdai" / "diagnostics" / "provider" / attempt_id
    return [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(directory.glob("*.json"))
    ]


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_v020_end_to_end_context_routing_retry_diagnostics_and_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, feature = _project(tmp_path)
    runtime = AgentRuntime(root)

    plan = runtime.build_context_plan(FEATURE, Capability.CODING)
    assert plan.feature_id == FEATURE
    assert any(item.source.endswith("requirements.md") for item in plan.files)
    assert any(item.source == "src/release_gate_worker.py" for item in plan.files)
    assert SECRET_CONTEXT not in plan.to_json()

    explanation = build_context_explanation(root, FEATURE, Capability.CODING)
    explained = explanation.as_dict()
    assert explained["contextPlan"]["planSha256"] == plan.sha256
    assert explained["metrics"]["combinedPrompt"]["utf8Bytes"] > 0
    assert SECRET_CONTEXT not in explanation.to_json()

    decision, invocation = _routed_invocation(
        runtime,
        context_chars=explained["metrics"]["combinedPrompt"]["chars"],
    )
    persisted_route = persist_routing_decision(root, FEATURE, decision)
    assert persisted_route is not None
    routing_document_sha = routing_decision_document_sha256(decision)
    assert persisted_route.name == f"{routing_document_sha.removeprefix('sha256:')}.json"

    provider = _ObservableRetryProvider()
    monkeypatch.setattr(
        ProviderFactory,
        "create",
        staticmethod(lambda *args, **kwargs: provider),
    )
    result = execute_with_retry(
        runtime,
        invocation,
        policy=RetryPolicy(
            max_attempts=2,
            base_delay_ms=0,
            max_delay_ms=0,
        ),
        sleeper=lambda _: None,
        retry_id_factory=lambda: "v020-release-retry",
    )
    assert result.output == SECRET_OUTPUT
    assert provider.calls == 2

    first_events = _provider_events(feature, "v020-release-retry-a001")
    second_events = _provider_events(feature, "v020-release-retry-a002")
    assert [event["phase"] for event in first_events] == [
        "started",
        "provider-ready",
        "first-output",
        "heartbeat",
        "failed",
    ]
    assert [event["phase"] for event in second_events] == [
        "started",
        "provider-ready",
        "first-output",
        "heartbeat",
        "completed",
    ]
    assert all(
        event.get("routingDecisionDocumentSha256") == routing_document_sha
        for event in (*first_events, *second_events)
    )

    before = _tree_bytes(root)
    report = build_diagnostics_report(root, FEATURE)
    after = _tree_bytes(root)
    assert before == after
    assert report.exit_code == 0
    body = report.to_dict()
    assert body["status"] == "available"
    assert body["context"]["available"] is True
    assert body["context"]["basis"] == "current-repository-state"
    assert body["routing"]["available"] is True
    assert body["routing"]["routingDecisionDocumentSha256"] == routing_document_sha
    assert body["routing"]["decisionSha256"] == decision.sha256
    assert body["retryExecutions"][0]["status"] == "succeeded"
    assert body["retryExecutions"][0]["attempts"] == 2
    assert [item["status"] for item in body["providerAttempts"]] == ["failed", "succeeded"]
    assert body["audit"]["eventCount"] == 4
    assert body["audit"]["ledgerHeadSha256"].startswith("sha256:")

    serialized = report.to_json()
    assert SECRET_CONTEXT not in serialized
    assert SECRET_OUTPUT not in serialized
    assert SECRET_ERROR not in serialized
    assert "bounded release-gate execution context" not in serialized

    # The operator CLI must be a pure read: changing ProviderFactory now must not
    # affect diagnostics and the project bytes must remain identical.
    def provider_must_not_run(*args, **kwargs):
        raise AssertionError("sdai diagnostics must never invoke a provider")

    monkeypatch.setattr(ProviderFactory, "create", staticmethod(provider_must_not_run))
    before_cli = _tree_bytes(root)
    assert sdai_main(["diagnostics", FEATURE, "--json", "--path", str(root)]) == 0
    cli_body = json.loads(capsys.readouterr().out)
    assert cli_body["reportSha256"] == body["reportSha256"]
    assert _tree_bytes(root) == before_cli


def test_v020_cli_utf8_first_output_heartbeat_and_cancellation(tmp_path: Path) -> None:
    root, _ = _project(tmp_path)
    private_output = "私有-output-✓"
    successful = CliProvider(
        [
            sys.executable,
            "-c",
            (
                "import sys,time; "
                f"sys.stdout.buffer.write({private_output!r}.encode('utf-8')); "
                "sys.stdout.buffer.flush(); time.sleep(0.12)"
            ),
        ],
        cwd=root,
        provider_name="v020-cli",
        timeout_seconds=5,
        heartbeat_interval_seconds=0.03,
        poll_interval_seconds=0.01,
    )
    observed: list[ProviderProgressEvent] = []
    output = successful.complete_observable(
        system="system-✓",
        prompt="task-工程",
        cancellation=ProviderCancellationToken(),
        progress=observed.append,
    )
    assert output == private_output
    assert any(item.kind == "first-output" for item in observed)
    assert any(item.kind == "heartbeat" for item in observed)
    assert private_output not in json.dumps([item.__dict__ for item in observed], ensure_ascii=False)

    cancellable = CliProvider(
        [sys.executable, "-c", "import time; time.sleep(5)"],
        cwd=root,
        provider_name="v020-cli-cancel",
        timeout_seconds=10,
        heartbeat_interval_seconds=0.03,
        poll_interval_seconds=0.01,
    )
    token = ProviderCancellationToken()
    cancellation_events: list[ProviderProgressEvent] = []
    timer = Timer(0.15, token.cancel)
    timer.start()
    try:
        with pytest.raises(ProviderCancelledError):
            cancellable.complete_observable(
                system="system",
                prompt="task",
                cancellation=token,
                progress=cancellation_events.append,
            )
    finally:
        timer.cancel()
    assert any(item.kind == "heartbeat" for item in cancellation_events)


def test_v020_audit_persistence_failure_never_double_executes_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _ = _project(tmp_path)
    runtime = AgentRuntime(root)
    explanation = build_context_explanation(root, FEATURE, Capability.CODING)
    _, invocation = _routed_invocation(
        runtime,
        context_chars=explanation.metrics["combinedPrompt"].chars,
    )
    provider = _SingleCallProvider()
    monkeypatch.setattr(
        ProviderFactory,
        "create",
        staticmethod(lambda *args, **kwargs: provider),
    )

    def fail_audit_success(self, *args, **kwargs):
        raise AgentAuditError("SDAI-AGENT-AUDIT-005: forced-release-gate-persistence-failure")

    monkeypatch.setattr(AgentAuditRecorder, "succeeded", fail_audit_success)
    with pytest.raises(AgentAuditError):
        execute_with_retry(
            runtime,
            invocation,
            policy=RetryPolicy(max_attempts=3),
            sleeper=lambda _: pytest.fail("audit persistence ambiguity must never retry"),
            retry_id_factory=lambda: "v020-audit-failure",
        )
    assert provider.calls == 1
