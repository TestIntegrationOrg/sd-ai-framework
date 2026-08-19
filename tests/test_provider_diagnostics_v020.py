from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

from sdai.agent_platform import AgentRuntime, Capability, ExecutionMode
from sdai.agent_platform.provider_diagnostics import (
    PROVIDER_DIAGNOSTIC_API_VERSION,
    ProviderDiagnosticError,
    ProviderDiagnosticRecorder,
)
from sdai.providers.base import ProviderCapabilities
from sdai.providers.factory import ProviderFactory
from sdai.scaffold import init_project


FEATURE = "PROVIDER-DIAG-020"
SECRET_PROMPT = "prompt-secret-never-persist"
SECRET_OUTPUT = "output-secret-never-persist"
SECRET_ERROR = "provider-error-secret-never-persist"


class _FakeClock:
    def __init__(self, monotonic_values: list[int]) -> None:
        self._values = iter(monotonic_values)
        self._utc_index = 0

    def monotonic_ns(self) -> int:
        return next(self._values)

    def utc_now(self) -> datetime:
        value = datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc) + timedelta(
            seconds=self._utc_index
        )
        self._utc_index += 1
        return value


class _FakeProvider:
    def __init__(self, *, output: str = SECRET_OUTPUT, error: BaseException | None = None) -> None:
        self.output = output
        self.error = error
        self.calls = 0

    def diagnostic_capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities()

    def complete(self, *, system: str, prompt: str) -> str:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.output


def _workspace(root: Path) -> Path:
    init_project(root)
    feature = root / "specs" / "changes" / FEATURE
    feature.mkdir(parents=True, exist_ok=True)
    (feature / "requirements.md").write_text(
        "# Requirements\n\n- FR-253: Record provider timing without secrets.\n",
        encoding="utf-8",
    )
    return feature


def _invocation(runtime: AgentRuntime):
    return runtime.build_explicit_context_invocation(
        FEATURE,
        Capability.CODING,
        SECRET_PROMPT,
        mode=ExecutionMode.ADVISORY,
    )


def _diagnostic_events(feature: Path, attempt_id: str) -> list[dict[str, object]]:
    directory = feature / ".sdai" / "diagnostics" / "provider" / attempt_id
    return [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(directory.glob("*.json"))
    ]


def _audit_events(feature: Path) -> list[dict[str, object]]:
    path = feature / ".sdai" / "audit" / "events.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_success_records_exact_timing_capabilities_and_audit_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    feature = _workspace(tmp_path)
    provider = _FakeProvider()
    monkeypatch.setattr(
        ProviderFactory,
        "create",
        staticmethod(lambda *args, **kwargs: provider),
    )
    runtime = AgentRuntime(
        tmp_path,
        diagnostic_clock=_FakeClock([100, 250, 1000]),
        diagnostic_id_factory=lambda: "attempt-001",
    )

    result = runtime.execute_invocation(_invocation(runtime))

    assert result.output == SECRET_OUTPUT
    assert provider.calls == 1
    events = _diagnostic_events(feature, "attempt-001")
    assert [event["phase"] for event in events] == ["started", "provider-ready", "completed"]
    assert all(event["apiVersion"] == PROVIDER_DIAGNOSTIC_API_VERSION for event in events)
    assert events[0]["timing"] == {
        "startupNs": None,
        "invocationNs": None,
        "totalNs": 0,
        "firstOutput": {
            "available": False,
            "elapsedNs": None,
            "reason": "provider-not-created",
        },
    }
    assert events[1]["timing"]["startupNs"] == 150
    assert events[1]["timing"]["totalNs"] == 150
    assert events[2]["timing"]["startupNs"] == 150
    assert events[2]["timing"]["invocationNs"] == 750
    assert events[2]["timing"]["totalNs"] == 900
    assert events[2]["timing"]["firstOutput"]["reason"] == "provider-complete-interface"
    assert events[2]["providerCapabilities"] == {
        "streaming": False,
        "heartbeat": False,
        "cancellation": False,
        "firstOutputTiming": False,
    }
    assert events[2]["profile"]
    assert events[2]["provider"]
    assert events[2]["costClass"] in {"economy", "standard", "premium"}
    assert events[2]["auditStartSha256"].startswith("sha256:")

    persisted = "\n".join(json.dumps(item, sort_keys=True) for item in events)
    assert SECRET_PROMPT not in persisted
    assert SECRET_OUTPUT not in persisted

    audits = _audit_events(feature)
    assert [item["action"]["kind"] for item in audits] == [
        "agent.execution.started",
        "agent.execution.succeeded",
    ]
    terminal_bindings = audits[-1]["bindings"]
    diagnostic = next(
        item
        for item in terminal_bindings
        if "/.sdai/diagnostics/provider/attempt-001/002-completed.json" in item["source"]
    )
    completed_file = (
        feature
        / ".sdai"
        / "diagnostics"
        / "provider"
        / "attempt-001"
        / "002-completed.json"
    )
    from hashlib import sha256

    assert diagnostic["sha256"] == "sha256:" + sha256(completed_file.read_bytes()).hexdigest()


