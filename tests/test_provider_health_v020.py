from __future__ import annotations

import json
from pathlib import Path

import pytest

from sdai.agent_platform import (
    ProviderHealthError,
    build_provider_health_snapshot,
)
from sdai.agent_platform.provider_diagnostics import ProviderDiagnosticEvent
from sdai.providers.base import ProviderCapabilities
from sdai.scaffold import init_project


FEATURE = "ROUTING-HEALTH-020"


def _workspace(root: Path) -> Path:
    init_project(root)
    feature = root / "specs" / "changes" / FEATURE
    feature.mkdir(parents=True, exist_ok=True)
    return feature


def _terminal(
    feature: Path,
    *,
    attempt: str,
    profile: str,
    provider: str,
    occurred_at: str,
    status: str,
    total_ns: int,
    failure_category: str | None = None,
) -> Path:
    phase = "completed" if status == "succeeded" else ("cancelled" if status == "cancelled" else "failed")
    event = ProviderDiagnosticEvent(
        attempt_id=attempt,
        sequence=2,
        phase=phase,
        occurred_at=occurred_at,
        feature_id=FEATURE,
        capability="coding",
        mode="advisory",
        profile=profile,
        provider=provider,
        model=None,
        cost_class="standard",
        semantic_agent=None,
        routing_document_sha256=None,
        audit_start_sha256=None,
        provider_capabilities=ProviderCapabilities(),
        startup_ns=10,
        invocation_ns=max(0, total_ns - 10),
        total_ns=total_ns,
        first_output={"available": False, "elapsedNs": None, "reason": "provider-complete-interface"},
        status=status,
        failure=(
            {"category": failure_category or "provider-failure", "type": "SyntheticError"}
            if status == "failed"
            else None
        ),
    )
    root = feature / ".sdai" / "diagnostics" / "provider" / attempt
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"002-{phase}.json"
    path.write_text(event.to_json(), encoding="utf-8", newline="\n")
    return path


def test_health_snapshot_is_deterministic_and_uses_success_latency(tmp_path: Path) -> None:
    feature = _workspace(tmp_path)
    _terminal(
        feature,
        attempt="a1",
        profile="fast",
        provider="provider-a",
        occurred_at="2026-08-19T12:00:00.000000Z",
        status="succeeded",
        total_ns=100,
    )
    _terminal(
        feature,
        attempt="a2",
        profile="fast",
        provider="provider-a",
        occurred_at="2026-08-19T12:01:00.000000Z",
        status="succeeded",
        total_ns=300,
    )
    _terminal(
        feature,
        attempt="a3",
        profile="fast",
        provider="provider-a",
        occurred_at="2026-08-19T12:02:00.000000Z",
        status="succeeded",
        total_ns=200,
    )
    _terminal(
        feature,
        attempt="a4",
        profile="fast",
        provider="provider-a",
        occurred_at="2026-08-19T12:03:00.000000Z",
        status="cancelled",
        total_ns=50,
    )

    first = build_provider_health_snapshot(tmp_path, FEATURE)
    second = build_provider_health_snapshot(tmp_path, FEATURE)

    assert first.to_json() == second.to_json()
    assert first.sha256 == second.sha256
    signal = first.signals["fast"]
    assert signal.state == "healthy"
    assert signal.samples == 4
    assert signal.successes == 3
    assert signal.failures == 0
    assert signal.p50_total_ns == 200
    assert signal.latest_status == "cancelled"


def test_two_recent_unavailability_failures_mark_profile_unavailable(tmp_path: Path) -> None:
    feature = _workspace(tmp_path)
    for index in range(2):
        _terminal(
            feature,
            attempt=f"down-{index}",
            profile="down",
            provider="provider-down",
            occurred_at=f"2026-08-19T12:0{index}:00.000000Z",
            status="failed",
            total_ns=100,
            failure_category="provider-unavailable",
        )

    snapshot = build_provider_health_snapshot(tmp_path, FEATURE)

    signal = snapshot.signals["down"]
    assert signal.state == "unavailable"
    assert signal.failures == 2
    assert signal.p50_total_ns is None


def test_cancelled_only_attempts_do_not_make_provider_unhealthy(tmp_path: Path) -> None:
    feature = _workspace(tmp_path)
    _terminal(
        feature,
        attempt="cancel-only",
        profile="cancelled",
        provider="provider-c",
        occurred_at="2026-08-19T12:00:00.000000Z",
        status="cancelled",
        total_ns=25,
    )

    signal = build_provider_health_snapshot(tmp_path, FEATURE).signals["cancelled"]

    assert signal.state == "unknown"
    assert signal.samples == 1
    assert signal.successes == 0
    assert signal.failures == 0


def test_tampered_terminal_diagnostic_is_rejected_before_health_can_affect_routing(
    tmp_path: Path,
) -> None:
    feature = _workspace(tmp_path)
    path = _terminal(
        feature,
        attempt="tampered",
        profile="fast",
        provider="provider-a",
        occurred_at="2026-08-19T12:00:00.000000Z",
        status="succeeded",
        total_ns=100,
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["status"] = "failed"
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")

    with pytest.raises(ProviderHealthError, match="SHA-256 verification"):
        build_provider_health_snapshot(tmp_path, FEATURE)
