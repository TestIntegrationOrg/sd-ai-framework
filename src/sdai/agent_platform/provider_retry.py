from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import subprocess
import time
from typing import Callable, Mapping
from uuid import uuid4

from sdai.agent_platform.models import AgentExecutionResult, AgentInvocation, ExecutionMode
from sdai.agent_platform.provider_diagnostics import ProviderDiagnosticError
from sdai.agent_platform.runtime import AgentRuntime
from sdai.models import FeatureContext, validate_feature_id
from sdai.path_safety import PathSafetyError, ensure_within_project
from sdai.policy import PolicyError
from sdai.providers.control import ProviderCancellationToken, ProviderCancelledError


PROVIDER_RETRY_API_VERSION = "sdai.provider-retry/v1"
_RETRY_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class ProviderRetryError(RuntimeError):
    """Raised when retry policy/evidence cannot be applied safely."""


def _fail(code: str, message: str) -> ProviderRetryError:
    return ProviderRetryError(f"{code}: {message}")


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
        raise _fail("SDAI-PROVIDER-RETRY-001", "retry value is not canonical JSON") from exc


def _canonical_sha256(value: object) -> str:
    return "sha256:" + sha256(_canonical_bytes(value)).hexdigest()


def _safe_retry_id(value: str) -> str:
    if not isinstance(value, str) or _RETRY_ID.fullmatch(value) is None:
        raise _fail("SDAI-PROVIDER-RETRY-001", "retry id must be a safe portable identifier")
    return value


def _safe_chain(root: Path, candidate: Path, *, label: str) -> Path:
    try:
        safe = ensure_within_project(root, candidate, label=label)
    except PathSafetyError as exc:
        raise _fail("SDAI-PROVIDER-RETRY-002", f"{label} escapes the project workspace") from exc
    resolved_root = root.resolve()
    current = resolved_root
    try:
        relative = safe.relative_to(resolved_root)
    except ValueError:
        relative = safe.resolve(strict=False).relative_to(resolved_root)
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise _fail("SDAI-PROVIDER-RETRY-002", f"{label} contains a symlink component")
    return safe


class ProviderFailureCategory(StrEnum):
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"
    RATE_LIMIT = "rate-limit"
    AUTHENTICATION = "authentication"
    PROVIDER_UNAVAILABLE = "provider-unavailable"
    MALFORMED_OUTPUT = "malformed-output"
    LOCAL_SUBPROCESS = "local-subprocess"
    POLICY = "policy"
    OBSERVABILITY = "observability"
    AUDIT = "audit"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class FailureClassification:
    category: ProviderFailureCategory
    retryable: bool
    reason_code: str
    exception_type: str

    def as_dict(self) -> dict[str, object]:
        return {
            "category": self.category.value,
            "retryable": self.retryable,
            "reasonCode": self.reason_code,
            "exceptionType": self.exception_type,
        }


def _status_code(error: BaseException) -> int | None:
    for name in ("status_code", "status", "code"):
        value = getattr(error, name, None)
        if isinstance(value, int) and 100 <= value <= 599:
            return value
    return None


def _is_audit_persistence_error(error: BaseException) -> bool:
    module = type(error).__module__
    name = type(error).__name__.casefold()
    return module.startswith("sdai.audit") or module.startswith("sdai.agent_platform.audit") or (
        "audit" in name and ("error" in name or "exception" in name)
    )


