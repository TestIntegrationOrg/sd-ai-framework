from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from hashlib import sha256
import json
import math
from pathlib import Path
import re
from types import MappingProxyType
from typing import Mapping, Sequence

from sdai.artifact_state import ArtifactStateReport
from sdai.models import validate_feature_id
from sdai.path_safety import PathSafetyError, ensure_within_project
from sdai.trace_evidence import (
    EvidenceKind,
    EvidenceStatus,
    TraceEvidence,
    TraceEvidenceError,
)
from sdai.trace_freshness import (
    CommitPolicy,
    EvidenceFreshnessReport,
    ProofFreshness,
    evaluate_trace_evidence_freshness,
)
from sdai.trace_graph import TraceProvenance


VERIFY_REPORT_API_VERSION = "sdai.verify-report/v1"
SEMANTIC_REVIEW_API_VERSION = "sdai.semantic-review/v1"
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_GIT_COMMIT = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_REVIEW_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/#@+\-]{0,255}$")
_FINDING_CODE = re.compile(r"^[A-Z][A-Z0-9_-]{1,127}$")
_MAX_TEXT = 4096
_MAX_JSON_DEPTH = 16
_MAX_JSON_ITEMS = 4096


class VerificationError(RuntimeError):
    """Raised when verification truth or semantic-review evidence is invalid."""


class VerificationFindingSource(str, Enum):
    DETERMINISTIC = "deterministic"
    SEMANTIC = "semantic"


class VerificationCategory(str, Enum):
    ARTIFACT_FRESHNESS = "artifact-freshness"
    ANALYSIS = "analysis"
    TRACE_COVERAGE = "trace-coverage"
    TASK_STATE = "task-state"
    EXECUTION = "execution"
    TEST = "test"
    QUALITY = "quality"
    SECURITY = "security"
    APPROVAL = "approval"
    CONTRACT = "contract"
    CURRENT_STATE = "current-state"
    REQUIREMENT_SATISFACTION = "requirement-satisfaction"
    ARCHITECTURE_INTENT = "architecture-intent"
    FAILURE_BEHAVIOR = "failure-behavior"
    UNDOCUMENTED_BEHAVIOR = "undocumented-behavior"


class VerificationSeverity(str, Enum):
    BLOCKING = "blocking"
    REVIEW = "review"
    WARNING = "warning"
    INFO = "info"


class VerificationStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    BLOCKED = "blocked"
    MISSING = "missing"
    STALE = "stale"
    REVIEW_REQUIRED = "review-required"


class VerificationOutcome(str, Enum):
    PASSED = "passed"
    REVIEW = "review"
    BLOCKED = "blocked"


class SemanticReviewDimension(str, Enum):
    REQUIREMENT_SATISFACTION = "requirement-satisfaction"
    ARCHITECTURE_INTENT = "architecture-intent"
    FAILURE_BEHAVIOR = "failure-behavior"
    UNDOCUMENTED_BEHAVIOR = "undocumented-behavior"


_REVIEW_CATEGORIES = {
    SemanticReviewDimension.REQUIREMENT_SATISFACTION: VerificationCategory.REQUIREMENT_SATISFACTION,
    SemanticReviewDimension.ARCHITECTURE_INTENT: VerificationCategory.ARCHITECTURE_INTENT,
    SemanticReviewDimension.FAILURE_BEHAVIOR: VerificationCategory.FAILURE_BEHAVIOR,
    SemanticReviewDimension.UNDOCUMENTED_BEHAVIOR: VerificationCategory.UNDOCUMENTED_BEHAVIOR,
}


def _fail(code: str, message: str) -> VerificationError:
    return VerificationError(f"{code}: {message}")


def _validate_json(
    value: object,
    *,
    label: str,
    depth: int = 0,
    counter: list[int] | None = None,
) -> None:
    if counter is None:
        counter = [0]
    counter[0] += 1
    if counter[0] > _MAX_JSON_ITEMS:
        raise _fail("SDAI-VERIFY-001", f"{label} exceeds the finite JSON item limit")
    if depth > _MAX_JSON_DEPTH:
        raise _fail("SDAI-VERIFY-001", f"{label} exceeds the finite JSON nesting limit")
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise _fail("SDAI-VERIFY-001", f"{label} contains a non-finite number")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _validate_json(item, label=f"{label}[{index}]", depth=depth + 1, counter=counter)
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str) or not key:
                raise _fail("SDAI-VERIFY-001", f"{label} keys must be non-empty strings")
            _validate_json(item, label=f"{label}.{key}", depth=depth + 1, counter=counter)
        return
    raise _fail("SDAI-VERIFY-001", f"{label} contains unsupported JSON type {type(value).__name__}")


