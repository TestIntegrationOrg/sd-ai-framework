from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import re

from sdai.artifacts import write_text
from sdai.models import FeatureContext
from sdai.text import read_utf8_text


REVIEW_OWNER = "requirements-analyst"
IMPLEMENTATION_SELF_APPROVAL = "forbidden"


@dataclass(frozen=True)
class ClarificationCategory:
    id: str
    name: str
    prompt: str
    evidence_terms: tuple[str, ...]


@dataclass(frozen=True)
class ClarificationFinding:
    id: str
    category: str
    status: str
    question: str
    evidence_terms: tuple[str, ...]


@dataclass(frozen=True)
class RequirementsFinding:
    id: str
    title: str
    severity: str
    status: str
    detail: str


@dataclass(frozen=True)
class RequirementsQualityReport:
    feature_id: str
    specification_sha256: str
    findings: tuple[RequirementsFinding, ...]

    @property
    def blocking_failures(self) -> tuple[RequirementsFinding, ...]:
        return tuple(
            finding
            for finding in self.findings
            if finding.severity == "blocking" and finding.status == "fail"
        )

    @property
    def warning_failures(self) -> tuple[RequirementsFinding, ...]:
        return tuple(
            finding
            for finding in self.findings
            if finding.severity == "warning" and finding.status == "fail"
        )


CLARIFICATION_CATEGORIES: tuple[ClarificationCategory, ...] = (
    ClarificationCategory(
        "CLAR-001",
        "Functional behavior",
        "What exact externally observable behavior must the feature provide?",
        ("functional requirement", "must", "acceptance criteria"),
    ),
    ClarificationCategory(
        "CLAR-002",
        "Actors and permissions",
        "Who may invoke or administer the capability, and what authorization boundaries apply?",
        ("actor", "user", "caller", "role", "permission", "authorization"),
    ),
    ClarificationCategory(
        "CLAR-003",
        "Inputs and outputs",
        "What inputs are accepted, what validation applies, and what exact outputs/contracts are returned?",
        ("input", "output", "request", "response", "payload", "file"),
    ),
    ClarificationCategory(
        "CLAR-004",
        "Errors and failure modes",
        "What failures can occur, how are they surfaced, and which are retryable versus terminal?",
        ("error", "failure", "invalid", "timeout", "retry"),
    ),
    ClarificationCategory(
        "CLAR-005",
        "Edge cases and boundaries",
        "What boundary, empty, maximum/minimum, duplicate, or malformed cases require defined behavior?",
        (
            "edge",
            "boundary",
            "empty",
            "maximum",
            "minimum",
            "duplicate",
            "malformed",
        ),
    ),
    ClarificationCategory(
        "CLAR-006",
        "State and lifecycle",
        "What states/statuses exist and which transitions, cancellation, or terminal conditions are valid?",
        ("state", "status", "transition", "lifecycle", "cancel"),
    ),
    ClarificationCategory(
        "CLAR-007",
        "Performance and scale",
        "What measurable latency, throughput, concurrency, payload-size, or scale targets apply?",
        ("latency", "throughput", "performance", "scalability", "concurrency", "scale"),
    ),
    ClarificationCategory(
        "CLAR-008",
        "Security and privacy",
        "What trust boundaries, sensitive data, authentication/authorization, cryptographic, or privacy constraints apply?",
        (
            "security",
            "privacy",
            "authentication",
            "authorization",
            "trust",
            "cryptograph",
        ),
    ),
    ClarificationCategory(
        "CLAR-009",
        "Compatibility and migration",
        "What existing consumers/contracts must remain compatible and what migration behavior is required?",
        ("compatibility", "backward", "migration", "consumer", "breaking"),
    ),
    ClarificationCategory(
        "CLAR-010",
        "Observability",
        "Which logs, metrics, traces, correlation identifiers, and alerts prove the behavior in production?",
        ("observability", "log", "metric", "trace", "alert", "correlation"),
    ),
    ClarificationCategory(
        "CLAR-011",
        "Deployment and rollout",
        "How is the capability deployed, enabled, phased, and verified during rollout?",
        ("deployment", "deploy", "rollout", "release", "feature flag"),
    ),
    ClarificationCategory(
        "CLAR-012",
        "Rollback and recovery",
        "What rollback, recovery, retry, idempotency, or compensating-action behavior is required?",
        ("rollback", "recovery", "revert", "idempot", "compensat"),
    ),
    ClarificationCategory(
        "CLAR-013",
        "Retention and deletion",
        "What retention, deletion, archival, purge, or lifecycle rules apply to persisted data/artifacts?",
        ("retention", "delete", "deletion", "archive", "purge"),
    ),
    ClarificationCategory(
        "CLAR-014",
        "Compliance and audit",
        "What regulatory, audit, evidence, residency, or organizational compliance requirements apply?",
        ("compliance", "regulatory", "audit", "residency", "evidence"),
    ),
)