def test_provider_failure_is_sanitized_and_linked_without_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    feature = _workspace(tmp_path)
    provider = _FakeProvider(error=RuntimeError(SECRET_ERROR))
    monkeypatch.setattr(
        ProviderFactory,
        "create",
        staticmethod(lambda *args, **kwargs: provider),
    )
    runtime = AgentRuntime(
        tmp_path,
        diagnostic_clock=_FakeClock([10, 20, 35]),
        diagnostic_id_factory=lambda: "attempt-failed",
    )

    with pytest.raises(RuntimeError, match=SECRET_ERROR):
        runtime.execute_invocation(_invocation(runtime))

    assert provider.calls == 1
    events = _diagnostic_events(feature, "attempt-failed")
    assert [event["phase"] for event in events] == ["started", "provider-ready", "failed"]
    failed = events[-1]
    assert failed["status"] == "failed"
    assert failed["failure"] == {"category": "provider-failure", "type": "RuntimeError"}
    assert failed["timing"]["startupNs"] == 10
    assert failed["timing"]["invocationNs"] == 15
    serialized = json.dumps(events, sort_keys=True)
    assert SECRET_ERROR not in serialized
    assert SECRET_PROMPT not in serialized

    audits = _audit_events(feature)
    assert audits[-1]["action"]["kind"] == "agent.execution.failed"
    assert audits[-1]["metadata"]["failureType"] == "RuntimeError"
    assert SECRET_ERROR not in json.dumps(audits, sort_keys=True)
    assert any(
        "/.sdai/diagnostics/provider/attempt-failed/002-failed.json" in item["source"]
        for item in audits[-1]["bindings"]
    )


def test_provider_factory_failure_records_startup_failure_without_provider_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    feature = _workspace(tmp_path)
    calls = 0

    def fail_factory(*args: object, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        raise FileNotFoundError(SECRET_ERROR)

    monkeypatch.setattr(ProviderFactory, "create", staticmethod(fail_factory))
    runtime = AgentRuntime(
        tmp_path,
        diagnostic_clock=_FakeClock([1000, 1300]),
        diagnostic_id_factory=lambda: "attempt-startup",
    )

    with pytest.raises(FileNotFoundError, match=SECRET_ERROR):
        runtime.execute_invocation(_invocation(runtime))

    assert calls == 1
    events = _diagnostic_events(feature, "attempt-startup")
    assert [event["phase"] for event in events] == ["started", "failed"]
    assert events[-1]["failure"] == {
        "category": "provider-not-found",
        "type": "FileNotFoundError",
    }
    assert events[-1]["timing"]["startupNs"] == 300
    assert events[-1]["timing"]["invocationNs"] is None
    assert events[-1]["timing"]["totalNs"] == 300
    assert events[-1]["timing"]["firstOutput"]["reason"] == "provider-not-created"
    assert SECRET_ERROR not in json.dumps(events, sort_keys=True)


def test_diagnostic_start_failure_prevents_provider_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _workspace(tmp_path)
    provider_factory_calls = 0

    def factory(*args: object, **kwargs: object) -> object:
        nonlocal provider_factory_calls
        provider_factory_calls += 1
        return _FakeProvider()

    def fail_start(self: ProviderDiagnosticRecorder, *, audit_start_sha256: str | None):
        raise ProviderDiagnosticError("SDAI-PROVIDER-DIAG-TEST: start persistence failed")

    monkeypatch.setattr(ProviderFactory, "create", staticmethod(factory))
    monkeypatch.setattr(ProviderDiagnosticRecorder, "start", fail_start)
    runtime = AgentRuntime(tmp_path, diagnostic_id_factory=lambda: "attempt-start-fail")

    with pytest.raises(ProviderDiagnosticError, match="start persistence failed"):
        runtime.execute_invocation(_invocation(runtime))

    assert provider_factory_calls == 0


def test_terminal_diagnostic_failure_does_not_reexecute_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    feature = _workspace(tmp_path)
    provider = _FakeProvider()
    monkeypatch.setattr(
        ProviderFactory,
        "create",
        staticmethod(lambda *args, **kwargs: provider),
    )

    def fail_completed(self: ProviderDiagnosticRecorder):
        raise ProviderDiagnosticError("SDAI-PROVIDER-DIAG-TEST: terminal persistence failed")

    monkeypatch.setattr(ProviderDiagnosticRecorder, "completed", fail_completed)
    runtime = AgentRuntime(
        tmp_path,
        diagnostic_clock=_FakeClock([1, 2]),
        diagnostic_id_factory=lambda: "attempt-terminal-fail",
    )

    with pytest.raises(ProviderDiagnosticError, match="terminal persistence failed"):
        runtime.execute_invocation(_invocation(runtime))

    assert provider.calls == 1
    events = _diagnostic_events(feature, "attempt-terminal-fail")
    assert [event["phase"] for event in events] == ["started", "provider-ready"]
    audits = _audit_events(feature)
    assert audits[-1]["action"]["kind"] == "agent.execution.failed"
    assert audits[-1]["metadata"]["failureType"] == "ProviderDiagnosticError"
    assert SECRET_OUTPUT not in json.dumps(audits, sort_keys=True)
