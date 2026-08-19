from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
import statistics
from types import MappingProxyType
from typing import Mapping

from sdai.agent_platform.provider_diagnostics import PROVIDER_DIAGNOSTIC_API_VERSION
from sdai.models import FeatureContext, validate_feature_id
from sdai.path_safety import PathSafetyError, ensure_within_project


PROVIDER_HEALTH_API_VERSION = "sdai.provider-health/v1"
_MAX_TERMINAL_EVENTS = 5_000
_HEALTH_STATES = frozenset({"healthy", "degraded", "unavailable", "unknown"})


class ProviderHealthError(RuntimeError):
    pass


def _fail(code: str, message: str) -> ProviderHealthError:
    return ProviderHealthError(f"{code}: {message}")


def _canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise _fail("SDAI-PROVIDER-HEALTH-001", "health value is not canonical JSON") from exc


def _sha(value: object) -> str:
    return "sha256:" + sha256(_canonical_bytes(value)).hexdigest()


def _safe(root: Path, candidate: Path, *, label: str) -> Path:
    try:
        safe = ensure_within_project(root, candidate, label=label)
    except PathSafetyError as exc:
        raise _fail("SDAI-PROVIDER-HEALTH-002", f"{label} escapes project root") from exc
    resolved_root = root.resolve()
    current = resolved_root
    try:
        relative = safe.relative_to(resolved_root)
    except ValueError:
        relative = safe.resolve(strict=False).relative_to(resolved_root)
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise _fail("SDAI-PROVIDER-HEALTH-002", f"{label} contains a symlink component")
    return safe