def classify_provider_failure(error: BaseException) -> FailureClassification:
    """Classify one failure without persisting its potentially sensitive message."""
    if not isinstance(error, BaseException):
        raise TypeError("error must be a BaseException")
    exception_type = type(error).__name__[:128] or "Exception"
    if isinstance(error, ProviderCancelledError):
        return FailureClassification(
            ProviderFailureCategory.CANCELLED, False, "cancelled-by-request", exception_type
        )
    if isinstance(error, ProviderDiagnosticError):
        return FailureClassification(
            ProviderFailureCategory.OBSERVABILITY, False, "diagnostic-persistence", exception_type
        )
    if isinstance(error, PolicyError):
        return FailureClassification(
            ProviderFailureCategory.POLICY, False, "policy-rejected", exception_type
        )
    if _is_audit_persistence_error(error):
        return FailureClassification(
            ProviderFailureCategory.AUDIT, False, "audit-persistence", exception_type
        )
    if isinstance(error, (subprocess.TimeoutExpired, TimeoutError)):
        return FailureClassification(
            ProviderFailureCategory.TIMEOUT, True, "provider-timeout", exception_type
        )
    if isinstance(error, FileNotFoundError):
        return FailureClassification(
            ProviderFailureCategory.PROVIDER_UNAVAILABLE,
            False,
            "provider-executable-not-found",
            exception_type,
        )
    if isinstance(error, PermissionError):
        return FailureClassification(
            ProviderFailureCategory.AUTHENTICATION, False, "permission-denied", exception_type
        )

    status = _status_code(error)
    if status == 429:
        return FailureClassification(
            ProviderFailureCategory.RATE_LIMIT, True, "provider-rate-limit", exception_type
        )
    if status in {401, 403}:
        return FailureClassification(
            ProviderFailureCategory.AUTHENTICATION, False, "provider-authentication", exception_type
        )
    if status in {408, 425, 502, 503, 504}:
        return FailureClassification(
            ProviderFailureCategory.PROVIDER_UNAVAILABLE,
            True,
            "provider-transient-status",
            exception_type,
        )
    if isinstance(error, ConnectionError):
        return FailureClassification(
            ProviderFailureCategory.PROVIDER_UNAVAILABLE,
            True,
            "provider-connection",
            exception_type,
        )

    # ProviderExecutionError intentionally remains an optional dependency here. Its
    # message is inspected transiently only to derive a bounded reason code; raw text
    # is never put into retry evidence.
    if exception_type == "ProviderExecutionError":
        message = str(error).casefold()
        if "429" in message or "rate limit" in message or "too many requests" in message:
            return FailureClassification(
                ProviderFailureCategory.RATE_LIMIT, True, "provider-rate-limit", exception_type
            )
        if any(token in message for token in ("401", "403", "unauthorized", "forbidden", "authentication")):
            return FailureClassification(
                ProviderFailureCategory.AUTHENTICATION,
                False,
                "provider-authentication",
                exception_type,
            )
        if any(
            token in message
            for token in (
                "502",
                "503",
                "504",
                "temporarily unavailable",
                "connection reset",
                "connection refused",
                "service unavailable",
            )
        ):
            return FailureClassification(
                ProviderFailureCategory.PROVIDER_UNAVAILABLE,
                True,
                "provider-transient-execution",
                exception_type,
            )
        if any(token in message for token in ("invalid utf-8", "returned no output", "malformed")):
            return FailureClassification(
                ProviderFailureCategory.MALFORMED_OUTPUT,
                False,
                "provider-malformed-output",
                exception_type,
            )
        return FailureClassification(
            ProviderFailureCategory.LOCAL_SUBPROCESS,
            False,
            "provider-subprocess-failure",
            exception_type,
        )

    lowered_type = exception_type.casefold()
    if "parse" in lowered_type or "malformed" in lowered_type or "decode" in lowered_type:
        return FailureClassification(
            ProviderFailureCategory.MALFORMED_OUTPUT,
            False,
            "provider-malformed-output",
            exception_type,
        )
    return FailureClassification(
        ProviderFailureCategory.UNKNOWN, False, "unclassified-provider-failure", exception_type
    )


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 1
    base_delay_ms: int = 250
    max_delay_ms: int = 5_000
    multiplier: int = 2
    jitter_basis_points: int = 0
    retryable_categories: tuple[ProviderFailureCategory, ...] = (
        ProviderFailureCategory.TIMEOUT,
        ProviderFailureCategory.RATE_LIMIT,
        ProviderFailureCategory.PROVIDER_UNAVAILABLE,
    )

    def __post_init__(self) -> None:
        if not 1 <= self.max_attempts <= 10:
            raise ValueError("max_attempts must be between 1 and 10")
        if not 0 <= self.base_delay_ms <= 60_000:
            raise ValueError("base_delay_ms must be between 0 and 60000")
        if not self.base_delay_ms <= self.max_delay_ms <= 300_000:
            raise ValueError("max_delay_ms must be >= base_delay_ms and <= 300000")
        if not 1 <= self.multiplier <= 10:
            raise ValueError("multiplier must be between 1 and 10")
        if not 0 <= self.jitter_basis_points <= 5_000:
            raise ValueError("jitter_basis_points must be between 0 and 5000")
        if len(set(self.retryable_categories)) != len(self.retryable_categories):
            raise ValueError("retryable_categories must not contain duplicates")

    def as_dict(self) -> dict[str, object]:
        return {
            "apiVersion": PROVIDER_RETRY_API_VERSION,
            "maxAttempts": self.max_attempts,
            "baseDelayMs": self.base_delay_ms,
            "maxDelayMs": self.max_delay_ms,
            "multiplier": self.multiplier,
            "jitterBasisPoints": self.jitter_basis_points,
            "retryableCategories": [item.value for item in self.retryable_categories],
        }

    @property
    def sha256(self) -> str:
        return _canonical_sha256(self.as_dict())