def _canonical_bytes(value: Mapping[str, object]) -> bytes:
    _validate_json(value, label="verification record")
    try:
        return json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise _fail("SDAI-VERIFY-001", f"verification record is not canonical JSON: {exc}") from exc


def _digest(value: Mapping[str, object]) -> str:
    return "sha256:" + sha256(_canonical_bytes(value)).hexdigest()


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _json_mapping(value: Mapping[str, object] | None, *, label: str) -> Mapping[str, object]:
    raw: Mapping[str, object] = value or {}
    if not isinstance(raw, Mapping):
        raise _fail("SDAI-VERIFY-001", f"{label} must be a mapping")
    _validate_json(raw, label=label)
    clone = json.loads(
        json.dumps(raw, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    )
    frozen = _freeze(clone)
    if not isinstance(frozen, Mapping):
        raise _fail("SDAI-VERIFY-001", f"{label} must normalize to a mapping")
    return frozen


def _json_dict(value: Mapping[str, object]) -> dict[str, object]:
    result = _thaw(value)
    if not isinstance(result, dict):
        raise _fail("SDAI-VERIFY-001", "verification metadata did not normalize to an object")
    return result


def _text(value: object, *, label: str, required: bool = True) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip():
        raise _fail("SDAI-VERIFY-001", f"{label} must be non-empty text")
    result = value.strip()
    if len(result) > _MAX_TEXT or "\x00" in result:
        raise _fail("SDAI-VERIFY-001", f"{label} is invalid or too long")
    return result


def _commit(value: object) -> str:
    if not isinstance(value, str):
        raise _fail("SDAI-VERIFY-001", "git_commit must be a string")
    normalized = value.strip().casefold()
    if not _GIT_COMMIT.fullmatch(normalized):
        raise _fail("SDAI-VERIFY-001", f"invalid Git commit identity: {value!r}")
    return normalized


def _sha(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise _fail("SDAI-VERIFY-001", f"{label} must be canonical sha256:<64 lowercase hex>")
    return value


def _provenance(values: Sequence[TraceProvenance], *, label: str) -> tuple[TraceProvenance, ...]:
    by_location: dict[tuple[str, int], TraceProvenance] = {}
    for item in values:
        if not isinstance(item, TraceProvenance):
            raise _fail("SDAI-VERIFY-002", f"{label} provenance contains an invalid item")
        existing = by_location.get(item.location)
        if existing is not None and existing != item:
            raise _fail(
                "SDAI-VERIFY-002",
                f"conflicting provenance at {item.source}:{item.line} for {label}",
            )
        by_location[item.location] = item
    if not by_location:
        raise _fail("SDAI-VERIFY-002", f"{label} requires source/line provenance")
    return tuple(
        sorted(
            by_location.values(),
            key=lambda item: (
                item.source.casefold(),
                item.source,
                item.line,
                item.declaration_sha256 or "",
                item.detail or "",
            ),
        )
    )


@dataclass(frozen=True)
class SemanticReviewEvidence:
    """One semantic review conclusion bound to canonical typed review evidence.

    Producer/provider/model metadata is deliberately outside `truth_dict()` because
    semantic truth is the reviewed claim + current bound evidence, not who happened
    to execute the review.
    """

    review_id: str
    dimension: SemanticReviewDimension
    subject: str
    summary: str
    evidence: TraceEvidence

    def __post_init__(self) -> None:
        if not isinstance(self.review_id, str) or not _REVIEW_ID.fullmatch(self.review_id):
            raise _fail("SDAI-VERIFY-003", f"invalid portable review_id: {self.review_id!r}")
        if "\\" in self.review_id:
            raise _fail("SDAI-VERIFY-003", f"review_id is not portable: {self.review_id!r}")
        try:
            dimension = (
                self.dimension
                if isinstance(self.dimension, SemanticReviewDimension)
                else SemanticReviewDimension(self.dimension)
            )
        except ValueError as exc:
            raise _fail("SDAI-VERIFY-003", f"unsupported semantic review dimension: {self.dimension!r}") from exc
        object.__setattr__(self, "dimension", dimension)
        subject = _text(self.subject, label="semantic review subject")
        summary = _text(self.summary, label="semantic review summary")
        assert subject is not None and summary is not None
        object.__setattr__(self, "subject", subject)
        object.__setattr__(self, "summary", summary)
        if not isinstance(self.evidence, TraceEvidence):
            raise _fail("SDAI-VERIFY-003", "semantic review evidence must be a validated TraceEvidence")
        if self.evidence.kind is not EvidenceKind.REVIEW:
            raise _fail("SDAI-VERIFY-003", "semantic review evidence must use kind=review")
        if self.evidence.status not in {
            EvidenceStatus.PASSED,
            EvidenceStatus.FAILED,
            EvidenceStatus.BLOCKED,
        }:
            raise _fail(
                "SDAI-VERIFY-003",
                "semantic review evidence status must be passed, failed, or blocked",
            )
        if self.evidence.evidence_id != self.review_id:
            raise _fail("SDAI-VERIFY-003", "semantic review_id must match evidence_id")
        if self.evidence.subject != self.subject:
            raise _fail("SDAI-VERIFY-003", "semantic review subject must match evidence subject")

    @property
    def status(self) -> EvidenceStatus:
        return self.evidence.status

    @property
    def category(self) -> VerificationCategory:
        return _REVIEW_CATEGORIES[self.dimension]

    def truth_dict(self) -> dict[str, object]:
        return {
            "apiVersion": SEMANTIC_REVIEW_API_VERSION,
            "review_id": self.review_id,
            "dimension": self.dimension.value,
            "subject": self.subject,
            "status": self.status.value,
            "summary": self.summary,
            "evidence_truth_sha256": self.evidence.truth_sha256,
        }

    @property
    def truth_sha256(self) -> str:
        return _digest(self.truth_dict())

    def body_dict(self) -> dict[str, object]:
        body = self.truth_dict()
        body["evidence"] = self.evidence.as_dict()
        body["truth_sha256"] = self.truth_sha256
        return body

    @property
    def sha256(self) -> str:
        return _digest(self.body_dict())

    def as_dict(self) -> dict[str, object]:
        result = self.body_dict()
        result["sha256"] = self.sha256
        return result

    def to_json(self) -> str:
        return _canonical_bytes(self.as_dict()).decode("utf-8")

    @classmethod
    def from_mapping(cls, value: object) -> "SemanticReviewEvidence":
        if not isinstance(value, Mapping):
            raise _fail("SDAI-VERIFY-003", "semantic review must be a mapping")
        expected = {
            "apiVersion",
            "review_id",
            "dimension",
            "subject",
            "status",
            "summary",
            "evidence_truth_sha256",
            "evidence",
            "truth_sha256",
            "sha256",
        }
        if set(value) != expected:
            raise _fail("SDAI-VERIFY-003", "semantic review fields do not match sdai.semantic-review/v1")
        if value.get("apiVersion") != SEMANTIC_REVIEW_API_VERSION:
            raise _fail("SDAI-VERIFY-003", "unsupported semantic review apiVersion")
        scalar_fields = ("review_id", "dimension", "subject", "status", "summary")
        if any(not isinstance(value.get(name), str) for name in scalar_fields):
            raise _fail("SDAI-VERIFY-003", "semantic review identity/dimension/status fields must be strings")
        try:
            evidence = TraceEvidence.from_mapping(value["evidence"])
        except TraceEvidenceError as exc:
            raise _fail("SDAI-VERIFY-003", f"invalid nested review evidence: {exc}") from exc
        record = cls(
            review_id=value["review_id"],
            dimension=value["dimension"],
            subject=value["subject"],
            summary=value["summary"],
            evidence=evidence,
        )
        if value["status"] != record.status.value:
            raise _fail("SDAI-VERIFY-003", "semantic review status does not match nested evidence")
        if value["evidence_truth_sha256"] != record.evidence.truth_sha256:
            raise _fail("SDAI-VERIFY-003", "semantic review evidence truth SHA-256 does not match")
        truth = _sha(value["truth_sha256"], label="semantic review truth_sha256")
        digest = _sha(value["sha256"], label="semantic review sha256")
        if truth != record.truth_sha256:
            raise _fail("SDAI-VERIFY-003", "semantic review truth SHA-256 does not match canonical claim")
        if digest != record.sha256:
            raise _fail("SDAI-VERIFY-003", "semantic review SHA-256 does not match canonical record")
        return record

    @classmethod
    def from_json(cls, value: str | bytes) -> "SemanticReviewEvidence":
        try:
            text = value.decode("utf-8") if isinstance(value, bytes) else value
            raw = json.loads(text)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
            raise _fail("SDAI-VERIFY-003", f"invalid semantic review JSON: {exc}") from exc
        return cls.from_mapping(raw)


@dataclass(frozen=True)
class SemanticReviewState:
    review_id: str
    dimension: SemanticReviewDimension
    subject: str
    status: EvidenceStatus
    freshness: ProofFreshness
    truth_sha256: str
    evidence_git_commit: str | None
    current_git_commit: str | None
    commit_reachable: bool | None
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.review_id, str) or not _REVIEW_ID.fullmatch(self.review_id):
            raise _fail("SDAI-VERIFY-004", f"invalid semantic review state id: {self.review_id!r}")
        try:
            dimension = (
                self.dimension
                if isinstance(self.dimension, SemanticReviewDimension)
                else SemanticReviewDimension(self.dimension)
            )
            status = self.status if isinstance(self.status, EvidenceStatus) else EvidenceStatus(self.status)
            freshness = (
                self.freshness
                if isinstance(self.freshness, ProofFreshness)
                else ProofFreshness(self.freshness)
            )
        except ValueError as exc:
            raise _fail("SDAI-VERIFY-004", "invalid semantic review state enum value") from exc
        object.__setattr__(self, "dimension", dimension)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "freshness", freshness)
        subject = _text(self.subject, label="semantic review state subject")
        assert subject is not None
        object.__setattr__(self, "subject", subject)
        object.__setattr__(self, "truth_sha256", _sha(self.truth_sha256, label="semantic review state truth_sha256"))
        for label, commit in (
            ("evidence_git_commit", self.evidence_git_commit),
            ("current_git_commit", self.current_git_commit),
        ):
            if commit is not None:
                object.__setattr__(self, label, _commit(commit))
        if self.commit_reachable is not None and not isinstance(self.commit_reachable, bool):
            raise _fail("SDAI-VERIFY-004", "semantic review commit_reachable must be bool or null")
        normalized_reasons: list[str] = []
        for reason in self.reasons:
            text = _text(reason, label="semantic review freshness reason")
            assert text is not None
            normalized_reasons.append(text)
        object.__setattr__(self, "reasons", tuple(normalized_reasons))

    @property
    def satisfies_current_verification(self) -> bool:
        return self.status is EvidenceStatus.PASSED and self.freshness is ProofFreshness.VALID

    def as_dict(self) -> dict[str, object]:
        return {
            "review_id": self.review_id,
            "dimension": self.dimension.value,
            "subject": self.subject,
            "status": self.status.value,
            "freshness": self.freshness.value,
            "satisfies_current_verification": self.satisfies_current_verification,
            "truth_sha256": self.truth_sha256,
            "evidence_git_commit": self.evidence_git_commit,
            "current_git_commit": self.current_git_commit,
            "commit_reachable": self.commit_reachable,
            "reasons": list(self.reasons),
        }

    @classmethod
    def from_mapping(cls, value: object) -> "SemanticReviewState":
        if not isinstance(value, Mapping):
            raise _fail("SDAI-VERIFY-004", "semantic review state must be a mapping")
        expected = {
            "review_id",
            "dimension",
            "subject",
            "status",
            "freshness",
            "satisfies_current_verification",
            "truth_sha256",
            "evidence_git_commit",
            "current_git_commit",
            "commit_reachable",
            "reasons",
        }
        if set(value) != expected:
            raise _fail("SDAI-VERIFY-004", "semantic review state fields do not match contract")
        reasons = value["reasons"]
        if not isinstance(reasons, list) or any(not isinstance(item, str) for item in reasons):
            raise _fail("SDAI-VERIFY-004", "semantic review state reasons must be a string list")
        state = cls(
            review_id=value["review_id"],
            dimension=value["dimension"],
            subject=value["subject"],
            status=value["status"],
            freshness=value["freshness"],
            truth_sha256=value["truth_sha256"],
            evidence_git_commit=value["evidence_git_commit"],
            current_git_commit=value["current_git_commit"],
            commit_reachable=value["commit_reachable"],
            reasons=tuple(reasons),
        )
        if value["satisfies_current_verification"] is not state.satisfies_current_verification:
            raise _fail("SDAI-VERIFY-004", "semantic review current-verification flag is inconsistent")
        return state


