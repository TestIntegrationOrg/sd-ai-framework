from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Iterable

from sdai.models import FeatureContext, validate_feature_id
from sdai.path_safety import ensure_within_project
from sdai.providers.base import ProviderUsage


USAGE_REPORT_API_VERSION = "sdai.usage-report/v1"
_TOKEN_FIELDS = (
    "inputTokens",
    "cachedInputTokens",
    "outputTokens",
    "reasoningTokens",
    "totalTokens",
)


@dataclass(frozen=True)
class UsageAttempt:
    attempt_id: str
    workflow: str | None
    step_id: str | None
    capability: str
    profile: str
    provider: str
    model: str | None
    outcome: str
    usage: ProviderUsage
    requested_profile: str | None = None
    requested_provider: str | None = None
    host_reused: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "attemptId": self.attempt_id,
            "workflow": self.workflow,
            "stepId": self.step_id,
            "capability": self.capability,
            "profile": self.profile,
            "provider": self.provider,
            "model": self.model,
            "outcome": self.outcome,
            "usage": self.usage.as_dict(),
            "requestedProfile": self.requested_profile,
            "requestedProvider": self.requested_provider,
            "hostReused": self.host_reused,
        }


def _usage(raw: object) -> ProviderUsage:
    if not isinstance(raw, dict):
        return ProviderUsage.unavailable("legacy-diagnostic-without-usage")
    aliases = {
        "input_tokens": "inputTokens",
        "cached_input_tokens": "cachedInputTokens",
        "output_tokens": "outputTokens",
        "reasoning_tokens": "reasoningTokens",
        "total_tokens": "totalTokens",
    }
    values = {target: raw.get(source) for target, source in aliases.items()}
    return ProviderUsage(
        **values,
        measurement=str(raw.get("measurement") or "unavailable"),
        complete=raw.get("complete") is True,
        unavailable_reason=(
            str(raw["unavailableReason"])
            if raw.get("unavailableReason") is not None
            else None
        ),
    )


def _terminal_files(root: Path, feature_id: str) -> Iterable[Path]:
    workspace = FeatureContext(root, feature_id).feature_dir
    diagnostics = ensure_within_project(
        root,
        workspace / ".sdai" / "diagnostics" / "provider",
        label="provider usage diagnostics",
    )
    if not diagnostics.exists():
        return ()
    result: list[Path] = []
    for attempt in sorted(diagnostics.iterdir(), key=lambda item: item.name):
        if not attempt.is_dir() or attempt.is_symlink():
            continue
        for path in sorted(attempt.glob("*.json"), key=lambda item: item.name):
            if path.name.endswith(("-completed.json", "-failed.json", "-cancelled.json")):
                result.append(ensure_within_project(root, path, label="provider usage event"))
    return result


def load_usage_attempts(
    project_root: Path,
    feature_id: str,
    *,
    workflow: str | None = None,
    step_id: str | None = None,
    attempt_id: str | None = None,
) -> tuple[UsageAttempt, ...]:
    root = project_root.resolve()
    feature = validate_feature_id(feature_id)
    attempts: list[UsageAttempt] = []
    for path in _terminal_files(root, feature):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"provider diagnostic must be a JSON object: {path}")
        current_attempt = str(payload.get("attemptId") or "")
        current_workflow = payload.get("workflow")
        current_step = payload.get("stepId")
        if workflow is not None and current_workflow != workflow:
            continue
        if step_id is not None and current_step != step_id:
            continue
        if attempt_id is not None and current_attempt != attempt_id:
            continue
        selection = payload.get("providerSelection")
        selection = selection if isinstance(selection, dict) else {}
        attempts.append(
            UsageAttempt(
                attempt_id=current_attempt,
                workflow=str(current_workflow) if current_workflow else None,
                step_id=str(current_step) if current_step else None,
                capability=str(payload.get("capability") or "-"),
                profile=str(
                    selection.get("effectiveProfile") or payload.get("profile") or "-"
                ),
                provider=str(
                    selection.get("effectiveProvider") or payload.get("provider") or "-"
                ),
                model=str(payload["model"]) if payload.get("model") else None,
                outcome=str(payload.get("status") or "unknown"),
                usage=_usage(payload.get("usage")),
                requested_profile=(
                    str(selection["requestedProfile"])
                    if selection.get("requestedProfile")
                    else None
                ),
                requested_provider=(
                    str(selection["requestedProvider"])
                    if selection.get("requestedProvider")
                    else None
                ),
                host_reused=selection.get("hostReused") is True,
            )
        )
    return tuple(attempts)


def usage_report(feature_id: str, attempts: tuple[UsageAttempt, ...]) -> dict[str, object]:
    known = {field: 0 for field in _TOKEN_FIELDS}
    coverage = {field: True for field in _TOKEN_FIELDS}
    exact = True
    for attempt in attempts:
        raw = attempt.usage.as_dict()
        if attempt.usage.measurement != "provider-reported" or not attempt.usage.complete:
            exact = False
        for field in _TOKEN_FIELDS:
            value = raw[field]
            if value is None:
                coverage[field] = False
            else:
                known[field] += int(value)
    return {
        "apiVersion": USAGE_REPORT_API_VERSION,
        "featureId": validate_feature_id(feature_id),
        "attemptCount": len(attempts),
        "attempts": [attempt.as_dict() for attempt in attempts],
        "knownTotals": known,
        "completeCoverage": coverage,
        "actualTotalKnown": bool(attempts) and exact and coverage["totalTokens"],
    }


__all__ = [
    "USAGE_REPORT_API_VERSION",
    "UsageAttempt",
    "load_usage_attempts",
    "usage_report",
]
