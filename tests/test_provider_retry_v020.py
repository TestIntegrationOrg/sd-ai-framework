from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from sdai.agent_platform import (
    AgentRuntime,
    Capability,
    ExecutionMode,
    ProviderCancellationToken,
    ProviderCancelledError,
    ProviderDiagnosticError,
    ProviderFailureCategory,
    ProviderRetryError,
    ProviderRetryRecorder,
    RetryPolicy,
    classify_provider_failure,
    decide_retry,
    execute_with_retry,
    retry_delay_ms,
)
from sdai.policy import PolicyError
from sdai.providers.cli import ProviderExecutionError
from sdai.providers.factory import ProviderFactory
from sdai.scaffold import init_project


FEATURE = "PROVIDER-RETRY-020"
SECRET = "TOP-SECRET-provider-message"


class _HttpError(RuntimeError):
    def __init__(self, status_code: int, message: str = SECRET) -> None:
        super().__init__(message)
        self.status_code = status_code


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
        "# Requirements\n\n- FR-255: Deterministic retry policy.\n",
        encoding="utf-8",
    )
    return feature


def _invocation(runtime: AgentRuntime, *, mode: ExecutionMode = ExecutionMode.ADVISORY):
    return runtime.build_explicit_context_invocation(
        FEATURE,
        Capability.CODING,
        "bounded explicit retry context",
        mode=mode,
    )


def _retry_files(feature: Path, retry_id: str) -> list[Path]:
    return sorted((feature / ".sdai" / "diagnostics" / "retry" / retry_id).glob("*.json"))


def test_failure_taxonomy_is_bounded_and_conservative() -> None:
    assert classify_provider_failure(subprocess.TimeoutExpired(["tool"], 1)).category == ProviderFailureCategory.TIMEOUT
    assert classify_provider_failure(subprocess.TimeoutExpired(["tool"], 1)).retryable is True

    cancelled = classify_provider_failure(ProviderCancelledError())
    assert cancelled.category == ProviderFailureCategory.CANCELLED
    assert cancelled.retryable is False

    policy = classify_provider_failure(PolicyError(SECRET))
    assert policy.category == ProviderFailureCategory.POLICY
    assert policy.retryable is False

    observability = classify_provider_failure(ProviderDiagnosticError(f"SDAI-PROVIDER-DIAG-003: {SECRET}"))
    assert observability.category == ProviderFailureCategory.OBSERVABILITY
    assert observability.retryable is False

    rate_limit = classify_provider_failure(_HttpError(429))
    assert rate_limit.category == ProviderFailureCategory.RATE_LIMIT
    assert rate_limit.retryable is True

    auth = classify_provider_failure(_HttpError(401))
    assert auth.category == ProviderFailureCategory.AUTHENTICATION
    assert auth.retryable is False

    unavailable = classify_provider_failure(_HttpError(503))
    assert unavailable.category == ProviderFailureCategory.PROVIDER_UNAVAILABLE
    assert unavailable.retryable is True

    missing = classify_provider_failure(FileNotFoundError(SECRET))
    assert missing.category == ProviderFailureCategory.PROVIDER_UNAVAILABLE
    assert missing.retryable is False

    malformed = classify_provider_failure(ProviderExecutionError(f"invalid UTF-8 {SECRET}"))
    assert malformed.category == ProviderFailureCategory.MALFORMED_OUTPUT
    assert malformed.retryable is False

    subprocess_failure = classify_provider_failure(ProviderExecutionError(f"exit code 7 {SECRET}"))
    assert subprocess_failure.category == ProviderFailureCategory.LOCAL_SUBPROCESS
    assert subprocess_failure.retryable is False

    for result in (policy, observability, rate_limit, auth, unavailable, missing, malformed, subprocess_failure):
        assert SECRET not in json.dumps(result.as_dict(), sort_keys=True)