@dataclass(frozen=True)
class VerificationFinding:
    code: str
    source: VerificationFindingSource
    category: VerificationCategory
    severity: VerificationSeverity
    status: VerificationStatus
    message: str
    provenance: tuple[TraceProvenance, ...]
    subject: str | None = None
    evidence_truth_sha256: str | None = None
    metadata: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.code, str) or not _FINDING_CODE.fullmatch(self.code):
            raise _fail("SDAI-VERIFY-005", f"invalid verification finding code: {self.code!r}")
        try:
            source = self.source if isinstance(self.source, VerificationFindingSource) else VerificationFindingSource(self.source)
            category = self.category if isinstance(self.category, VerificationCategory) else VerificationCategory(self.category)
            severity = self.severity if isinstance(self.severity, VerificationSeverity) else VerificationSeverity(self.severity)
            status = self.status if isinstance(self.status, VerificationStatus) else VerificationStatus(self.status)
        except ValueError as exc:
            raise _fail("SDAI-VERIFY-005", "invalid verification finding enum value") from exc
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "category", category)
        object.__setattr__(self, "severity", severity)
        object.__setattr__(self, "status", status)
        message = _text(self.message, label="verification finding message")
        assert message is not None
        object.__setattr__(self, "message", message)
        if self.subject is not None:
            subject = _text(self.subject, label="verification finding subject")
            assert subject is not None
            object.__setattr__(self, "subject", subject)
        object.__setattr__(self, "provenance", _provenance(self.provenance, label=f"finding {self.code}"))
        if self.evidence_truth_sha256 is not None:
            object.__setattr__(
                self,
                "evidence_truth_sha256",
                _sha(self.evidence_truth_sha256, label="finding evidence_truth_sha256"),
            )
        if source is VerificationFindingSource.SEMANTIC and self.evidence_truth_sha256 is None:
            raise _fail(
                "SDAI-VERIFY-005",
                "semantic verification findings require current evidence truth SHA-256",
            )
        object.__setattr__(self, "metadata", _json_mapping(self.metadata, label=f"finding {self.code} metadata"))

    @property
    def unresolved(self) -> bool:
        return self.status is not VerificationStatus.PASS

    def as_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "source": self.source.value,
            "category": self.category.value,
            "severity": self.severity.value,
            "status": self.status.value,
            "message": self.message,
            "subject": self.subject,
            "evidence_truth_sha256": self.evidence_truth_sha256,
            "provenance": [item.as_dict() for item in self.provenance],
            "metadata": _json_dict(self.metadata or {}),
        }

    @classmethod
    def from_mapping(cls, value: object) -> "VerificationFinding":
        if not isinstance(value, Mapping):
            raise _fail("SDAI-VERIFY-005", "verification finding must be a mapping")
        expected = {
            "code",
            "source",
            "category",
            "severity",
            "status",
            "message",
            "subject",
            "evidence_truth_sha256",
            "provenance",
            "metadata",
        }
        if set(value) != expected:
            raise _fail("SDAI-VERIFY-005", "verification finding fields do not match contract")
        raw_provenance = value["provenance"]
        metadata = value["metadata"]
        if not isinstance(raw_provenance, list) or not isinstance(metadata, Mapping):
            raise _fail("SDAI-VERIFY-005", "verification finding provenance/metadata types are invalid")
        return cls(
            code=value["code"],
            source=value["source"],
            category=value["category"],
            severity=value["severity"],
            status=value["status"],
            message=value["message"],
            subject=value["subject"],
            evidence_truth_sha256=value["evidence_truth_sha256"],
            provenance=tuple(TraceProvenance.from_mapping(item) for item in raw_provenance),
            metadata=dict(metadata),
        )