@dataclass(frozen=True)
class ProviderHealthSignal:
    state: str = "unknown"
    samples: int = 0
    successes: int = 0
    failures: int = 0
    p50_total_ns: int | None = None
    latest_status: str | None = None
    source_sha256: str | None = None

    def __post_init__(self) -> None:
        state = str(self.state).strip().casefold()
        if state not in _HEALTH_STATES:
            raise ProviderHealthError(f"unsupported provider health state: {self.state!r}")
        object.__setattr__(self, "state", state)
        for label, value in (
            ("samples", self.samples),
            ("successes", self.successes),
            ("failures", self.failures),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ProviderHealthError(f"{label} must be a non-negative integer")
        if self.successes + self.failures > self.samples:
            raise ProviderHealthError("success/failure counts cannot exceed samples")
        if self.p50_total_ns is not None and (
            not isinstance(self.p50_total_ns, int)
            or isinstance(self.p50_total_ns, bool)
            or self.p50_total_ns < 0
        ):
            raise ProviderHealthError("p50_total_ns must be a non-negative integer or null")
        if self.latest_status is not None and self.latest_status not in {
            "succeeded",
            "failed",
            "cancelled",
        }:
            raise ProviderHealthError("latest_status is invalid")
        if self.source_sha256 is not None and (
            not isinstance(self.source_sha256, str)
            or not self.source_sha256.startswith("sha256:")
            or len(self.source_sha256) != 71
        ):
            raise ProviderHealthError("source_sha256 is invalid")

    def as_dict(self) -> dict[str, object]:
        return {
            "state": self.state,
            "samples": self.samples,
            "successes": self.successes,
            "failures": self.failures,
            "p50TotalNs": self.p50_total_ns,
            "latestStatus": self.latest_status,
            "sourceSha256": self.source_sha256,
        }


@dataclass(frozen=True)
class ProviderHealthSnapshot:
    feature_id: str
    signals: Mapping[str, ProviderHealthSignal]

    def __post_init__(self) -> None:
        feature = validate_feature_id(self.feature_id)
        normalized: dict[str, ProviderHealthSignal] = {}
        if not isinstance(self.signals, Mapping):
            raise ProviderHealthError("signals must be a mapping")
        for key, signal in self.signals.items():
            if not isinstance(key, str) or not key.strip():
                raise ProviderHealthError("health signal keys must be non-empty text")
            if not isinstance(signal, ProviderHealthSignal):
                raise ProviderHealthError("health signal values must be ProviderHealthSignal")
            normalized[key.strip()] = signal
        object.__setattr__(self, "feature_id", feature)
        object.__setattr__(self, "signals", MappingProxyType(dict(sorted(normalized.items()))))

    def as_dict(self) -> dict[str, object]:
        return {
            "apiVersion": PROVIDER_HEALTH_API_VERSION,
            "featureId": self.feature_id,
            "signals": {key: value.as_dict() for key, value in self.signals.items()},
        }

    @property
    def sha256(self) -> str:
        return _sha(self.as_dict())

    def to_json(self) -> str:
        payload = self.as_dict()
        payload["sha256"] = self.sha256
        return _canonical_bytes(payload).decode("utf-8")


def _terminal_payload(root: Path, path: Path) -> dict[str, object]:
    safe = _safe(root, path, label="provider diagnostic health input")
    if not safe.is_file():
        raise _fail("SDAI-PROVIDER-HEALTH-003", "health input must be a regular file")
    try:
        raw = safe.read_text(encoding="utf-8")
        payload = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise _fail("SDAI-PROVIDER-HEALTH-003", "provider diagnostic health input is invalid") from exc
    if not isinstance(payload, dict) or payload.get("apiVersion") != PROVIDER_DIAGNOSTIC_API_VERSION:
        raise _fail("SDAI-PROVIDER-HEALTH-003", "unsupported provider diagnostic health input")
    if payload.get("phase") not in {"completed", "failed", "cancelled"}:
        raise _fail("SDAI-PROVIDER-HEALTH-003", "health input is not a terminal diagnostic")
    for key in ("attemptId", "profile", "provider", "occurredAt", "status", "timing"):
        if key not in payload:
            raise _fail("SDAI-PROVIDER-HEALTH-003", f"terminal diagnostic is missing {key}")
    return payload


def _profile_signal(records: list[dict[str, object]]) -> ProviderHealthSignal:
    ordered = sorted(records, key=lambda item: (str(item["occurredAt"]), str(item["attemptId"])))
    considered = [item for item in ordered if item.get("status") != "cancelled"]
    if not considered:
        source = _sha([{"attemptId": item["attemptId"], "sha256": item.get("sha256")} for item in ordered])
        return ProviderHealthSignal(
            state="unknown",
            samples=len(ordered),
            latest_status=str(ordered[-1]["status"]) if ordered else None,
            source_sha256=source,
        )
    successes = [item for item in considered if item.get("status") == "succeeded"]
    failures = [item for item in considered if item.get("status") == "failed"]
    successful_latencies: list[int] = []
    for item in successes:
        timing = item.get("timing")
        if isinstance(timing, dict):
            total = timing.get("totalNs")
            if isinstance(total, int) and not isinstance(total, bool) and total >= 0:
                successful_latencies.append(total)
    recent = considered[-2:]
    unavailable = len(recent) == 2 and all(
        item.get("status") == "failed"
        and isinstance(item.get("failure"), dict)
        and item["failure"].get("category") == "provider-unavailable"
        for item in recent
    )
    ratio = len(successes) / len(considered)
    latest_status = str(ordered[-1]["status"])
    if unavailable:
        state = "unavailable"
    elif considered[-1].get("status") == "succeeded" and ratio >= 0.75:
        state = "healthy"
    else:
        state = "degraded"
    p50 = int(statistics.median(successful_latencies)) if successful_latencies else None
    source = _sha(
        [
            {
                "attemptId": item["attemptId"],
                "sha256": item.get("sha256"),
                "status": item.get("status"),
            }
            for item in ordered
        ]
    )
    return ProviderHealthSignal(
        state=state,
        samples=len(ordered),
        successes=len(successes),
        failures=len(failures),
        p50_total_ns=p50,
        latest_status=latest_status,
        source_sha256=source,
    )


def build_provider_health_snapshot(
    project_root: Path,
    feature_id: str,
    *,
    max_attempts_per_profile: int = 20,
) -> ProviderHealthSnapshot:
    """Summarize persisted diagnostics into an explicit, non-authoritative routing snapshot."""
    if not isinstance(max_attempts_per_profile, int) or isinstance(max_attempts_per_profile, bool) or not 1 <= max_attempts_per_profile <= 100:
        raise ProviderHealthError("max_attempts_per_profile must be between 1 and 100")
    root = project_root.resolve()
    feature = validate_feature_id(feature_id)
    workspace = FeatureContext(root, feature).feature_dir
    if not workspace.exists():
        return ProviderHealthSnapshot(feature, {})
    diagnostic_root = _safe(
        root,
        workspace / ".sdai" / "diagnostics" / "provider",
        label="provider diagnostic health directory",
    )
    if not diagnostic_root.exists():
        return ProviderHealthSnapshot(feature, {})
    if not diagnostic_root.is_dir():
        raise _fail("SDAI-PROVIDER-HEALTH-002", "provider diagnostic health path is not a directory")
    terminals: list[Path] = []
    for attempt in sorted(diagnostic_root.iterdir(), key=lambda item: item.name):
        safe_attempt = _safe(root, attempt, label="provider diagnostic health attempt")
        if not safe_attempt.is_dir():
            continue
        for path in sorted(safe_attempt.glob("*.json"), key=lambda item: item.name):
            if path.name.endswith(("-completed.json", "-failed.json", "-cancelled.json")):
                terminals.append(path)
                if len(terminals) > _MAX_TERMINAL_EVENTS:
                    raise _fail("SDAI-PROVIDER-HEALTH-004", "too many terminal provider diagnostics")
    by_profile: dict[str, list[dict[str, object]]] = {}
    for path in terminals:
        payload = _terminal_payload(root, path)
        profile = str(payload["profile"])
        by_profile.setdefault(profile, []).append(payload)
    signals: dict[str, ProviderHealthSignal] = {}
    for profile, records in sorted(by_profile.items()):
        ordered = sorted(records, key=lambda item: (str(item["occurredAt"]), str(item["attemptId"])))
        signals[profile] = _profile_signal(ordered[-max_attempts_per_profile:])
    return ProviderHealthSnapshot(feature, signals)


__all__ = [
    "PROVIDER_HEALTH_API_VERSION",
    "ProviderHealthError",
    "ProviderHealthSignal",
    "ProviderHealthSnapshot",
    "build_provider_health_snapshot",
]