_REQUIRED_SECTIONS = (
    "Problem",
    "Goals",
    "Functional Requirements",
    "Non-Functional Requirements",
    "Acceptance Criteria",
)
_PLACEHOLDER = re.compile(
    r"\b(TBD|TODO|TBC|FIXME)\b|\[(?:placeholder|fill[- ]?in)\]",
    re.IGNORECASE,
)
_REQUIREMENT_LINE = re.compile(
    r"^\s*-\s*((?:FR|NFR|AC)-\d{3})\s*:\s*(.+?)\s*$",
    re.MULTILINE,
)
_MEASURABLE = re.compile(
    r"\b\d+(?:\.\d+)?\s*(?:ms|s|sec|seconds?|minutes?|rps|qps|tps|%|percent|mb|gb|kb|requests?|users?|devices?|concurrent)\b",
    re.IGNORECASE,
)


def _specification(
    project_root: Path,
    feature_id: str,
) -> tuple[FeatureContext, Path, str]:
    context = FeatureContext(project_root, feature_id)
    path = context.artifact("specification.md")
    if not path.exists():
        raise FileNotFoundError(
            f"Feature '{context.feature_id}' has no specification.md; "
            "run the requirements/specification step first"
        )
    return context, path, read_utf8_text(path)


def _sections(markdown: str) -> dict[str, str]:
    headings = list(
        re.finditer(r"^##\s+(.+?)\s*$", markdown, flags=re.MULTILINE)
    )
    result: dict[str, str] = {}
    for index, match in enumerate(headings):
        start = match.end()
        end = (
            headings[index + 1].start()
            if index + 1 < len(headings)
            else len(markdown)
        )
        result[match.group(1).strip()] = markdown[start:end].strip()
    return result


def analyze_clarifications(
    project_root: Path,
    feature_id: str,
) -> tuple[ClarificationFinding, ...]:
    _, _, specification = _specification(project_root, feature_id)
    folded = specification.casefold()
    findings: list[ClarificationFinding] = []
    for category in CLARIFICATION_CATEGORIES:
        evidence = tuple(
            term for term in category.evidence_terms if term in folded
        )
        # Keyword evidence is intentionally not treated as reviewer approval. It only
        # reduces obvious noise and remains explicitly marked for human/analyst review.
        status = "candidate-covered" if evidence else "open"
        findings.append(
            ClarificationFinding(
                id=category.id,
                category=category.name,
                status=status,
                question=category.prompt,
                evidence_terms=evidence,
            )
        )
    return tuple(findings)


def write_clarifications(project_root: Path, feature_id: str) -> Path:
    context, spec_path, specification = _specification(project_root, feature_id)
    findings = analyze_clarifications(project_root, feature_id)
    digest = sha256(specification.encode("utf-8")).hexdigest()
    lines = [
        "---",
        "version: 1",
        f"feature: {context.feature_id}",
        f"specification_sha256: {digest}",
        f"review_owner: {REVIEW_OWNER}",
        "approval_status: pending",
        f"implementation_self_approval: {IMPLEMENTATION_SELF_APPROVAL}",
        "---",
        "",
        f"# Requirements Clarification — {context.feature_id}",
        "",
        f"Source: `{spec_path.relative_to(context.project_root).as_posix()}`",
        "",
        "> `candidate-covered` means the specification contains related evidence; "
        "it does not mean the question is approved or resolved.",
        "",
        "| ID | Category | Status | Question | Evidence terms |",
        "|---|---|---|---|---|",
    ]
    for finding in findings:
        evidence = (
            ", ".join(finding.evidence_terms)
            if finding.evidence_terms
            else "—"
        )
        question = finding.question.replace("|", "\\|")
        lines.append(
            f"| {finding.id} | {finding.category} | {finding.status} | "
            f"{question} | {evidence} |"
        )
    lines.extend(
        [
            "",
            "## Reviewer Notes",
            "",
            "Record answers or references here. Apply accepted changes to the "
            "canonical specification through the requirements workflow; this "
            "clarification artifact never rewrites the specification automatically.",
            "",
        ]
    )
    return write_text(
        context.artifact("quality/clarifications.md"),
        "\n".join(lines),
        overwrite=True,
    )


