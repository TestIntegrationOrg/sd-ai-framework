from __future__ import annotations

import json
from pathlib import Path

import pytest

from sdai.agent_platform import (
    AgentProgressEvent,
    AgentRuntime,
    Capability,
    ExecutionMode,
    ProviderProgressEvent,
)
from sdai.entrypoint import main as sdai_main
from sdai.providers.base import ProviderCapabilities
from sdai.providers.cli import ProviderExecutionError, ProviderTimeoutError
from sdai.providers.factory import ProviderFactory
from sdai.scaffold import init_project
from sdai.v05_scaffold import install_v05_scaffold
from sdai.workflow_templates import install_current_workflows


FEATURE = "LIVE-PROGRESS-009"
PRIVATE_OUTPUT = "private-provider-output-must-not-appear-in-progress"


class _ProgressProvider:
    def diagnostic_capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            heartbeat=True,
            cancellation=True,
            first_output_timing=True,
        )

    def complete_observable(self, *, system, prompt, cancellation, progress) -> str:
        progress(
            ProviderProgressEvent(
                "started",
                "subprocess-started",
                process_id=4242,
                elapsed_seconds=0.1,
            )
        )
        progress(
            ProviderProgressEvent(
                "heartbeat",
                "subprocess-running",
                process_id=4242,
                elapsed_seconds=2.0,
            )
        )
        progress(
            ProviderProgressEvent(
                "first-output",
                "stdout-observed",
                process_id=4242,
                elapsed_seconds=2.1,
            )
        )
        return PRIVATE_OUTPUT


class _FailureProvider(_ProgressProvider):
    def __init__(self, error: Exception) -> None:
        self.error = error

    def complete_observable(self, *, system, prompt, cancellation, progress) -> str:
        progress(
            ProviderProgressEvent(
                "started",
                "subprocess-started",
                process_id=4343,
                elapsed_seconds=0.1,
            )
        )
        raise self.error


def _project(root: Path) -> None:
    init_project(root)
    install_v05_scaffold(root)
    install_current_workflows(root)
    feature = root / "specs" / "changes" / FEATURE
    feature.mkdir(parents=True, exist_ok=True)
    (feature / "requirements.md").write_text(
        "# Requirements\n\n- FR-009: Show safe live provider progress.\n",
        encoding="utf-8",
    )


def test_step_run_verbose_shows_safe_progress_before_completion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _project(tmp_path)
    monkeypatch.setattr(
        ProviderFactory,
        "create",
        staticmethod(lambda *args, **kwargs: _ProgressProvider()),
    )

    exit_code = sdai_main(
        [
            "step",
            "run",
            FEATURE,
            "architecture-review",
            "--workflow",
            "agentic",
            "--verbose",
            "--path",
            str(tmp_path),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "status=starting" in captured.err
    assert "status=running pid=4242 elapsed=0.1s" in captured.err
    assert "status=heartbeat pid=4242 elapsed=2.0s" in captured.err
    assert "status=first-output pid=4242 elapsed=2.1s" in captured.err
    assert "status=completed" in captured.err
    assert captured.err.index("status=heartbeat") < captured.err.index("status=completed")
    assert "profile=claude" in captured.err
    assert "agent=architect" in captured.err
    assert "mode=advisory" in captured.err
    assert "timeout=900s" in captured.err
    assert "prompt_bytes=" in captured.err
    assert "encoding=utf-8" in captured.err
    assert PRIVATE_OUTPUT not in captured.err
    assert "SYSTEM" not in captured.err
    assert "TASK" not in captured.err


@pytest.mark.parametrize(
    ("error", "expected_category"),
    [
        (ProviderTimeoutError("test-cli", 3), "timeout"),
        (ProviderExecutionError("test-cli failed with exit code 7"), "provider-execution"),
    ],
)
def test_runtime_progress_distinguishes_timeout_from_nonzero_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    expected_category: str,
) -> None:
    _project(tmp_path)
    runtime = AgentRuntime(tmp_path)
    invocation = runtime.build_invocation(
        FEATURE,
        Capability.ARCHITECTURE,
        profile_name="claude",
        agent_name="architect",
        mode=ExecutionMode.ADVISORY,
    )
    monkeypatch.setattr(
        ProviderFactory,
        "create",
        staticmethod(lambda *args, **kwargs: _FailureProvider(error)),
    )
    observed: list[AgentProgressEvent] = []

    with pytest.raises(type(error)):
        runtime.execute_invocation(invocation, progress=observed.append)

    assert [event.phase for event in observed[:3]] == [
        "starting",
        "provider-ready",
        "process-started",
    ]
    assert observed[-1].phase == "failed"
    assert observed[-1].failure_category == expected_category
    assert observed[2].process_id == 4343
    assert all(PRIVATE_OUTPUT not in repr(event) for event in observed)
    diagnostic_events = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in (tmp_path / "specs" / "changes" / FEATURE / ".sdai" / "diagnostics" / "provider").glob(
            "*/*.json"
        )
    ]
    terminal = next(event for event in diagnostic_events if event["phase"] == "failed")
    assert terminal["failure"]["category"] == expected_category