@dataclass(frozen=True)
class VerificationReport:
    feature_id: str
    git_commit: str
    input_sha256: str
    findings: tuple[VerificationFinding, ...]
    semantic_reviews: tuple[SemanticReviewState, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "feature_id", validate_feature_id(self.feature_id))
        object.__setattr__(self, "git_commit", _commit(self.git_commit))
        object.__setattr__(self, "input_sha256", _sha(self.input_sha256, label="verification input_sha256"))
        findings: dict[tuple[object, ...], VerificationFinding] = {}
        for item in self.findings:
            if not isinstance(item, VerificationFinding):
                raise _fail("SDAI-VERIFY-006", "verification report contains an invalid finding")
            key = (
                item.code,
                item.source.value,
                item.category.value,
                item.severity.value,
                item.status.value,
                item.subject or "",
                item.evidence_truth_sha256 or "",
                item.message,
                tuple((p.source, p.line) for p in item.provenance),
                _canonical_bytes(_json_dict(item.metadata or {})),
            )
            findings[key] = item
        object.__setattr__(
            self,
            "findings",
            tuple(findings[key] for key in sorted(findings, key=lambda value: tuple(str(part) for part in value))),
        )
        reviews: dict[tuple[str, str, str], SemanticReviewState] = {}
        for item in self.semantic_reviews:
            if not isinstance(item, SemanticReviewState):
                raise _fail("SDAI-VERIFY-006", "verification report contains an invalid semantic review state")
            key = (item.dimension.value, item.subject, item.review_id)
            existing = reviews.get(key)
            if existing is not None and existing != item:
                raise _fail(
                    "SDAI-VERIFY-006",
                    f"conflicting semantic review state for {item.dimension.value}:{item.subject}:{item.review_id}",
                )
            reviews[key] = item
        object.__setattr__(self, "semantic_reviews", tuple(reviews[key] for key in sorted(reviews)))

    @property
    def outcome(self) -> VerificationOutcome:
        # Deterministic and semantic findings share the same explicit severity
        # boundary. A provider pass cannot cancel another blocking finding because
        # outcomes are monotonic over the complete finding set.
        if any(
            item.severity is VerificationSeverity.BLOCKING and item.unresolved
            for item in self.findings
        ):
            return VerificationOutcome.BLOCKED
        if any(
            item.severity is VerificationSeverity.REVIEW and item.unresolved
            for item in self.findings
        ):
            return VerificationOutcome.REVIEW
        return VerificationOutcome.PASSED

    @property
    def passed(self) -> bool:
        return self.outcome is VerificationOutcome.PASSED

    def body_dict(self) -> dict[str, object]:
        return {
            "apiVersion": VERIFY_REPORT_API_VERSION,
            "feature_id": self.feature_id,
            "git_commit": self.git_commit,
            "input_sha256": self.input_sha256,
            "outcome": self.outcome.value,
            "passed": self.passed,
            "findings": [item.as_dict() for item in self.findings],
            "semantic_reviews": [item.as_dict() for item in self.semantic_reviews],
        }

    @property
    def sha256(self) -> str:
        return _digest(self.body_dict())

    def as_dict(self) -> dict[str, object]:
        result = self.body_dict()
        result["sha256"] = self.sha256
        return result

    def to_json(self) -> str:
        return _canonical_bytes(self.as_dict()).decode("utf-8")

    @classmethod
    def from_mapping(cls, value: object) -> "VerificationReport":
        if not isinstance(value, Mapping):
            raise _fail("SDAI-VERIFY-006", "verification report must be a mapping")
        expected = {
            "apiVersion",
            "feature_id",
            "git_commit",
            "input_sha256",
            "outcome",
            "passed",
            "findings",
            "semantic_reviews",
            "sha256",
        }
        if set(value) != expected:
            raise _fail("SDAI-VERIFY-006", "verification report fields do not match sdai.verify-report/v1")
        if value.get("apiVersion") != VERIFY_REPORT_API_VERSION:
            raise _fail("SDAI-VERIFY-006", "unsupported verification report apiVersion")
        raw_findings = value["findings"]
        raw_reviews = value["semantic_reviews"]
        if not isinstance(raw_findings, list) or not isinstance(raw_reviews, list):
            raise _fail("SDAI-VERIFY-006", "verification findings/reviews must be lists")
        report = cls(
            feature_id=value["feature_id"],
            git_commit=value["git_commit"],
            input_sha256=value["input_sha256"],
            findings=tuple(VerificationFinding.from_mapping(item) for item in raw_findings),
            semantic_reviews=tuple(SemanticReviewState.from_mapping(item) for item in raw_reviews),
        )
        if value["outcome"] != report.outcome.value or value["passed"] is not report.passed:
            raise _fail("SDAI-VERIFY-006", "verification report outcome/pass flag is inconsistent")
        digest = _sha(value["sha256"], label="verification report sha256")
        if digest != report.sha256:
            raise _fail("SDAI-VERIFY-006", "verification report SHA-256 does not match canonical content")
        return report

    @classmethod
    def from_json(cls, value: str | bytes) -> "VerificationReport":
        try:
            text = value.decode("utf-8") if isinstance(value, bytes) else value
            raw = json.loads(text)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
            raise _fail("SDAI-VERIFY-006", f"invalid verification report JSON: {exc}") from exc
        return cls.from_mapping(raw)