def _finding(
    finding_id: str,
    title: str,
    severity: str,
    passed: bool,
    detail: str,
) -> RequirementsFinding:
    return RequirementsFinding(
        id=finding_id,
        title=title,
        severity=severity,
        status="pass" if passed else "fail",
        detail=detail,
    )


def check_requirements(
    project_root: Path,
    feature_id: str,
) -> RequirementsQualityReport:
    context, _, specification = _specification(project_root, feature_id)
    sections = _sections(specification)
    folded = specification.casefold()
    requirements = _REQUIREMENT_LINE.findall(specification)
    ids = [requirement_id for requirement_id, _ in requirements]
    fr_ids = [requirement_id for requirement_id in ids if requirement_id.startswith("FR-")]
    nfr_ids = [requirement_id for requirement_id in ids if requirement_id.startswith("NFR-")]
    ac_ids = [requirement_id for requirement_id in ids if requirement_id.startswith("AC-")]
    missing_families = [
        label
        for label, values in (("FR", fr_ids), ("NFR", nfr_ids), ("AC", ac_ids))
        if not values
    ]

    missing_sections = [
        section
        for section in _REQUIRED_SECTIONS
        if not sections.get(section, "").strip()
    ]
    duplicates = sorted(
        {requirement_id for requirement_id in ids if ids.count(requirement_id) > 1}
    )
    non_normative = [
        requirement_id
        for requirement_id, statement in requirements
        if requirement_id.startswith(("FR-", "NFR-"))
        and not re.search(
            r"\b(MUST|SHALL|SHOULD)\b",
            statement,
            flags=re.IGNORECASE,
        )
    ]
    placeholders_present = _PLACEHOLDER.search(specification) is not None
    open_questions = sections.get("Open Questions", "").strip()
    open_questions_resolved = not open_questions or open_questions.casefold() in {
        "none",
        "n/a",
        "not applicable",
    }
    has_actor = any(
        term in folded
        for term in (
            "actor",
            "user",
            "caller",
            "role",
            "permission",
            "authorization",
        )
    )
    has_io = any(
        term in folded
        for term in ("input", "output", "request", "response", "payload", "file")
    )
    has_edge_or_state = any(
        term in folded
        for term in (
            "edge",
            "boundary",
            "empty",
            "maximum",
            "minimum",
            "state",
            "transition",
            "lifecycle",
        )
    )
    has_compatibility = any(
        term in folded
        for term in ("compatibility", "backward", "migration", "breaking")
    )
    has_failure_and_observability = (
        "failure" in folded or "error" in folded
    ) and any(
        term in folded
        for term in ("observability", "log", "metric", "trace")
    )
    measurable_nfr = _MEASURABLE.search(
        sections.get("Non-Functional Requirements", "")
    ) is not None
    operational_groups = (
        ("deploy", "rollout", "release"),
        ("rollback", "recovery", "revert"),
        ("retention", "delete", "archive", "purge"),
        ("compliance", "regulatory", "audit"),
    )
    operational_scope_complete = all(
        any(term in folded for term in group) for group in operational_groups
    )
    has_all_requirement_families = not missing_families
    ids_valid = has_all_requirement_families and not duplicates
    normative_valid = bool(fr_ids) and bool(nfr_ids) and not non_normative

    findings = (
        _finding(
            "RQ-001",
            "Required requirements structure is present",
            "blocking",
            not missing_sections,
            "All required sections present"
            if not missing_sections
            else f"Missing/empty sections: {', '.join(missing_sections)}",
        ),
        _finding(
            "RQ-002",
            "No unresolved placeholder markers",
            "blocking",
            not placeholders_present,
            "No TBD/TODO/TBC/FIXME placeholders detected"
            if not placeholders_present
            else "Specification contains unresolved placeholder markers",
        ),
        _finding(
            "RQ-003",
            "FR, NFR, and acceptance IDs are present and unique",
            "blocking",
            ids_valid,
            f"Found FR={len(fr_ids)}, NFR={len(nfr_ids)}, AC={len(ac_ids)} unique IDs"
            if ids_valid
            else (
                f"Duplicate IDs: {', '.join(duplicates)}"
                if duplicates
                else f"Missing structured requirement families: {', '.join(missing_families)}"
            ),
        ),
        _finding(
            "RQ-004",
            "Functional and non-functional requirements use normative language",
            "blocking",
            normative_valid,
            "All FR/NFR statements use MUST/SHALL/SHOULD"
            if normative_valid
            else (
                f"Non-normative requirements: {', '.join(non_normative)}"
                if non_normative
                else "Both FR-NNN and NFR-NNN requirements are required"
            ),
        ),
        _finding(
            "RQ-005",
            "Acceptance criteria are identifiable",
            "blocking",
            bool(ac_ids),
            "Acceptance criteria IDs are present"
            if ac_ids
            else "No AC-NNN acceptance criteria found",
        ),
        _finding(
            "RQ-006",
            "Security or privacy constraints are explicit",
            "blocking",
            "security" in folded or "privacy" in folded,
            "Security/privacy language is present"
            if "security" in folded or "privacy" in folded
            else "Security/privacy considerations are absent",
        ),
        _finding(
            "RQ-007",
            "Failure behavior and observability are explicit",
            "blocking",
            has_failure_and_observability,
            "Failure and observability language are both present"
            if has_failure_and_observability
            else "Define both failure behavior and production observability",
        ),
        _finding(
            "RQ-008",
            "Performance/scalability NFRs contain a measurable target",
            "blocking",
            measurable_nfr,
            "A measurable performance/scale target is present"
            if measurable_nfr
            else "Add at least one measurable latency/throughput/scale/payload/concurrency target",
        ),
        _finding(
            "RQ-009",
            "Compatibility or migration impact is explicit",
            "warning",
            has_compatibility,
            "Compatibility/migration language is present"
            if has_compatibility
            else "State compatibility, migration, or breaking-change impact",
        ),
        _finding(
            "RQ-010",
            "Open questions that can change implementation are resolved",
            "blocking",
            open_questions_resolved,
            "No unresolved Open Questions remain"
            if open_questions_resolved
            else "Open Questions remain; resolve or explicitly mark them not applicable",
        ),
        _finding(
            "RQ-011",
            "Actors and authorization boundaries are explicit",
            "warning",
            has_actor,
            "Actor/permission language is present"
            if has_actor
            else "Identify callers/users/roles and authorization boundaries",
        ),
        _finding(
            "RQ-012",
            "Inputs and outputs/contracts are explicit",
            "warning",
            has_io,
            "Input/output or contract language is present"
            if has_io
            else "Define accepted inputs and externally observable outputs/contracts",
        ),
        _finding(
            "RQ-013",
            "Edge cases or state transitions are bounded",
            "warning",
            has_edge_or_state,
            "Boundary/state language is present"
            if has_edge_or_state
            else "Define boundary cases and/or lifecycle state transitions",
        ),
        _finding(
            "RQ-014",
            "Clarification scope covers rollout, rollback, retention, and compliance",
            "warning",
            operational_scope_complete,
            "Operational lifecycle/compliance topics are represented"
            if operational_scope_complete
            else "Review deployment/rollout, rollback/recovery, retention/deletion, and compliance applicability",
        ),
    )
    return RequirementsQualityReport(
        feature_id=context.feature_id,
        specification_sha256=sha256(specification.encode("utf-8")).hexdigest(),
        findings=findings,
    )


