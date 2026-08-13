from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Mapping

from sdai.models import validate_feature_id
from sdai.path_safety import PathSafetyError, ensure_within_project
from sdai.trace_graph import TraceProvenance
from sdai.verification import (
    SemanticReviewDimension,
    VerificationCategory,
    VerificationFinding,
    VerificationFindingSource,
    VerificationOutcome,
    VerificationReport,
    VerificationSeverity,
    VerificationStatus,
)
from sdai.verify_engine import verify_feature


CONVERGENCE_STATE_API_VERSION = "sdai.convergence-state/v1"
REMEDIATION_TASK_API_VERSION = "sdai.remediation-task/v1"
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_TASK_ID = re.compile(r"^REMEDIATE-[0-9a-f]{16}$")
_ROUND_ID = re.compile(r"^ROUND-[0-9a-f]{16}$")
_RISKS = frozenset({"trivial", "standard", "critical", "regulated"})


class ConvergenceError(RuntimeError):
    """Raised when deterministic convergence state is invalid or unsafe."""


class ConvergenceStatus(str, Enum):
    VERIFIED = "verified"
    ACTION_REQUIRED = "action-required"
    ESCALATED = "escalated"


class EscalationReason(str, Enum):
    MAX_ROUNDS = "max-rounds"
    NON_REMEDIABLE = "non-remediable"
    NO_PROGRESS = "no-progress"


class RemediationKind(str, Enum):
    IMPLEMENTATION = "implementation"
    TEST = "test"
    ARCHITECTURE = "architecture"
    SECURITY = "security"
    CONTRACT = "contract"
    REVIEW = "review"


def _fail(code: str, message: str) -> ConvergenceError:
    return ConvergenceError(f"{code}: {message}")


def _canonical_bytes(value: Mapping[str, object]) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise _fail("SDAI-CONVERGE-001", f"convergence record is not canonical JSON: {exc}") from exc


def _hash(value: Mapping[str, object]) -> str:
    return "sha256:" + sha256(_canonical_bytes(value)).hexdigest()