def evaluate_semantic_review_freshness(
    project_root: Path,
    review: SemanticReviewEvidence,
    *,
    commit_policy: CommitPolicy = CommitPolicy.ANCESTOR,
    artifact_state_report: ArtifactStateReport | None = None,
) -> SemanticReviewState:
    if not isinstance(review, SemanticReviewEvidence):
        raise _fail("SDAI-VERIFY-004", "review must be validated SemanticReviewEvidence")
    evidence_state = evaluate_trace_evidence_freshness(
        project_root,
        review.evidence,
        commit_policy=commit_policy,
        artifact_state_report=artifact_state_report,
    )
    return _semantic_state(review, evidence_state)


def _semantic_state(
    review: SemanticReviewEvidence,
    evidence_state: EvidenceFreshnessReport,
) -> SemanticReviewState:
    if evidence_state.evidence_id != review.review_id or evidence_state.subject != review.subject:
        raise _fail("SDAI-VERIFY-004", "semantic review freshness identity does not match review")
    return SemanticReviewState(
        review_id=review.review_id,
        dimension=review.dimension,
        subject=review.subject,
        status=review.status,
        freshness=evidence_state.freshness,
        truth_sha256=review.truth_sha256,
        evidence_git_commit=evidence_state.evidence_git_commit,
        current_git_commit=evidence_state.current_git_commit,
        commit_reachable=evidence_state.commit_reachable,
        reasons=evidence_state.reasons,
    )


