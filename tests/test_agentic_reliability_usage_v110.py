from __future__ import annotations

from io import StringIO
import json
from pathlib import Path
import sys
import time

import pytest

from sdai.agent_platform import AgentRuntime, Capability, ExecutionMode
from sdai.entrypoint import main as sdai_main
from sdai.orchestrator import Orchestrator
from sdai.providers.base import (
    HostProviderContext,
    NestedExecutionSupport,
    ProviderCapabilities,
    ProviderResult,
    ProviderUsage,
)
from sdai.providers.cli import (
    CliProvider,
    ProviderFirstOutputTimeoutError,
    ProviderIdleOutputTimeoutError,
)
from sdai.providers.factory import ProviderFactory
from sdai.scaffold import init_project
from sdai.usage import load_usage_attempts, usage_report
from sdai.workflow_selection import (
    resolve_feature_workflow,
    select_workflow,
    workflow_choices,
)
from sdai.workflow_templates import install_current_workflows


FEATURE = "RELIABLE-AGENTIC-110"


def _project(root: Path) -> None:
    init_project(root)
    install_current_workflows(root)
    feature = root / "specs" / "changes" / FEATURE
    feature.mkdir(parents=True, exist_ok=True)
    (feature / "requirements.md").write_text(
        "# Requirements\n\n- FR-110: Prove reliable agent execution.\n",
        encoding="utf-8",
    )


def test_workflow_selector_lists_builtins_and_accepts_name(tmp_path: Path) -> None:
    _project(tmp_path)
    choices = workflow_choices(tmp_path)
    assert {item.name for item in choices} == {
        "light",
        "standard",
        "critical",
        "agentic",
        "enterprise",
    }
    output = StringIO()
    selected = select_workflow(
        tmp_path,
        requested=None,
        interactive=True,
        input_stream=StringIO("agentic\n"),
        output_stream=output,
    )
    assert selected == "agentic"
    assert "Agent-enabled" in output.getvalue()
    assert "[default]" in output.getvalue()


