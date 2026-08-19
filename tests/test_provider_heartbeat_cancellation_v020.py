from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
from threading import Timer

import pytest

from sdai.agent_platform import (
    AgentRuntime,
    Capability,
    ExecutionMode,
    ProviderCancellationToken,
    ProviderCancelledError,
    ProviderProgressEvent,
)
from sdai.providers.base import ProviderCapabilities
from sdai.providers.cli import CliProvider
from sdai.providers.factory import ProviderFactory
from sdai.scaffold import init_project


FEATURE = "PROVIDER-CONTROL-020"
SECRET_OUTPUT = "secret-output-must-not-enter-progress"


class _FakeClock:
    def __init__(self, values: list[int]) -> None:
        self._values = iter(values)
        self._utc_index = 0

    def monotonic_ns(self) -> int:
        return next(self._values)

    def utc_now(self) -> datetime:
        value = datetime(2026, 8, 19, 13, 0, tzinfo=timezone.utc) + timedelta(
            seconds=self._utc_index
        )
        self._utc_index += 1
        return value


class _ObservableProvider:
    def __init__(self, *, cancel_during_call: bool = False) -> None:
        self.calls = 0
        self.cancel_during_call = cancel_during_call

    def diagnostic_capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            streaming=True,
            heartbeat=True,
            cancellation=True,
            first_output_timing=True,
        )

    def complete_observable(self, *, system, prompt, cancellation, progress) -> str:
        self.calls += 1
        progress(ProviderProgressEvent("first-output", "provider-stream-started"))
        progress(ProviderProgressEvent("heartbeat", "provider-running"))
        if self.cancel_during_call:
            cancellation.cancel()
            cancellation.raise_if_cancelled()
        return SECRET_OUTPUT


class _LegacyProvider:
    def __init__(self) -> None:
        self.calls = 0

    def diagnostic_capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities()

    def complete(self, *, system: str, prompt: str) -> str:
        self.calls += 1
        return "legacy-output"


def _workspace(root: Path) -> Path:
    init_project(root)
    feature = root / "specs" / "changes" / FEATURE
    feature.mkdir(parents=True, exist_ok=True)
    (feature / "requirements.md").write_text(
        "# Requirements\n\n- FR-254: Observable provider heartbeat and cancellation.\n",
        encoding="utf-8",
    )
    return feature


def _invocation(runtime: AgentRuntime):
    return runtime.build_explicit_context_invocation(
        FEATURE,
        Capability.CODING,
        "bounded explicit context",
        mode=ExecutionMode.ADVISORY,
    )


def _events(feature: Path, attempt: str) -> list[dict[str, object]]:
    root = feature / ".sdai" / "diagnostics" / "provider" / attempt
    return [json.loads(path.read_text(encoding="utf-8")) for path in sorted(root.glob("*.json"))]


def test_observable_provider_records_first_output_heartbeat_and_dynamic_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    feature = _workspace(tmp_path)
    provider = _ObservableProvider()
    monkeypatch.setattr(ProviderFactory, "create", staticmethod(lambda *args, **kwargs: provider))
    runtime = AgentRuntime(
        tmp_path,
        diagnostic_clock=_FakeClock([100, 200, 350, 500, 900]),
        diagnostic_id_factory=lambda: "progress-attempt",
    )

    result = runtime.execute_invocation(_invocation(runtime))

    assert result.output == SECRET_OUTPUT
    assert provider.calls == 1
    events = _events(feature, "progress-attempt")
    assert [event["phase"] for event in events] == [
        "started",
        "provider-ready",
        "first-output",
        "heartbeat",
        "completed",
    ]
    assert [event["sequence"] for event in events] == [0, 1, 2, 3, 4]
    assert events[2]["timing"]["firstOutput"] == {
        "available": True,
        "elapsedNs": 150,
        "reason": "provider-reported",
    }
    assert events[2]["progressReason"] == "provider-stream-started"
    assert events[3]["progressReason"] == "provider-running"
    assert "progressReason" not in events[0]
    assert "progressReason" not in events[1]
    assert "progressReason" not in events[-1]
    assert events[-1]["timing"]["firstOutput"]["available"] is True
    serialized = json.dumps(events, sort_keys=True)
    assert SECRET_OUTPUT not in serialized


def test_complete_only_provider_keeps_original_three_event_shape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    feature = _workspace(tmp_path)
    provider = _LegacyProvider()
    monkeypatch.setattr(ProviderFactory, "create", staticmethod(lambda *args, **kwargs: provider))
    runtime = AgentRuntime(
        tmp_path,
        diagnostic_clock=_FakeClock([10, 20, 30]),
        diagnostic_id_factory=lambda: "legacy-attempt",
    )

    result = runtime.execute_invocation(_invocation(runtime))

    assert result.output == "legacy-output"
    assert provider.calls == 1
    attempt = feature / ".sdai" / "diagnostics" / "provider" / "legacy-attempt"
    assert [path.name for path in sorted(attempt.glob("*.json"))] == [
        "000-started.json",
        "001-provider-ready.json",
        "002-completed.json",
    ]
    events = _events(feature, "legacy-attempt")
    assert [event["phase"] for event in events] == ["started", "provider-ready", "completed"]
    assert all("progressReason" not in event for event in events)