def _retry_seed(invocation: AgentInvocation) -> str:
    payload = {
        "featureId": invocation.feature_id,
        "capability": invocation.capability.value,
        "mode": invocation.mode.value,
        "profile": invocation.profile.name,
        "provider": invocation.profile.provider,
        "model": invocation.profile.model,
        "routingDecisionSha256": (
            "sha256:" + sha256(invocation.routing_decision.encode("utf-8")).hexdigest()
            if invocation.routing_decision
            else None
        ),
    }
    return _canonical_sha256(payload)


def retry_delay_ms(
    policy: RetryPolicy,
    *,
    failed_attempt: int,
    category: ProviderFailureCategory,
    seed: str,
) -> int:
    if not 1 <= failed_attempt <= policy.max_attempts:
        raise ValueError("failed_attempt is outside retry policy bounds")
    delay = min(
        policy.max_delay_ms,
        policy.base_delay_ms * (policy.multiplier ** (failed_attempt - 1)),
    )
    if delay == 0 or policy.jitter_basis_points == 0:
        return delay
    digest = sha256(
        f"{seed}|{failed_attempt}|{category.value}".encode("utf-8")
    ).digest()
    span = policy.jitter_basis_points
    delta_bp = int.from_bytes(digest[:8], "big") % (2 * span + 1) - span
    jittered = delay + (delay * delta_bp // 10_000)
    return max(0, min(policy.max_delay_ms, jittered))


@dataclass(frozen=True)
class RetryDecision:
    failed_attempt: int
    action: str
    delay_ms: int
    reason_code: str
    classification: FailureClassification
    policy_sha256: str
    diagnostic_attempt_id: str

    def as_dict(self) -> dict[str, object]:
        return {
            "apiVersion": PROVIDER_RETRY_API_VERSION,
            "failedAttempt": self.failed_attempt,
            "action": self.action,
            "delayMs": self.delay_ms,
            "reasonCode": self.reason_code,
            "classification": self.classification.as_dict(),
            "policySha256": self.policy_sha256,
            "diagnosticAttemptId": self.diagnostic_attempt_id,
        }

    @property
    def sha256(self) -> str:
        return _canonical_sha256(self.as_dict())


def decide_retry(
    policy: RetryPolicy,
    classification: FailureClassification,
    *,
    failed_attempt: int,
    mode: ExecutionMode,
    seed: str,
    diagnostic_attempt_id: str,
) -> RetryDecision:
    if failed_attempt >= policy.max_attempts:
        action = "fail"
        reason = "attempt-limit-reached"
        delay = 0
    elif mode != ExecutionMode.ADVISORY:
        # Workspace-write attempts can have ambiguous side effects after a transport
        # failure. A future execution-identity contract may safely relax this; 0.20.5
        # deliberately fails closed instead of assuming idempotency.
        action = "fail"
        reason = "workspace-write-side-effect-ambiguity"
        delay = 0
    elif not classification.retryable:
        action = "fail"
        reason = classification.reason_code
        delay = 0
    elif classification.category not in policy.retryable_categories:
        action = "fail"
        reason = "category-not-enabled"
        delay = 0
    else:
        action = "retry"
        reason = classification.reason_code
        delay = retry_delay_ms(
            policy,
            failed_attempt=failed_attempt,
            category=classification.category,
            seed=seed,
        )
    return RetryDecision(
        failed_attempt=failed_attempt,
        action=action,
        delay_ms=delay,
        reason_code=reason,
        classification=classification,
        policy_sha256=policy.sha256,
        diagnostic_attempt_id=_safe_retry_id(diagnostic_attempt_id),
    )


@dataclass
class ProviderRetryRecorder:
    root: Path
    feature_id: str
    retry_id: str
    retry_dir: Path

    @classmethod
    def optional_for(
        cls,
        project_root: Path,
        feature_id: str,
        *,
        id_factory: Callable[[], str],
    ) -> "ProviderRetryRecorder | None":
        root = project_root.resolve()
        feature = validate_feature_id(feature_id)
        workspace = FeatureContext(root, feature).feature_dir
        if not workspace.exists() and not workspace.is_symlink():
            return None
        if workspace.is_symlink() or not workspace.is_dir():
            raise _fail("SDAI-PROVIDER-RETRY-002", "feature workspace is missing or unsafe")
        retry_id = _safe_retry_id(id_factory())
        retry_root = _safe_chain(
            root,
            workspace / ".sdai" / "diagnostics" / "retry",
            label="provider retry directory",
        )
        retry_root.mkdir(parents=True, exist_ok=True)
        retry_dir = _safe_chain(
            root, retry_root / retry_id, label="provider retry execution directory"
        )
        try:
            retry_dir.mkdir()
        except FileExistsError as exc:
            raise _fail("SDAI-PROVIDER-RETRY-003", "provider retry id already exists") from exc
        except OSError as exc:
            raise _fail("SDAI-PROVIDER-RETRY-003", "unable to create provider retry directory") from exc
        return cls(root, feature, retry_id, retry_dir)

    def _persist(self, filename: str, payload: Mapping[str, object]) -> None:
        path = _safe_chain(self.root, self.retry_dir / filename, label="provider retry evidence")
        data = _canonical_bytes(payload) + b"\n"
        try:
            with path.open("xb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError as exc:
            raise _fail("SDAI-PROVIDER-RETRY-003", f"retry evidence already exists: {filename}") from exc
        except OSError as exc:
            raise _fail("SDAI-PROVIDER-RETRY-003", f"unable to persist retry evidence: {filename}") from exc

    def policy(self, policy: RetryPolicy) -> None:
        self._persist(
            "000-policy.json",
            {
                "apiVersion": PROVIDER_RETRY_API_VERSION,
                "retryId": self.retry_id,
                "featureId": self.feature_id,
                "policy": policy.as_dict(),
                "policySha256": policy.sha256,
            },
        )

    def decision(self, decision: RetryDecision) -> None:
        payload = decision.as_dict()
        payload["retryId"] = self.retry_id
        payload["sha256"] = decision.sha256
        self._persist(f"{decision.failed_attempt:03d}-decision.json", payload)

    def summary(
        self,
        *,
        status: str,
        attempts: int,
        policy: RetryPolicy,
        final_classification: FailureClassification | None,
    ) -> None:
        self._persist(
            "summary.json",
            {
                "apiVersion": PROVIDER_RETRY_API_VERSION,
                "retryId": self.retry_id,
                "featureId": self.feature_id,
                "status": status,
                "attempts": attempts,
                "policySha256": policy.sha256,
                "finalClassification": (
                    final_classification.as_dict()
                    if final_classification is not None
                    else None
                ),
            },
        )


RetryEscalationHook = Callable[[RetryDecision], None]


def execute_with_retry(
    runtime: AgentRuntime,
    invocation: AgentInvocation,
    *,
    policy: RetryPolicy | None = None,
    cancellation: ProviderCancellationToken | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    retry_id_factory: Callable[[], str] | None = None,
    escalation: RetryEscalationHook | None = None,
) -> AgentExecutionResult:
    """Execute bounded governed attempts without weakening the single-attempt boundary."""
    if not isinstance(runtime, AgentRuntime):
        raise TypeError("runtime must be an AgentRuntime")
    if not isinstance(invocation, AgentInvocation):
        raise TypeError("invocation must be an AgentInvocation")
    effective_policy = policy or RetryPolicy()
    if not isinstance(effective_policy, RetryPolicy):
        raise TypeError("policy must be a RetryPolicy")
    if cancellation is not None and not isinstance(cancellation, ProviderCancellationToken):
        raise TypeError("cancellation must be a ProviderCancellationToken")
    if not callable(sleeper):
        raise TypeError("sleeper must be callable")
    if escalation is not None and not callable(escalation):
        raise TypeError("escalation must be callable")

    recorder = ProviderRetryRecorder.optional_for(
        runtime.project_root,
        invocation.feature_id,
        id_factory=retry_id_factory or (lambda: uuid4().hex),
    )
    retry_id = recorder.retry_id if recorder is not None else _safe_retry_id(
        (retry_id_factory or (lambda: uuid4().hex))()
    )
    if recorder is not None:
        recorder.policy(effective_policy)
    seed = _retry_seed(invocation)

    final_classification: FailureClassification | None = None
    for attempt in range(1, effective_policy.max_attempts + 1):
        diagnostic_attempt_id = f"{retry_id}-a{attempt:03d}"
        attempt_runtime = replace(
            runtime,
            diagnostic_id_factory=lambda value=diagnostic_attempt_id: value,
        )
        try:
            result = attempt_runtime.execute_invocation(
                invocation,
                cancellation=cancellation,
            )
        except BaseException as error:
            classification = classify_provider_failure(error)
            final_classification = classification
            decision = decide_retry(
                effective_policy,
                classification,
                failed_attempt=attempt,
                mode=invocation.mode,
                seed=seed,
                diagnostic_attempt_id=diagnostic_attempt_id,
            )
            # Persist the decision before another provider attempt. If persistence
            # fails, that error propagates and the provider is not retried.
            if recorder is not None:
                recorder.decision(decision)
            if decision.action != "retry":
                if recorder is not None:
                    recorder.summary(
                        status="failed",
                        attempts=attempt,
                        policy=effective_policy,
                        final_classification=classification,
                    )
                if escalation is not None:
                    escalation(decision)
                raise
            sleeper(decision.delay_ms / 1000.0)
            continue

        if recorder is not None:
            recorder.summary(
                status="succeeded",
                attempts=attempt,
                policy=effective_policy,
                final_classification=final_classification,
            )
        return result

    raise _fail("SDAI-PROVIDER-RETRY-004", "retry controller exhausted without terminal result")


__all__ = [
    "PROVIDER_RETRY_API_VERSION",
    "FailureClassification",
    "ProviderFailureCategory",
    "ProviderRetryError",
    "ProviderRetryRecorder",
    "RetryDecision",
    "RetryEscalationHook",
    "RetryPolicy",
    "classify_provider_failure",
    "decide_retry",
    "execute_with_retry",
    "retry_delay_ms",
]