def test_backoff_and_jitter_are_deterministic_and_bounded() -> None:
    policy = RetryPolicy(
        max_attempts=5,
        base_delay_ms=100,
        max_delay_ms=1_000,
        multiplier=2,
        jitter_basis_points=1_000,
    )
    first = [
        retry_delay_ms(
            policy,
            failed_attempt=attempt,
            category=ProviderFailureCategory.TIMEOUT,
            seed="sha256:" + "a" * 64,
        )
        for attempt in range(1, 5)
    ]
    second = [
        retry_delay_ms(
            policy,
            failed_attempt=attempt,
            category=ProviderFailureCategory.TIMEOUT,
            seed="sha256:" + "a" * 64,
        )
        for attempt in range(1, 5)
    ]
    assert first == second
    assert all(0 <= value <= policy.max_delay_ms for value in first)

    no_jitter = RetryPolicy(
        max_attempts=4,
        base_delay_ms=100,
        max_delay_ms=1_000,
        multiplier=2,
        jitter_basis_points=0,
    )
    assert [
        retry_delay_ms(
            no_jitter,
            failed_attempt=attempt,
            category=ProviderFailureCategory.TIMEOUT,
            seed="stable",
        )
        for attempt in range(1, 4)
    ] == [100, 200, 400]


def test_workspace_write_never_retries_transient_failure() -> None:
    policy = RetryPolicy(max_attempts=3)
    classification = classify_provider_failure(TimeoutError("transient"))
    decision = decide_retry(
        policy,
        classification,
        failed_attempt=1,
        mode=ExecutionMode.WORKSPACE_WRITE,
        seed="stable",
        diagnostic_attempt_id="retry-a001",
    )
    assert decision.action == "fail"
    assert decision.reason_code == "workspace-write-side-effect-ambiguity"
    assert decision.delay_ms == 0


def test_transient_failures_retry_as_separate_governed_attempts_then_succeed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    feature = _workspace(tmp_path)
    provider = _SequencedProvider([TimeoutError(SECRET), ConnectionError(SECRET), "success"])
    monkeypatch.setattr(ProviderFactory, "create", staticmethod(lambda *args, **kwargs: provider))
    runtime = AgentRuntime(tmp_path)
    sleeps: list[float] = []

    result = execute_with_retry(
        runtime,
        _invocation(runtime),
        policy=RetryPolicy(
            max_attempts=3,
            base_delay_ms=10,
            max_delay_ms=100,
            multiplier=2,
            jitter_basis_points=0,
        ),
        sleeper=sleeps.append,
        retry_id_factory=lambda: "retry-success",
    )

    assert result.output == "success"
    assert provider.calls == 3
    assert sleeps == [0.01, 0.02]
    retry_files = _retry_files(feature, "retry-success")
    assert [path.name for path in retry_files] == [
        "000-policy.json",
        "001-decision.json",
        "002-decision.json",
        "summary.json",
    ]
    decisions = [
        json.loads((feature / ".sdai" / "diagnostics" / "retry" / "retry-success" / name).read_text(encoding="utf-8"))
        for name in ("001-decision.json", "002-decision.json")
    ]
    assert [item["action"] for item in decisions] == ["retry", "retry"]
    assert [item["diagnosticAttemptId"] for item in decisions] == [
        "retry-success-a001",
        "retry-success-a002",
    ]
    summary = json.loads(retry_files[-1].read_text(encoding="utf-8"))
    assert summary["status"] == "succeeded"
    assert summary["attempts"] == 3

    diagnostic_root = feature / ".sdai" / "diagnostics" / "provider"
    assert (diagnostic_root / "retry-success-a001").is_dir()
    assert (diagnostic_root / "retry-success-a002").is_dir()
    assert (diagnostic_root / "retry-success-a003").is_dir()

    audit_path = feature / ".sdai" / "audit" / "events.jsonl"
    audit = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]
    assert [item["action"]["kind"] for item in audit] == [
        "agent.execution.started",
        "agent.execution.failed",
        "agent.execution.started",
        "agent.execution.failed",
        "agent.execution.started",
        "agent.execution.succeeded",
    ]
    persisted = "\n".join(path.read_text(encoding="utf-8") for path in retry_files)
    assert SECRET not in persisted