def test_pre_cancelled_token_prevents_provider_call_and_records_cancelled_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    feature = _workspace(tmp_path)
    provider = _ObservableProvider()
    monkeypatch.setattr(ProviderFactory, "create", staticmethod(lambda *args, **kwargs: provider))
    token = ProviderCancellationToken()
    token.cancel()
    runtime = AgentRuntime(
        tmp_path,
        diagnostic_clock=_FakeClock([10, 20, 30]),
        diagnostic_id_factory=lambda: "pre-cancelled",
    )

    with pytest.raises(ProviderCancelledError, match="cancelled-by-request"):
        runtime.execute_invocation(_invocation(runtime), cancellation=token)

    assert provider.calls == 0
    events = _events(feature, "pre-cancelled")
    assert [event["phase"] for event in events] == ["started", "provider-ready", "cancelled"]
    assert events[-1]["status"] == "cancelled"
    assert events[-1]["progressReason"] == "cancelled-by-request"
    assert events[-1]["failure"] == {
        "category": "cancelled",
        "type": "ProviderCancelledError",
    }


def test_cancellation_after_progress_is_terminal_and_never_retries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    feature = _workspace(tmp_path)
    provider = _ObservableProvider(cancel_during_call=True)
    monkeypatch.setattr(ProviderFactory, "create", staticmethod(lambda *args, **kwargs: provider))
    runtime = AgentRuntime(
        tmp_path,
        diagnostic_clock=_FakeClock([100, 200, 300, 400, 500]),
        diagnostic_id_factory=lambda: "cancel-progress",
    )

    with pytest.raises(ProviderCancelledError):
        runtime.execute_invocation(
            _invocation(runtime), cancellation=ProviderCancellationToken()
        )

    assert provider.calls == 1
    events = _events(feature, "cancel-progress")
    assert [event["phase"] for event in events] == [
        "started",
        "provider-ready",
        "first-output",
        "heartbeat",
        "cancelled",
    ]
    assert events[-1]["failure"]["category"] == "cancelled"
    audit_path = feature / ".sdai" / "audit" / "events.jsonl"
    audit = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]
    assert [item["action"]["kind"] for item in audit] == [
        "agent.execution.started",
        "agent.execution.failed",
    ]
    assert audit[-1]["metadata"]["failureType"] == "ProviderCancelledError"


def test_cli_provider_emits_synthetic_heartbeat_and_can_cancel_subprocess(tmp_path: Path) -> None:
    provider = CliProvider(
        [sys.executable, "-c", "import time; time.sleep(5); print('done')"],
        cwd=tmp_path,
        provider_name="test-cli",
        timeout_seconds=10,
        heartbeat_interval_seconds=0.03,
        poll_interval_seconds=0.01,
    )
    token = ProviderCancellationToken()
    observed: list[ProviderProgressEvent] = []
    timer = Timer(0.15, token.cancel)
    timer.start()
    try:
        with pytest.raises(ProviderCancelledError):
            provider.complete_observable(
                system="system",
                prompt="task",
                cancellation=token,
                progress=observed.append,
            )
    finally:
        timer.cancel()

    capabilities = provider.diagnostic_capabilities()
    assert capabilities.streaming is False
    assert capabilities.heartbeat is True
    assert capabilities.cancellation is True
    assert capabilities.first_output_timing is True
    assert any(event.kind == "heartbeat" for event in observed)
    assert all(event.reason == "subprocess-running" for event in observed)


def test_cli_provider_reports_first_output_without_exposing_output_in_progress(tmp_path: Path) -> None:
    provider = CliProvider(
        [
            sys.executable,
            "-c",
            "import sys,time; sys.stdout.write('private-chunk'); sys.stdout.flush(); time.sleep(0.15); print('-done')",
        ],
        cwd=tmp_path,
        provider_name="test-cli",
        timeout_seconds=5,
        heartbeat_interval_seconds=0.05,
        poll_interval_seconds=0.01,
    )
    observed: list[ProviderProgressEvent] = []

    output = provider.complete_observable(
        system="system",
        prompt="task",
        cancellation=ProviderCancellationToken(),
        progress=observed.append,
    )

    assert output == "private-chunk-done"
    first = [event for event in observed if event.kind == "first-output"]
    assert len(first) == 1
    assert first[0].reason == "stdout-observed"
    assert "private-chunk" not in json.dumps([event.__dict__ for event in observed])