def _require_sha(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise _fail("SDAI-CONVERGE-002", f"{label} must be canonical sha256:<64 lowercase hex>")
    return value


def _require_text(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _fail("SDAI-CONVERGE-002", f"{label} must be non-empty text")
    result = value.strip()
    if "\x00" in result or len(result) > 4096:
        raise _fail("SDAI-CONVERGE-002", f"{label} is invalid or too long")
    return result


def _risk(value: str) -> str:
    normalized = value.strip().lower() if isinstance(value, str) else ""
    if normalized not in _RISKS:
        raise _fail("SDAI-CONVERGE-002", f"risk must be one of: {', '.join(sorted(_RISKS))}")
    return normalized


def _forbidden_roots(feature_id: str) -> tuple[str, ...]:
    return (
        f"specs/changes/{feature_id}/requirements.md",
        "specs/current",
    )


def _allowed_roots(kind: RemediationKind, feature_id: str) -> tuple[str, ...]:
    feature = f"specs/changes/{feature_id}"
    return {
        RemediationKind.IMPLEMENTATION: (
            "src",
            "tests",
            f"{feature}/plan.md",
            f"{feature}/tasks.md",
            f"{feature}/tests.md",
        ),
        RemediationKind.TEST: (
            "src",
            "tests",
            f"{feature}/tests.md",
        ),
        RemediationKind.ARCHITECTURE: (
            "src",
            f"{feature}/architecture.md",
            f"{feature}/adr",
            f"{feature}/contracts",
        ),
        RemediationKind.SECURITY: (
            "src",
            "tests",
            f"{feature}/security.md",
            f"{feature}/security",
        ),
        RemediationKind.CONTRACT: (
            "src",
            "tests",
            f"{feature}/contracts",
        ),
        RemediationKind.REVIEW: (
            f".sdai/verification/{feature_id}/reviews",
        ),
    }[kind]


def _finding_payload(finding: VerificationFinding) -> dict[str, object]:
    return {
        "code": finding.code,
        "source": finding.source.value,
        "category": finding.category.value,
        "severity": finding.severity.value,
        "status": finding.status.value,
        "subject": finding.subject,
        "evidence_truth_sha256": finding.evidence_truth_sha256,
        "message": finding.message,
        "provenance": [item.as_dict() for item in finding.provenance],
    }


def _finding_key(finding: VerificationFinding) -> str:
    return _hash(_finding_payload(finding))


def _actionable_findings(report: VerificationReport) -> tuple[VerificationFinding, ...]:
    return tuple(
        item
        for item in report.findings
        if item.status is not VerificationStatus.PASS
        and item.severity in {VerificationSeverity.BLOCKING, VerificationSeverity.REVIEW}
    )


def _finding_signature(report: VerificationReport) -> str:
    return _hash(
        {
            "findings": [
                _finding_payload(item)
                for item in sorted(
                    _actionable_findings(report),
                    key=lambda finding: (
                        finding.code,
                        finding.category.value,
                        finding.subject or "",
                        finding.status.value,
                        _finding_key(finding),
                    ),
                )
            ]
        }
    )


def _review_dimension(category: VerificationCategory) -> bool:
    return category in {
        VerificationCategory.REQUIREMENT_SATISFACTION,
        VerificationCategory.ARCHITECTURE_INTENT,
        VerificationCategory.FAILURE_BEHAVIOR,
        VerificationCategory.UNDOCUMENTED_BEHAVIOR,
    }


def _classify(finding: VerificationFinding) -> RemediationKind | None:
    if finding.status in {
        VerificationStatus.REVIEW_REQUIRED,
        VerificationStatus.STALE,
        VerificationStatus.MISSING,
    } and _review_dimension(finding.category):
        return RemediationKind.REVIEW

    if finding.category is VerificationCategory.APPROVAL:
        return None
    if finding.category is VerificationCategory.CURRENT_STATE:
        return None
    if "MISSING_NFR" in finding.code or "UNAPPROVED_BREAKING_CHANGE" in finding.code:
        return None
    if finding.category is VerificationCategory.ARTIFACT_FRESHNESS:
        subject = (finding.subject or "").casefold()
        if subject in {"requirements", "artifact:requirements"} or "STALE_ARTIFACT" in finding.code and subject == "requirements":
            return None
        if "architecture" in subject or "adr" in subject:
            return RemediationKind.ARCHITECTURE
        if "security" in subject:
            return RemediationKind.SECURITY
        if "tests" in subject:
            return RemediationKind.TEST
        return RemediationKind.IMPLEMENTATION
    if finding.category in {
        VerificationCategory.REQUIREMENT_SATISFACTION,
        VerificationCategory.FAILURE_BEHAVIOR,
        VerificationCategory.UNDOCUMENTED_BEHAVIOR,
        VerificationCategory.TRACE_COVERAGE,
        VerificationCategory.TASK_STATE,
    }:
        return RemediationKind.IMPLEMENTATION
    if finding.category is VerificationCategory.ARCHITECTURE_INTENT:
        return RemediationKind.ARCHITECTURE
    if finding.category in {VerificationCategory.TEST, VerificationCategory.QUALITY, VerificationCategory.EXECUTION}:
        return RemediationKind.TEST
    if finding.category is VerificationCategory.SECURITY:
        return RemediationKind.SECURITY
    if finding.category is VerificationCategory.CONTRACT:
        return RemediationKind.CONTRACT
    if finding.category is VerificationCategory.ANALYSIS:
        if "ARCHITECTURE_CONFLICT" in finding.code or "UNRESOLVED_ADR" in finding.code:
            return RemediationKind.ARCHITECTURE
        if "CONTRACT_CONFLICT" in finding.code:
            return RemediationKind.CONTRACT
        if "UNTESTED_SCENARIO" in finding.code:
            return RemediationKind.TEST
        if "UNMITIGATED_THREAT" in finding.code:
            return RemediationKind.SECURITY
        return RemediationKind.IMPLEMENTATION
    return RemediationKind.IMPLEMENTATION


@dataclass(frozen=True)
class RemediationTask:
    task_id: str
    feature_id: str
    round_id: str
    verification_report_sha256: str
    verification_input_sha256: str
    finding_sha256: str
    finding_code: str
    finding_source: VerificationFindingSource
    category: VerificationCategory
    severity: VerificationSeverity
    status: VerificationStatus
    subject: str | None
    summary: str
    remediation_kind: RemediationKind
    allowed_roots: tuple[str, ...]
    forbidden_roots: tuple[str, ...]
    provenance: tuple[TraceProvenance, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, str) or not _TASK_ID.fullmatch(self.task_id):
            raise _fail("SDAI-CONVERGE-003", f"invalid remediation task id: {self.task_id!r}")
        object.__setattr__(self, "feature_id", validate_feature_id(self.feature_id))
        if not isinstance(self.round_id, str) or not _ROUND_ID.fullmatch(self.round_id):
            raise _fail("SDAI-CONVERGE-003", f"invalid convergence round id: {self.round_id!r}")
        for label in ("verification_report_sha256", "verification_input_sha256", "finding_sha256"):
            object.__setattr__(self, label, _require_sha(getattr(self, label), label=label))
        try:
            object.__setattr__(
                self,
                "finding_source",
                self.finding_source
                if isinstance(self.finding_source, VerificationFindingSource)
                else VerificationFindingSource(self.finding_source),
            )
            object.__setattr__(
                self,
                "category",
                self.category if isinstance(self.category, VerificationCategory) else VerificationCategory(self.category),
            )
            object.__setattr__(
                self,
                "severity",
                self.severity if isinstance(self.severity, VerificationSeverity) else VerificationSeverity(self.severity),
            )
            object.__setattr__(
                self,
                "status",
                self.status if isinstance(self.status, VerificationStatus) else VerificationStatus(self.status),
            )
            object.__setattr__(
                self,
                "remediation_kind",
                self.remediation_kind
                if isinstance(self.remediation_kind, RemediationKind)
                else RemediationKind(self.remediation_kind),
            )
        except ValueError as exc:
            raise _fail("SDAI-CONVERGE-003", "invalid remediation task enum value") from exc
        object.__setattr__(self, "finding_code", _require_text(self.finding_code, label="finding_code"))
        object.__setattr__(self, "summary", _require_text(self.summary, label="remediation summary"))
        if self.subject is not None:
            object.__setattr__(self, "subject", _require_text(self.subject, label="remediation subject"))
        if not self.allowed_roots:
            raise _fail("SDAI-CONVERGE-003", "remediation task requires at least one allowed root")
        if not self.forbidden_roots:
            raise _fail("SDAI-CONVERGE-003", "remediation task requires forbidden source-truth roots")
        object.__setattr__(self, "allowed_roots", tuple(sorted(set(self.allowed_roots))))
        object.__setattr__(self, "forbidden_roots", tuple(sorted(set(self.forbidden_roots))))
        if set(self.allowed_roots) & set(self.forbidden_roots):
            raise _fail("SDAI-CONVERGE-003", "remediation allowed/forbidden roots overlap")
        if f"specs/changes/{self.feature_id}/requirements.md" not in self.forbidden_roots:
            raise _fail("SDAI-CONVERGE-003", "remediation task must forbid feature requirements source truth")
        if "specs/current" not in self.forbidden_roots:
            raise _fail("SDAI-CONVERGE-003", "remediation task must forbid current specification truth")
        if not self.provenance:
            raise _fail("SDAI-CONVERGE-003", "remediation task requires finding provenance")
        object.__setattr__(
            self,
            "provenance",
            tuple(
                sorted(
                    self.provenance,
                    key=lambda item: (item.source.casefold(), item.source, item.line),
                )
            ),
        )

    def body_dict(self) -> dict[str, object]:
        return {
            "apiVersion": REMEDIATION_TASK_API_VERSION,
            "task_id": self.task_id,
            "feature_id": self.feature_id,
            "round_id": self.round_id,
            "verification_report_sha256": self.verification_report_sha256,
            "verification_input_sha256": self.verification_input_sha256,
            "finding_sha256": self.finding_sha256,
            "finding_code": self.finding_code,
            "finding_source": self.finding_source.value,
            "category": self.category.value,
            "severity": self.severity.value,
            "status": self.status.value,
            "subject": self.subject,
            "summary": self.summary,
            "remediation_kind": self.remediation_kind.value,
            "allowed_roots": list(self.allowed_roots),
            "forbidden_roots": list(self.forbidden_roots),
            "provenance": [item.as_dict() for item in self.provenance],
        }

    @property
    def sha256(self) -> str:
        return _hash(self.body_dict())

    def as_dict(self) -> dict[str, object]:
        result = self.body_dict()
        result["sha256"] = self.sha256
        return result

    def to_json(self) -> str:
        return _canonical_bytes(self.as_dict()).decode("utf-8")

    @classmethod
    def from_mapping(cls, value: object) -> "RemediationTask":
        if not isinstance(value, Mapping):
            raise _fail("SDAI-CONVERGE-003", "remediation task must be a mapping")
        expected = {
            "apiVersion",
            "task_id",
            "feature_id",
            "round_id",
            "verification_report_sha256",
            "verification_input_sha256",
            "finding_sha256",
            "finding_code",
            "finding_source",
            "category",
            "severity",
            "status",
            "subject",
            "summary",
            "remediation_kind",
            "allowed_roots",
            "forbidden_roots",
            "provenance",
            "sha256",
        }
        if set(value) != expected or value.get("apiVersion") != REMEDIATION_TASK_API_VERSION:
            raise _fail("SDAI-CONVERGE-003", "remediation task fields/apiVersion do not match contract")
        if not isinstance(value["allowed_roots"], list) or not isinstance(value["forbidden_roots"], list):
            raise _fail("SDAI-CONVERGE-003", "remediation task roots must be lists")
        if not isinstance(value["provenance"], list):
            raise _fail("SDAI-CONVERGE-003", "remediation task provenance must be a list")
        task = cls(
            task_id=value["task_id"],
            feature_id=value["feature_id"],
            round_id=value["round_id"],
            verification_report_sha256=value["verification_report_sha256"],
            verification_input_sha256=value["verification_input_sha256"],
            finding_sha256=value["finding_sha256"],
            finding_code=value["finding_code"],
            finding_source=value["finding_source"],
            category=value["category"],
            severity=value["severity"],
            status=value["status"],
            subject=value["subject"],
            summary=value["summary"],
            remediation_kind=value["remediation_kind"],
            allowed_roots=tuple(value["allowed_roots"]),
            forbidden_roots=tuple(value["forbidden_roots"]),
            provenance=tuple(TraceProvenance.from_mapping(item) for item in value["provenance"]),
        )
        if value["sha256"] != task.sha256:
            raise _fail("SDAI-CONVERGE-003", "remediation task SHA-256 does not match canonical content")
        return task


@dataclass(frozen=True)
class ConvergenceRound:
    number: int
    round_id: str
    git_commit: str
    verification_report_sha256: str
    verification_input_sha256: str
    verification_outcome: VerificationOutcome
    finding_signature: str
    task_ids: tuple[str, ...]
    non_remediable: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.number, int) or isinstance(self.number, bool) or self.number < 1:
            raise _fail("SDAI-CONVERGE-004", "convergence round number must be positive")
        if not isinstance(self.round_id, str) or not _ROUND_ID.fullmatch(self.round_id):
            raise _fail("SDAI-CONVERGE-004", f"invalid convergence round id: {self.round_id!r}")
        if not isinstance(self.git_commit, str) or len(self.git_commit) not in {40, 64}:
            raise _fail("SDAI-CONVERGE-004", "convergence round git_commit is invalid")
        if any(char not in "0123456789abcdef" for char in self.git_commit):
            raise _fail("SDAI-CONVERGE-004", "convergence round git_commit must be lowercase hex")
        for label in ("verification_report_sha256", "verification_input_sha256", "finding_signature"):
            object.__setattr__(self, label, _require_sha(getattr(self, label), label=label))
        try:
            object.__setattr__(
                self,
                "verification_outcome",
                self.verification_outcome
                if isinstance(self.verification_outcome, VerificationOutcome)
                else VerificationOutcome(self.verification_outcome),
            )
        except ValueError as exc:
            raise _fail("SDAI-CONVERGE-004", "invalid verification outcome in convergence round") from exc
        if any(not isinstance(item, str) or not _TASK_ID.fullmatch(item) for item in self.task_ids):
            raise _fail("SDAI-CONVERGE-004", "convergence round contains invalid task id")
        object.__setattr__(self, "task_ids", tuple(sorted(set(self.task_ids))))
        object.__setattr__(
            self,
            "non_remediable",
            tuple(sorted({_require_text(item, label="non-remediable finding") for item in self.non_remediable})),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "number": self.number,
            "round_id": self.round_id,
            "git_commit": self.git_commit,
            "verification_report_sha256": self.verification_report_sha256,
            "verification_input_sha256": self.verification_input_sha256,
            "verification_outcome": self.verification_outcome.value,
            "finding_signature": self.finding_signature,
            "task_ids": list(self.task_ids),
            "non_remediable": list(self.non_remediable),
        }

    @classmethod
    def from_mapping(cls, value: object) -> "ConvergenceRound":
        if not isinstance(value, Mapping):
            raise _fail("SDAI-CONVERGE-004", "convergence round must be a mapping")
        expected = {
            "number",
            "round_id",
            "git_commit",
            "verification_report_sha256",
            "verification_input_sha256",
            "verification_outcome",
            "finding_signature",
            "task_ids",
            "non_remediable",
        }
        if set(value) != expected:
            raise _fail("SDAI-CONVERGE-004", "convergence round fields do not match contract")
        if not isinstance(value["task_ids"], list) or not isinstance(value["non_remediable"], list):
            raise _fail("SDAI-CONVERGE-004", "convergence round task/non-remediable values must be lists")
        return cls(
            number=value["number"],
            round_id=value["round_id"],
            git_commit=value["git_commit"],
            verification_report_sha256=value["verification_report_sha256"],
            verification_input_sha256=value["verification_input_sha256"],
            verification_outcome=value["verification_outcome"],
            finding_signature=value["finding_signature"],
            task_ids=tuple(value["task_ids"]),
            non_remediable=tuple(value["non_remediable"]),
        )


@dataclass(frozen=True)
class ConvergenceState:
    feature_id: str
    risk: str
    max_rounds: int
    status: ConvergenceStatus
    escalation_reason: EscalationReason | None
    current_verification_report_sha256: str
    current_verification_input_sha256: str
    rounds: tuple[ConvergenceRound, ...]
    tasks: tuple[RemediationTask, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "feature_id", validate_feature_id(self.feature_id))
        object.__setattr__(self, "risk", _risk(self.risk))
        if not isinstance(self.max_rounds, int) or isinstance(self.max_rounds, bool) or not 1 <= self.max_rounds <= 100:
            raise _fail("SDAI-CONVERGE-005", "max_rounds must be an integer from 1 to 100")
        try:
            status = self.status if isinstance(self.status, ConvergenceStatus) else ConvergenceStatus(self.status)
        except ValueError as exc:
            raise _fail("SDAI-CONVERGE-005", "invalid convergence status") from exc
        object.__setattr__(self, "status", status)
        if self.escalation_reason is not None:
            try:
                reason = (
                    self.escalation_reason
                    if isinstance(self.escalation_reason, EscalationReason)
                    else EscalationReason(self.escalation_reason)
                )
            except ValueError as exc:
                raise _fail("SDAI-CONVERGE-005", "invalid convergence escalation reason") from exc
            object.__setattr__(self, "escalation_reason", reason)
        if status is ConvergenceStatus.ESCALATED and self.escalation_reason is None:
            raise _fail("SDAI-CONVERGE-005", "escalated convergence state requires a reason")
        if status is not ConvergenceStatus.ESCALATED and self.escalation_reason is not None:
            raise _fail("SDAI-CONVERGE-005", "non-escalated convergence state cannot carry escalation reason")
        object.__setattr__(
            self,
            "current_verification_report_sha256",
            _require_sha(self.current_verification_report_sha256, label="current verification report SHA-256"),
        )
        object.__setattr__(
            self,
            "current_verification_input_sha256",
            _require_sha(self.current_verification_input_sha256, label="current verification input SHA-256"),
        )
        rounds = tuple(self.rounds)
        if tuple(item.number for item in rounds) != tuple(range(1, len(rounds) + 1)):
            raise _fail("SDAI-CONVERGE-005", "convergence rounds must be contiguous from 1")
        object.__setattr__(self, "rounds", rounds)
        tasks_by_id: dict[str, RemediationTask] = {}
        for task in self.tasks:
            existing = tasks_by_id.get(task.task_id)
            if existing is not None and existing != task:
                raise _fail("SDAI-CONVERGE-005", f"conflicting remediation task id: {task.task_id}")
            tasks_by_id[task.task_id] = task
        object.__setattr__(self, "tasks", tuple(tasks_by_id[key] for key in sorted(tasks_by_id)))
        known = set(tasks_by_id)
        for item in rounds:
            if any(task_id not in known for task_id in item.task_ids):
                raise _fail("SDAI-CONVERGE-005", f"round {item.number} references unknown remediation task")

    def body_dict(self) -> dict[str, object]:
        return {
            "apiVersion": CONVERGENCE_STATE_API_VERSION,
            "feature_id": self.feature_id,
            "risk": self.risk,
            "max_rounds": self.max_rounds,
            "status": self.status.value,
            "escalation_reason": None if self.escalation_reason is None else self.escalation_reason.value,
            "current_verification_report_sha256": self.current_verification_report_sha256,
            "current_verification_input_sha256": self.current_verification_input_sha256,
            "rounds": [item.as_dict() for item in self.rounds],
            "tasks": [item.as_dict() for item in self.tasks],
        }

    @property
    def sha256(self) -> str:
        return _hash(self.body_dict())

    def as_dict(self) -> dict[str, object]:
        result = self.body_dict()
        result["sha256"] = self.sha256
        return result

    def to_json(self) -> str:
        return _canonical_bytes(self.as_dict()).decode("utf-8")

    @classmethod
    def from_mapping(cls, value: object) -> "ConvergenceState":
        if not isinstance(value, Mapping):
            raise _fail("SDAI-CONVERGE-005", "convergence state must be a mapping")
        expected = {
            "apiVersion",
            "feature_id",
            "risk",
            "max_rounds",
            "status",
            "escalation_reason",
            "current_verification_report_sha256",
            "current_verification_input_sha256",
            "rounds",
            "tasks",
            "sha256",
        }
        if set(value) != expected or value.get("apiVersion") != CONVERGENCE_STATE_API_VERSION:
            raise _fail("SDAI-CONVERGE-005", "convergence state fields/apiVersion do not match contract")
        if not isinstance(value["rounds"], list) or not isinstance(value["tasks"], list):
            raise _fail("SDAI-CONVERGE-005", "convergence rounds/tasks must be lists")
        state = cls(
            feature_id=value["feature_id"],
            risk=value["risk"],
            max_rounds=value["max_rounds"],
            status=value["status"],
            escalation_reason=value["escalation_reason"],
            current_verification_report_sha256=value["current_verification_report_sha256"],
            current_verification_input_sha256=value["current_verification_input_sha256"],
            rounds=tuple(ConvergenceRound.from_mapping(item) for item in value["rounds"]),
            tasks=tuple(RemediationTask.from_mapping(item) for item in value["tasks"]),
        )
        if value["sha256"] != state.sha256:
            raise _fail("SDAI-CONVERGE-005", "convergence state SHA-256 does not match canonical content")
        return state

    @classmethod
    def from_json(cls, value: str | bytes) -> "ConvergenceState":
        try:
            text = value.decode("utf-8") if isinstance(value, bytes) else value
            raw = json.loads(text)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
            raise _fail("SDAI-CONVERGE-005", f"invalid convergence state JSON: {exc}") from exc
        return cls.from_mapping(raw)


def _state_directory(root: Path, feature_id: str) -> Path:
    try:
        return ensure_within_project(
            root,
            root / ".sdai" / "convergence" / feature_id,
            label="convergence state directory",
        )
    except PathSafetyError as exc:
        raise _fail("SDAI-CONVERGE-006", "convergence state path escapes project root") from exc


def convergence_state_path(project_root: Path, feature_id: str) -> Path:
    root = project_root.resolve()
    feature = validate_feature_id(feature_id)
    return _state_directory(root, feature) / "state.json"


def _task_path(root: Path, feature_id: str, task_id: str) -> Path:
    return _state_directory(root, feature_id) / "tasks" / f"{task_id}.json"


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="wb",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    )
    temp = Path(handle.name)
    try:
        with handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        if os.name != "nt":
            flags = os.O_RDONLY | (getattr(os, "O_DIRECTORY", 0))
            fd = os.open(path.parent, flags)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    finally:
        temp.unlink(missing_ok=True)


