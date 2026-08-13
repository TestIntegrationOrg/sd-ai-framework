from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json

from sdai.completion_policy import CompletionDimension, CompletionStage, normalize_risk
from sdai.models import validate_feature_id


COMPLETION_BARRIER_API_VERSION = "sdai.completion-barrier/v1"


class CompletionReportError(RuntimeError):
    pass


def _canonical_bytes(value: dict[str, object]) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise CompletionReportError(f"SDAI-COMPLETE-REPORT-001: non-canonical completion report: {exc}") from exc


@dataclass(frozen=True)
class CompletionFinding:
    dimension: CompletionDimension
    status: str
    reason: str
    source: str | None = None

    def __post_init__(self) -> None:
        dimension = self.dimension if isinstance(self.dimension, CompletionDimension) else CompletionDimension(self.dimension)
        object.__setattr__(self, "dimension", dimension)
        if self.status not in {"valid", "missing", "stale", "failed", "blocked", "wrong-attempt", "wrong-subject"}:
            raise CompletionReportError(f"SDAI-COMPLETE-REPORT-002: unsupported finding status {self.status!r}")
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise CompletionReportError("SDAI-COMPLETE-REPORT-002: finding reason must be non-empty")
        if self.source is not None and (not isinstance(self.source, str) or not self.source.strip()):
            raise CompletionReportError("SDAI-COMPLETE-REPORT-002: finding source must be non-empty or null")

    @property
    def satisfied(self) -> bool:
        return self.status == "valid"

    def as_dict(self) -> dict[str, object]:
        return {
            "dimension": self.dimension.value,
            "status": self.status,
            "satisfied": self.satisfied,
            "reason": self.reason,
            "source": self.source,
        }


@dataclass(frozen=True)
class CompletionBarrierReport:
    feature_id: str
    stage: CompletionStage
    subject: str
    risk: str
    git_commit: str
    attempt: int | None
    required: tuple[CompletionDimension, ...]
    findings: tuple[CompletionFinding, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "feature_id", validate_feature_id(self.feature_id))
        object.__setattr__(self, "stage", self.stage if isinstance(self.stage, CompletionStage) else CompletionStage(self.stage))
        object.__setattr__(self, "risk", normalize_risk(self.risk))
        if not isinstance(self.subject, str) or not self.subject.strip():
            raise CompletionReportError("SDAI-COMPLETE-REPORT-003: subject must be non-empty")
        if not isinstance(self.git_commit, str) or len(self.git_commit) not in {40, 64} or any(c not in "0123456789abcdef" for c in self.git_commit):
            raise CompletionReportError("SDAI-COMPLETE-REPORT-003: invalid Git commit")
        if self.attempt is not None and (not isinstance(self.attempt, int) or isinstance(self.attempt, bool) or self.attempt < 1):
            raise CompletionReportError("SDAI-COMPLETE-REPORT-003: attempt must be positive or null")
        required = tuple(sorted(set(self.required), key=lambda item: item.value))
        object.__setattr__(self, "required", required)
        by_dimension: dict[CompletionDimension, CompletionFinding] = {}
        for item in self.findings:
            if not isinstance(item, CompletionFinding):
                raise CompletionReportError("SDAI-COMPLETE-REPORT-003: invalid finding")
            if item.dimension in by_dimension:
                raise CompletionReportError(f"SDAI-COMPLETE-REPORT-003: duplicate finding for {item.dimension.value}")
            by_dimension[item.dimension] = item
        object.__setattr__(self, "findings", tuple(by_dimension[key] for key in sorted(by_dimension, key=lambda item: item.value)))

    @property
    def passed(self) -> bool:
        by_dimension = {item.dimension: item for item in self.findings}
        return all(item in by_dimension and by_dimension[item].satisfied for item in self.required)

    def body_dict(self) -> dict[str, object]:
        return {
            "apiVersion": COMPLETION_BARRIER_API_VERSION,
            "feature_id": self.feature_id,
            "stage": self.stage.value,
            "subject": self.subject,
            "risk": self.risk,
            "git_commit": self.git_commit,
            "attempt": self.attempt,
            "required": [item.value for item in self.required],
            "passed": self.passed,
            "findings": [item.as_dict() for item in self.findings],
        }

    @property
    def sha256(self) -> str:
        return "sha256:" + sha256(_canonical_bytes(self.body_dict())).hexdigest()

    def as_dict(self) -> dict[str, object]:
        result = self.body_dict()
        result["sha256"] = self.sha256
        return result

    def to_json(self) -> str:
        return _canonical_bytes(self.as_dict()).decode("utf-8")


__all__ = ["COMPLETION_BARRIER_API_VERSION", "CompletionBarrierReport", "CompletionFinding", "CompletionReportError"]