def test_feature_noninteractive_uses_configured_default_and_persists_it(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    init_project(tmp_path)
    install_current_workflows(tmp_path)
    code = sdai_main(
        [
            "feature",
            "SELECT-110",
            "--title",
            "Select workflow",
            "--description",
            "Persist workflow selection",
            "--no-input",
            "--path",
            str(tmp_path),
        ]
    )
    assert code == 0
    assert "Resolved workflow 'standard'" in capsys.readouterr().out
    assert resolve_feature_workflow(tmp_path, "SELECT-110", None) == "standard"


class _SelectedProvider:
    calls = 0

    def diagnostic_capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(nested_execution=NestedExecutionSupport.UNSUPPORTED)

    def complete(self, *, system: str, prompt: str) -> str:
        self.calls += 1
        raise AssertionError("nested selected provider must not be invoked")


class _HostBridge:
    def __init__(self) -> None:
        self.context = HostProviderContext(
            provider="codex",
            profile="codex",
            model="host-model",
            capabilities=frozenset({"architecture"}),
            execution_modes=frozenset({"advisory"}),
            invocation_id="host-110",
        )
        self.calls = 0

    def complete(self, *, system: str, prompt: str) -> ProviderResult:
        self.calls += 1
        return ProviderResult(
            "completed through current host provider",
            ProviderUsage(
                input_tokens=100,
                cached_input_tokens=25,
                output_tokens=20,
                reasoning_tokens=5,
                total_tokens=125,
                measurement="provider-reported",
                complete=True,
                unavailable_reason=None,
            ),
            model="host-model",
            request_id="request-110",
            finish_reason="stop",
        )


def test_unsupported_nested_provider_reuses_registered_current_host_and_records_usage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _project(tmp_path)
    selected = _SelectedProvider()
    bridge = _HostBridge()
    monkeypatch.setattr(
        ProviderFactory,
        "create",
        staticmethod(lambda *args, **kwargs: selected),
    )
    runtime = AgentRuntime(tmp_path, host_bridge=bridge)
    invocation = runtime.build_invocation(
        FEATURE,
        Capability.ARCHITECTURE,
        profile_name="claude",
        agent_name="architect",
        mode=ExecutionMode.ADVISORY,
    )
    from dataclasses import replace

    invocation = replace(invocation, workflow="agentic", step_id="architecture-review")
    readiness = runtime.preflight_invocation(invocation)
    assert readiness.host_reused is True
    assert readiness.provider == "codex"

    result = runtime.execute_invocation(invocation)
    assert selected.calls == 0
    assert bridge.calls == 1
    assert result.host_reused is True
    assert result.requested_provider == "claude"
    assert result.provider == "codex"
    assert result.usage.total_tokens == 125

    attempts = load_usage_attempts(tmp_path, FEATURE, workflow="agentic")
    assert len(attempts) == 1
    assert attempts[0].step_id == "architecture-review"
    assert attempts[0].provider == "codex"
    assert attempts[0].requested_provider == "claude"
    assert attempts[0].host_reused is True
    assert attempts[0].usage.input_tokens == 100
    report = usage_report(FEATURE, attempts)
    assert report["knownTotals"]["totalTokens"] == 125
    assert report["actualTotalKnown"] is True


def test_host_bridge_is_not_used_without_required_capability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _project(tmp_path)
    selected = _SelectedProvider()
    bridge = _HostBridge()
    bridge.context = HostProviderContext(
        provider="codex",
        profile="codex",
        model=None,
        capabilities=frozenset({"coding"}),
        execution_modes=frozenset({"advisory"}),
        invocation_id="host-no-architecture",
    )
    monkeypatch.setattr(
        ProviderFactory,
        "create",
        staticmethod(lambda *args, **kwargs: selected),
    )
    runtime = AgentRuntime(tmp_path, host_bridge=bridge)
    invocation = runtime.build_invocation(
        FEATURE,
        Capability.ARCHITECTURE,
        profile_name="claude",
    )
    readiness = runtime.preflight_invocation(invocation)
    assert readiness.host_reused is False


def test_cli_provider_first_output_timeout_is_separate_from_total_timeout(tmp_path: Path) -> None:
    provider = CliProvider(
        [sys.executable, "-c", "import time; time.sleep(1)"],
        cwd=tmp_path,
        provider_name="silent",
        timeout_seconds=5,
        first_output_timeout_seconds=0.1,
        idle_output_timeout_seconds=5,
        poll_interval_seconds=0.01,
    )
    started = time.monotonic()
    with pytest.raises(ProviderFirstOutputTimeoutError):
        provider.complete(system="system", prompt="task")
    assert time.monotonic() - started < 1


def test_cli_provider_idle_timeout_after_first_output(tmp_path: Path) -> None:
    provider = CliProvider(
        [
            sys.executable,
            "-c",
            "import time; print('started', flush=True); time.sleep(1)",
        ],
        cwd=tmp_path,
        provider_name="idle",
        timeout_seconds=5,
        first_output_timeout_seconds=1,
        idle_output_timeout_seconds=0.1,
        poll_interval_seconds=0.01,
    )
    with pytest.raises(ProviderIdleOutputTimeoutError):
        provider.complete(system="system", prompt="task")


def test_usage_json_preserves_unknown_instead_of_zero(tmp_path: Path) -> None:
    _project(tmp_path)
    diagnostic = (
        tmp_path
        / "specs"
        / "changes"
        / FEATURE
        / ".sdai"
        / "diagnostics"
        / "provider"
        / "legacy-attempt"
    )
    diagnostic.mkdir(parents=True)
    (diagnostic / "002-failed.json").write_text(
        json.dumps(
            {
                "apiVersion": "sdai.provider-diagnostic/v1",
                "attemptId": "legacy-attempt",
                "phase": "failed",
                "featureId": FEATURE,
                "capability": "requirements",
                "profile": "legacy",
                "provider": "custom",
                "model": None,
                "status": "failed",
            }
        ),
        encoding="utf-8",
    )
    report = usage_report(FEATURE, load_usage_attempts(tmp_path, FEATURE))
    assert report["knownTotals"]["totalTokens"] == 0
    assert report["completeCoverage"]["totalTokens"] is False
    assert report["actualTotalKnown"] is False
    assert report["attempts"][0]["usage"]["totalTokens"] is None


class _FallbackProvider:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.calls = 0

    def diagnostic_capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(nested_execution=NestedExecutionSupport.SUPPORTED)

    def complete(self, *, system: str, prompt: str) -> ProviderResult:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return ProviderResult(
            "fallback completed",
            ProviderUsage(
                input_tokens=10,
                output_tokens=4,
                total_tokens=14,
                measurement="provider-reported",
                complete=True,
                unavailable_reason=None,
            ),
        )


def test_route_candidates_fallback_after_no_first_output_and_record_both_attempts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _project(tmp_path)
    (tmp_path / ".sdai" / "routing.yaml").write_text(
        "version: 1\nroutes:\n  architecture: [claude, codex]\n",
        encoding="utf-8",
    )
    first = _FallbackProvider(
        error=ProviderFirstOutputTimeoutError("claude", 1)
    )
    second = _FallbackProvider()
    monkeypatch.setattr(
        ProviderFactory,
        "create",
        staticmethod(
            lambda profile, **kwargs: first if profile.name == "claude" else second
        ),
    )

    result = Orchestrator(tmp_path)._execute_agent(
        FEATURE,
        Capability.ARCHITECTURE,
        profile_name=None,
        agent_name=None,
        mode=ExecutionMode.ADVISORY,
        workflow_name="agentic",
        step_id="architecture-review",
    )

    assert result.provider == "codex"
    assert first.calls == 1
    assert second.calls == 1
    attempts = load_usage_attempts(tmp_path, FEATURE, workflow="agentic")
    by_provider = {attempt.provider: attempt for attempt in attempts}
    assert by_provider["claude"].outcome == "failed"
    assert by_provider["claude"].usage.total_tokens is None
    assert by_provider["codex"].outcome == "succeeded"
    assert by_provider["codex"].usage.total_tokens == 14


def test_host_invocation_chain_prevents_recursive_bridge_reuse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _project(tmp_path)
    selected = _SelectedProvider()
    bridge = _HostBridge()
    key = "codex:codex:host-110"
    bridge.context = HostProviderContext(
        provider="codex",
        profile="codex",
        model=None,
        capabilities=frozenset({"architecture"}),
        execution_modes=frozenset({"advisory"}),
        invocation_id="host-110",
        invocation_chain=(key,),
    )
    monkeypatch.setattr(
        ProviderFactory,
        "create",
        staticmethod(lambda *args, **kwargs: selected),
    )
    readiness = AgentRuntime(tmp_path, host_bridge=bridge).preflight(
        FEATURE,
        Capability.ARCHITECTURE,
        profile_name="claude",
    )
    assert readiness.host_reused is False


def test_usage_cli_emits_machine_readable_report(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _project(tmp_path)
    code = sdai_main(["usage", FEATURE, "--json", "--path", str(tmp_path)])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["apiVersion"] == "sdai.usage-report/v1"
    assert payload["attemptCount"] == 0
    assert payload["actualTotalKnown"] is False