def write_requirements_checklist(
    project_root: Path,
    feature_id: str,
) -> tuple[Path, RequirementsQualityReport]:
    context, spec_path, _ = _specification(project_root, feature_id)
    report = check_requirements(project_root, feature_id)
    lines = [
        "---",
        "version: 1",
        f"feature: {context.feature_id}",
        f"specification_sha256: {report.specification_sha256}",
        f"review_owner: {REVIEW_OWNER}",
        "approval_status: pending",
        f"implementation_self_approval: {IMPLEMENTATION_SELF_APPROVAL}",
        "---",
        "",
        f"# Requirements Quality Checklist — {context.feature_id}",
        "",
        f"Source: `{spec_path.relative_to(context.project_root).as_posix()}`",
        "",
        f"Blocking failures: **{len(report.blocking_failures)}**  ",
        f"Warning findings: **{len(report.warning_failures)}**",
        "",
        "| ID | Severity | Status | Quality dimension | Evidence / action |",
        "|---|---|---|---|---|",
    ]
    for finding in report.findings:
        detail = finding.detail.replace("|", "\\|")
        lines.append(
            f"| {finding.id} | {finding.severity} | {finding.status} | "
            f"{finding.title} | {detail} |"
        )
    lines.extend(
        [
            "",
            "## Reviewer Decision",
            "",
            "- Reviewer: unassigned",
            "- Decision: pending",
            "- Notes: requirements-analyst or an authorized human reviewer must "
            "resolve blocking findings. An implementation agent cannot self-approve "
            "this checklist.",
            "",
        ]
    )
    path = write_text(
        context.artifact("quality/requirements-checklist.md"),
        "\n".join(lines),
        overwrite=True,
    )
    return path, report