def load_convergence_state(project_root: Path, feature_id: str) -> ConvergenceState | None:
    path = convergence_state_path(project_root, feature_id)
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise _fail("SDAI-CONVERGE-006", "convergence state must be a regular non-symlink file")
    try:
        return ConvergenceState.from_json(path.read_bytes())
    except OSError as exc:
        raise _fail("SDAI-CONVERGE-006", f"unable to read convergence state: {exc}") from exc


def _persist_task(root: Path, task: RemediationTask) -> None:
    path = _task_path(root, task.feature_id, task.task_id)
    if path.exists():
        if path.is_symlink() or not path.is_file():
            raise _fail("SDAI-CONVERGE-006", f"remediation task path is unsafe: {task.task_id}")
        try:
            existing = RemediationTask.from_mapping(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise _fail("SDAI-CONVERGE-006", f"unable to validate existing remediation task: {exc}") from exc
        if existing != task:
            raise _fail("SDAI-CONVERGE-006", f"remediation task collision: {task.task_id}")
        return
    _atomic_write(path, task.to_json().encode("utf-8") + b"\n")


def _persist_state(root: Path, state: ConvergenceState) -> None:
    for task in state.tasks:
        _persist_task(root, task)
    _atomic_write(convergence_state_path(root, state.feature_id), state.to_json().encode("utf-8") + b"\n")


def _round_id(feature_id: str, report: VerificationReport) -> str:
    digest = sha256(
        f"{feature_id}\0{report.git_commit}\0{report.input_sha256}\0{report.sha256}".encode("utf-8")
    ).hexdigest()
    return f"ROUND-{digest[:16]}"


def _task_for_finding(
    feature_id: str,
    round_id: str,
    report: VerificationReport,
    finding: VerificationFinding,
    kind: RemediationKind,
) -> RemediationTask:
    finding_sha = _finding_key(finding)
    digest = sha256(f"{round_id}\0{finding_sha}".encode("utf-8")).hexdigest()
    return RemediationTask(
        task_id=f"REMEDIATE-{digest[:16]}",
        feature_id=feature_id,
        round_id=round_id,
        verification_report_sha256=report.sha256,
        verification_input_sha256=report.input_sha256,
        finding_sha256=finding_sha,
        finding_code=finding.code,
        finding_source=finding.source,
        category=finding.category,
        severity=finding.severity,
        status=finding.status,
        subject=finding.subject,
        summary=finding.message,
        remediation_kind=kind,
        allowed_roots=_allowed_roots(kind, feature_id),
        forbidden_roots=_forbidden_roots(feature_id),
        provenance=finding.provenance,
    )


def _round(
    number: int,
    report: VerificationReport,
    *,
    feature_id: str,
    tasks: tuple[RemediationTask, ...],
    non_remediable: tuple[str, ...],
) -> ConvergenceRound:
    return ConvergenceRound(
        number=number,
        round_id=_round_id(feature_id, report),
        git_commit=report.git_commit,
        verification_report_sha256=report.sha256,
        verification_input_sha256=report.input_sha256,
        verification_outcome=report.outcome,
        finding_signature=_finding_signature(report),
        task_ids=tuple(task.task_id for task in tasks),
        non_remediable=non_remediable,
    )


def run_convergence(
    project_root: Path,
    feature_id: str,
    *,
    risk: str = "standard",
    max_rounds: int = 3,
    environ: Mapping[str, str] | None = None,
) -> ConvergenceState:
    root = project_root.resolve()
    feature = validate_feature_id(feature_id)
    selected_risk = _risk(risk)
    if not isinstance(max_rounds, int) or isinstance(max_rounds, bool) or not 1 <= max_rounds <= 100:
        raise _fail("SDAI-CONVERGE-002", "max_rounds must be an integer from 1 to 100")

    previous = load_convergence_state(root, feature)
    if previous is not None:
        if previous.risk != selected_risk:
            raise _fail("SDAI-CONVERGE-007", "cannot change risk for an existing convergence ledger")
        if previous.max_rounds != max_rounds:
            raise _fail("SDAI-CONVERGE-007", "cannot change max_rounds for an existing convergence ledger")

    report = verify_feature(root, feature, risk=selected_risk, environ=environ)
    if previous is not None and report.input_sha256 == previous.current_verification_input_sha256:
        return previous

    prior_rounds = () if previous is None else previous.rounds
    prior_tasks = () if previous is None else previous.tasks

    if report.outcome is VerificationOutcome.PASSED:
        current_round = _round(
            len(prior_rounds) + 1,
            report,
            feature_id=feature,
            tasks=(),
            non_remediable=(),
        )
        state = ConvergenceState(
            feature_id=feature,
            risk=selected_risk,
            max_rounds=max_rounds,
            status=ConvergenceStatus.VERIFIED,
            escalation_reason=None,
            current_verification_report_sha256=report.sha256,
            current_verification_input_sha256=report.input_sha256,
            rounds=prior_rounds + (current_round,),
            tasks=prior_tasks,
        )
        _persist_state(root, state)
        return state

    if len(prior_rounds) >= max_rounds:
        state = ConvergenceState(
            feature_id=feature,
            risk=selected_risk,
            max_rounds=max_rounds,
            status=ConvergenceStatus.ESCALATED,
            escalation_reason=EscalationReason.MAX_ROUNDS,
            current_verification_report_sha256=report.sha256,
            current_verification_input_sha256=report.input_sha256,
            rounds=prior_rounds,
            tasks=prior_tasks,
        )
        _persist_state(root, state)
        return state

    actionable = _actionable_findings(report)
    classified: list[tuple[VerificationFinding, RemediationKind]] = []
    non_remediable: list[str] = []
    for finding in actionable:
        kind = _classify(finding)
        if kind is None:
            non_remediable.append(f"{finding.code}:{finding.subject or '-'}")
        else:
            classified.append((finding, kind))

    round_id = _round_id(feature, report)
    if non_remediable:
        current_round = _round(
            len(prior_rounds) + 1,
            report,
            feature_id=feature,
            tasks=(),
            non_remediable=tuple(non_remediable),
        )
        state = ConvergenceState(
            feature_id=feature,
            risk=selected_risk,
            max_rounds=max_rounds,
            status=ConvergenceStatus.ESCALATED,
            escalation_reason=EscalationReason.NON_REMEDIABLE,
            current_verification_report_sha256=report.sha256,
            current_verification_input_sha256=report.input_sha256,
            rounds=prior_rounds + (current_round,),
            tasks=prior_tasks,
        )
        _persist_state(root, state)
        return state

    if prior_rounds and prior_rounds[-1].finding_signature == _finding_signature(report):
        current_round = _round(
            len(prior_rounds) + 1,
            report,
            feature_id=feature,
            tasks=(),
            non_remediable=(),
        )
        state = ConvergenceState(
            feature_id=feature,
            risk=selected_risk,
            max_rounds=max_rounds,
            status=ConvergenceStatus.ESCALATED,
            escalation_reason=EscalationReason.NO_PROGRESS,
            current_verification_report_sha256=report.sha256,
            current_verification_input_sha256=report.input_sha256,
            rounds=prior_rounds + (current_round,),
            tasks=prior_tasks,
        )
        _persist_state(root, state)
        return state

    tasks = tuple(
        _task_for_finding(feature, round_id, report, finding, kind)
        for finding, kind in sorted(
            classified,
            key=lambda item: (
                item[0].code,
                item[0].category.value,
                item[0].subject or "",
                _finding_key(item[0]),
            ),
        )
    )
    if not tasks:
        current_round = _round(
            len(prior_rounds) + 1,
            report,
            feature_id=feature,
            tasks=(),
            non_remediable=("no-actionable-remediation",),
        )
        state = ConvergenceState(
            feature_id=feature,
            risk=selected_risk,
            max_rounds=max_rounds,
            status=ConvergenceStatus.ESCALATED,
            escalation_reason=EscalationReason.NON_REMEDIABLE,
            current_verification_report_sha256=report.sha256,
            current_verification_input_sha256=report.input_sha256,
            rounds=prior_rounds + (current_round,),
            tasks=prior_tasks,
        )
        _persist_state(root, state)
        return state

    current_round = _round(
        len(prior_rounds) + 1,
        report,
        feature_id=feature,
        tasks=tasks,
        non_remediable=(),
    )
    all_tasks = tuple(prior_tasks) + tasks
    state = ConvergenceState(
        feature_id=feature,
        risk=selected_risk,
        max_rounds=max_rounds,
        status=ConvergenceStatus.ACTION_REQUIRED,
        escalation_reason=None,
        current_verification_report_sha256=report.sha256,
        current_verification_input_sha256=report.input_sha256,
        rounds=prior_rounds + (current_round,),
        tasks=all_tasks,
    )
    _persist_state(root, state)
    return state


__all__ = [
    "CONVERGENCE_STATE_API_VERSION",
    "REMEDIATION_TASK_API_VERSION",
    "ConvergenceError",
    "ConvergenceRound",
    "ConvergenceState",
    "ConvergenceStatus",
    "EscalationReason",
    "RemediationKind",
    "RemediationTask",
    "convergence_state_path",
    "load_convergence_state",
    "run_convergence",
]