def test_cancellation_is_never_retried(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    feature = _workspace(tmp_path)
    provider = _SequencedProvider([ProviderCancelledError(), "must-not-run"])
    monkeypatch.setattr(ProviderFactory, "create", staticmethod(lambda *args, **kwargs: provider))
    runtime = AgentRuntime(tmp_path)
    sleeps: list[float] = []

    with pytest.raises(ProviderCancelledError):
        execute_with_retry(
            runtime,
            _invocation(runtime),
            policy=RetryPolicy(max_attempts=3, base_delay_ms=1, max_delay_ms=10),
            cancellation=ProviderCancellationToken(),
            sleeper=sleeps.append,
            retry_id_factory=lambda: "retry-cancelled",
        )

    assert provider.calls == 1
    assert sleeps == []
    summary = json.loads(
        (feature / ".sdai" / "diagnostics" / "retry" / "retry-cancelled" / "summary.json").read_text(encoding="utf-8")
    )
    assert summary["status"] == "failed"
    assert summary["attempts"] == 1
    assert summary["finalClassification"]["category"] == "cancelled"


def test_diagnostic_persistence_failure_does_not_retry_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _workspace(tmp_path)
    provider = _SequencedProvider(["provider-ran", "must-not-run"])
    monkeypatch.setattr(ProviderFactory, "create", staticmethod(lambda *args, **kwargs: provider))

    from sdai.agent_platform.provider_diagnostics import ProviderDiagnosticRecorder

    def fail_completed(self):
        raise ProviderDiagnosticError(f"SDAI-PROVIDER-DIAG-003: {SECRET}")

    monkeypatch.setattr(ProviderDiagnosticRecorder, "completed", fail_completed)
    runtime = AgentRuntime(tmp_path)

    with pytest.raises(ProviderDiagnosticError):
        execute_with_retry(
            runtime,
            _invocation(runtime),
            policy=RetryPolicy(max_attempts=3),
            sleeper=lambda _: pytest.fail("observability failure must not back off/retry"),
            retry_id_factory=lambda: "retry-diag-failure",
        )

    assert provider.calls == 1


def test_retry_decision_persistence_failure_prevents_second_provider_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _workspace(tmp_path)
    provider = _SequencedProvider([TimeoutError(SECRET), "must-not-run"])
    monkeypatch.setattr(ProviderFactory, "create", staticmethod(lambda *args, **kwargs: provider))

    def fail_decision(self, decision):
        raise ProviderRetryError("SDAI-PROVIDER-RETRY-003: forced-persistence-failure")

    monkeypatch.setattr(ProviderRetryRecorder, "decision", fail_decision)
    runtime = AgentRuntime(tmp_path)

    with pytest.raises(ProviderRetryError):
        execute_with_retry(
            runtime,
            _invocation(runtime),
            policy=RetryPolicy(max_attempts=3, base_delay_ms=1, max_delay_ms=10),
            sleeper=lambda _: pytest.fail("retry evidence must persist before backoff"),
            retry_id_factory=lambda: "retry-record-failure",
        )

    assert provider.calls == 1


def test_escalation_is_terminal_observer_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _workspace(tmp_path)
    provider = _SequencedProvider([PolicyError(SECRET), "must-not-run"])
    monkeypatch.setattr(ProviderFactory, "create", staticmethod(lambda *args, **kwargs: provider))
    observed = []
    runtime = AgentRuntime(tmp_path)

    with pytest.raises(PolicyError):
        execute_with_retry(
            runtime,
            _invocation(runtime),
            policy=RetryPolicy(max_attempts=3),
            retry_id_factory=lambda: "retry-escalate",
            escalation=observed.append,
        )

    assert provider.calls == 1
    assert len(observed) == 1
    assert observed[0].action == "fail"
    assert observed[0].classification.category == ProviderFailureCategory.POLICY