def load_semantic_review_evidence(project_root: Path, path: Path) -> SemanticReviewEvidence:
    root = project_root.resolve()
    candidate = path if path.is_absolute() else root / path
    try:
        safe = ensure_within_project(root, candidate, label="semantic review evidence")
    except PathSafetyError as exc:
        raise _fail("SDAI-VERIFY-007", "semantic review evidence path must remain inside project root") from exc
    try:
        relative = safe.relative_to(root)
    except ValueError as exc:
        raise _fail("SDAI-VERIFY-007", "semantic review evidence path must remain inside project root") from exc
    current = root
    for part in relative.parts:
        current = current / part
        if current.exists() and current.is_symlink():
            raise _fail(
                "SDAI-VERIFY-007",
                f"semantic review evidence path contains a symlink component: {relative.as_posix()}",
            )
    if safe.is_symlink() or not safe.is_file():
        raise _fail(
            "SDAI-VERIFY-007",
            f"semantic review evidence must be a regular non-symlink file: {relative.as_posix()}",
        )
    try:
        content = safe.read_bytes()
    except OSError as exc:
        raise _fail("SDAI-VERIFY-007", f"unable to read semantic review evidence: {exc}") from exc
    return SemanticReviewEvidence.from_json(content)


__all__ = [
    "SEMANTIC_REVIEW_API_VERSION",
    "VERIFY_REPORT_API_VERSION",
    "SemanticReviewDimension",
    "SemanticReviewEvidence",
    "SemanticReviewState",
    "VerificationCategory",
    "VerificationError",
    "VerificationFinding",
    "VerificationFindingSource",
    "VerificationOutcome",
    "VerificationReport",
    "VerificationSeverity",
    "VerificationStatus",
    "evaluate_semantic_review_freshness",
    "load_semantic_review_evidence",
]
