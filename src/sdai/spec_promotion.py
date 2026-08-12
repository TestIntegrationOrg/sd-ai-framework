from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Iterator

import yaml

from sdai.governance import ApprovalPolicy, load_approval_policies
from sdai.path_safety import ensure_within_project
from sdai.spec_changes import (
    CurrentSpecification,
    DeltaOperation,
    DeltaOperationKind,
    change_dir,
    current_spec_path,
    load_current_spec,
    load_spec_change,
)
from sdai.spec_validation import (
    CurrentRequirement,
    DeltaValidationReport,
    ParallelConflictReport,
    detect_parallel_change_conflicts,
    parse_current_requirements,
    spec_change_bundle_sha256,
    validate_spec_change,
)
from sdai.text import read_utf8_text


class SpecPromotionError(RuntimeError):
    pass


PROMOTION_GATE = "spec-promotion"


@dataclass(frozen=True)
class SemanticSpecChange:
    domain: str
    op: str
    requirement_id: str
    new_requirement_id: str | None
    section: str | None
    before_definition: str | None
    after_definition: str | None
    reason: str

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "domain": self.domain,
            "op": self.op,
            "requirement_id": self.requirement_id,
            "reason": self.reason,
        }
        for key, value in (
            ("new_requirement_id", self.new_requirement_id),
            ("section", self.section),
            ("before_definition", self.before_definition),
            ("after_definition", self.after_definition),
        ):
            if value is not None:
                result[key] = value
        return result


@dataclass(frozen=True)
class DomainSpecDiff:
    domain: str
    before_sha256: str | None
    after_sha256: str
    source: str
    proposed_content: str
    changes: tuple[SemanticSpecChange, ...]

    def as_dict(self, *, include_content: bool = False) -> dict[str, object]:
        result: dict[str, object] = {
            "domain": self.domain,
            "before_sha256": self.before_sha256,
            "after_sha256": self.after_sha256,
            "source": self.source,
            "changes": [item.as_dict() for item in self.changes],
        }
        if include_content:
            result["proposed_content"] = self.proposed_content
        return result


@dataclass(frozen=True)
class SpecDiffReport:
    feature_id: str
    change_sha256: str
    domains: tuple[DomainSpecDiff, ...]
    parallel_conflicts: ParallelConflictReport

    def as_dict(self, *, include_content: bool = False) -> dict[str, object]:
        return {
            "version": 1,
            "feature_id": self.feature_id,
            "change_sha256": self.change_sha256,
            "domains": [item.as_dict(include_content=include_content) for item in self.domains],
            "parallel_conflicts": self.parallel_conflicts.as_dict(),
        }

    def to_json(self, *, include_content: bool = False) -> str:
        return json.dumps(
            self.as_dict(include_content=include_content),
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
        )


@dataclass(frozen=True)
class PromotionApprovalDecision:
    gate: str
    feature_id: str
    change_sha256: str
    current_spec_sha256: dict[str, str | None]
    satisfied: bool
    approvals: int
    required: int
    missing_roles: tuple[str, ...]
    stale: bool
    detail: str
    identities: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "version": 1,
            "gate": self.gate,
            "feature_id": self.feature_id,
            "change_sha256": self.change_sha256,
            "current_spec_sha256": {
                key: self.current_spec_sha256[key]
                for key in sorted(self.current_spec_sha256)
            },
            "satisfied": self.satisfied,
            "approvals": self.approvals,
            "required": self.required,
            "missing_roles": list(self.missing_roles),
            "stale": self.stale,
            "detail": self.detail,
            "identities": list(self.identities),
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), indent=2, sort_keys=True, ensure_ascii=False)


@dataclass(frozen=True)
class PromotionPreview:
    diff: SpecDiffReport
    approval: PromotionApprovalDecision

    @property
    def eligible(self) -> bool:
        return self.approval.satisfied

    def as_dict(self, *, include_content: bool = False) -> dict[str, object]:
        return {
            "version": 1,
            "feature_id": self.diff.feature_id,
            "eligible": self.eligible,
            "diff": self.diff.as_dict(include_content=include_content),
            "approval": self.approval.as_dict(),
        }

    def to_json(self, *, include_content: bool = False) -> str:
        return json.dumps(
            self.as_dict(include_content=include_content),
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
        )


@dataclass(frozen=True)
class PromotionResult:
    feature_id: str
    promotion_id: str
    change_sha256: str
    archive_path: str
    before_sha256: dict[str, str | None]
    after_sha256: dict[str, str]
    approved_by: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "version": 1,
            "feature_id": self.feature_id,
            "promotion_id": self.promotion_id,
            "change_sha256": self.change_sha256,
            "archive_path": self.archive_path,
            "before_sha256": {
                key: self.before_sha256[key] for key in sorted(self.before_sha256)
            },
            "after_sha256": {
                key: self.after_sha256[key] for key in sorted(self.after_sha256)
            },
            "approved_by": list(self.approved_by),
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), indent=2, sort_keys=True, ensure_ascii=False)


_HEADING = re.compile(r"^##\s+(.+?)\s*$")
_REQUIREMENT_LINE = re.compile(
    r"^(?P<prefix>\s*-\s*)(?P<id>[A-Za-z0-9][A-Za-z0-9._-]{0,126})"
    r"(?P<sep>\s*:\s*)(?P<definition>.+?)\s*$"
)
_FAMILY_SUFFIX = re.compile(r"(?:[-._]\d+)$")


def _fail(code: str, message: str) -> SpecPromotionError:
    return SpecPromotionError(f"{code}: {message}")


def _normalized_hash(text: str) -> str:
    return "sha256:" + sha256(text.encode("utf-8")).hexdigest()


def _portable(root: Path, path: Path) -> str:
    safe = ensure_within_project(root, path, label="spec promotion path")
    return safe.relative_to(root.resolve()).as_posix()


def _require_valid(report: DeltaValidationReport) -> None:
    if report.valid:
        return
    codes = ", ".join(sorted({finding.code for finding in report.findings}))
    raise _fail(
        "SDAI-SPECPROMO-001",
        f"change '{report.feature_id}' is not promotable; validation findings: {codes}",
    )


def _family(requirement_id: str) -> str:
    return _FAMILY_SUFFIX.sub("", requirement_id).casefold()


def _known_section(requirement_id: str) -> str | None:
    upper = requirement_id.upper()
    if upper.startswith("NFR-"):
        return "Non-Functional Requirements"
    if upper.startswith("FR-"):
        return "Functional Requirements"
    if upper.startswith("AC-"):
        return "Acceptance Criteria"
    return None


def _section_positions(lines: list[str], section: str) -> tuple[int, int] | None:
    headings: list[tuple[int, str]] = []
    for index, line in enumerate(lines):
        match = _HEADING.match(line)
        if match:
            headings.append((index, match.group(1).strip()))
    matches = [item for item in headings if item[1].casefold() == section.casefold()]
    if len(matches) > 1:
        raise _fail(
            "SDAI-SPECPROMO-002",
            f"current specification contains duplicate section heading '{section}'",
        )
    if not matches:
        return None
    start = matches[0][0]
    later = [index for index, _ in headings if index > start]
    return start, min(later) if later else len(lines)


def _existing_section(lines: list[str], section: str) -> str | None:
    positions = _section_positions(lines, section)
    if positions is None:
        return None
    return _HEADING.match(lines[positions[0]]).group(1).strip()  # type: ignore[union-attr]


def _target_section(
    operation: DeltaOperation,
    requirements: tuple[CurrentRequirement, ...],
    requirement_sections: tuple[str, ...],
    lines: list[str],
) -> str:
    known = _known_section(operation.requirement_id)
    if known:
        return _existing_section(lines, known) or known
    sections = {
        item.section
        for item in requirements
        if _family(item.requirement_id) == _family(operation.requirement_id)
    }
    if len(sections) == 1:
        return next(iter(sections))
    if len(requirement_sections) == 1:
        return requirement_sections[0]
    return _existing_section(lines, "Requirements") or "Requirements"


def _append_to_section(lines: list[str], section: str, additions: list[str]) -> None:
    positions = _section_positions(lines, section)
    if positions is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend([f"## {section}", *additions])
        return
    start, end = positions
    insertion = end
    while insertion > start + 1 and not lines[insertion - 1].strip():
        insertion -= 1
    lines[insertion:insertion] = additions


def _replace_line(
    line: str,
    old_id: str,
    new_id: str,
    definition: str,
) -> str:
    match = _REQUIREMENT_LINE.match(line)
    if match is None or match.group("id") != old_id:
        raise _fail(
            "SDAI-SPECPROMO-002",
            f"cannot deterministically rewrite requirement '{old_id}'",
        )
    return f"{match.group('prefix')}{new_id}{match.group('sep')}{definition.strip()}"


def _render_existing(
    current: CurrentSpecification,
    operations: tuple[DeltaOperation, ...],
) -> tuple[str, tuple[SemanticSpecChange, ...]]:
    index = parse_current_requirements(current)
    if index.duplicate_ids:
        raise _fail(
            "SDAI-SPECPROMO-002",
            "cannot render current truth with duplicate requirement identities",
        )
    by_line = {item.line: item for item in index.requirements}
    source_ops = {
        item.requirement_id: item
        for item in operations
        if item.op is not DeltaOperationKind.ADDED
    }
    rendered: list[str] = []
    changes: list[SemanticSpecChange] = []
    for line_number, line in enumerate(current.content.splitlines(), start=1):
        requirement = by_line.get(line_number)
        operation = source_ops.get(requirement.requirement_id) if requirement else None
        if requirement is None or operation is None:
            rendered.append(line)
            continue
        if operation.op is DeltaOperationKind.REMOVED:
            changes.append(
                SemanticSpecChange(
                    current.domain,
                    operation.op.value,
                    requirement.requirement_id,
                    None,
                    requirement.section,
                    requirement.definition,
                    None,
                    operation.reason,
                )
            )
            continue
        if operation.op is DeltaOperationKind.MODIFIED:
            assert operation.definition is not None
            rendered.append(
                _replace_line(
                    line,
                    requirement.requirement_id,
                    requirement.requirement_id,
                    operation.definition,
                )
            )
            changes.append(
                SemanticSpecChange(
                    current.domain,
                    operation.op.value,
                    requirement.requirement_id,
                    None,
                    requirement.section,
                    requirement.definition,
                    operation.definition,
                    operation.reason,
                )
            )
            continue
        if operation.op is DeltaOperationKind.RENAMED:
            assert operation.new_requirement_id is not None
            rendered.append(
                _replace_line(
                    line,
                    requirement.requirement_id,
                    operation.new_requirement_id,
                    requirement.definition,
                )
            )
            changes.append(
                SemanticSpecChange(
                    current.domain,
                    operation.op.value,
                    requirement.requirement_id,
                    operation.new_requirement_id,
                    requirement.section,
                    requirement.definition,
                    requirement.definition,
                    operation.reason,
                )
            )
            continue
        rendered.append(line)

    additions: dict[str, list[DeltaOperation]] = {}
    order: list[str] = []
    for operation in operations:
        if operation.op is not DeltaOperationKind.ADDED:
            continue
        section = _target_section(
            operation,
            index.requirements,
            index.requirement_sections,
            rendered,
        )
        if section not in additions:
            additions[section] = []
            order.append(section)
        additions[section].append(operation)
    for section in order:
        _append_to_section(
            rendered,
            section,
            [
                f"- {operation.requirement_id}: {operation.definition.strip()}"
                for operation in additions[section]
                if operation.definition is not None
            ],
        )
        for operation in additions[section]:
            changes.append(
                SemanticSpecChange(
                    current.domain,
                    operation.op.value,
                    operation.requirement_id,
                    None,
                    section,
                    None,
                    operation.definition,
                    operation.reason,
                )
            )
    return "\n".join(rendered).rstrip() + "\n", tuple(changes)


def _render_new(
    domain: str,
    operations: tuple[DeltaOperation, ...],
) -> tuple[str, tuple[SemanticSpecChange, ...]]:
    lines = [f"# {domain}"]
    additions: dict[str, list[DeltaOperation]] = {}
    order: list[str] = []
    for operation in operations:
        if operation.op is not DeltaOperationKind.ADDED:
            raise _fail(
                "SDAI-SPECPROMO-002",
                f"new domain '{domain}' may contain only ADDED operations",
            )
        section = _known_section(operation.requirement_id) or "Requirements"
        if section not in additions:
            additions[section] = []
            order.append(section)
        additions[section].append(operation)
    changes: list[SemanticSpecChange] = []
    for section in order:
        _append_to_section(
            lines,
            section,
            [
                f"- {operation.requirement_id}: {operation.definition.strip()}"
                for operation in additions[section]
                if operation.definition is not None
            ],
        )
        for operation in additions[section]:
            changes.append(
                SemanticSpecChange(
                    domain,
                    operation.op.value,
                    operation.requirement_id,
                    None,
                    section,
                    None,
                    operation.definition,
                    operation.reason,
                )
            )
    return "\n".join(lines).rstrip() + "\n", tuple(changes)


def _verify_proposed(
    domain: str,
    proposed: str,
    operations: tuple[DeltaOperation, ...],
    source: str,
) -> str:
    digest = _normalized_hash(proposed)
    index = parse_current_requirements(
        CurrentSpecification(domain, proposed, digest, source)
    )
    if index.duplicate_ids:
        raise _fail(
            "SDAI-SPECPROMO-003",
            "proposed current specification contains duplicate requirement identities: "
            + ", ".join(index.duplicate_ids),
        )
    by_id = index.by_id()
    for operation in operations:
        if operation.op is DeltaOperationKind.ADDED:
            assert operation.definition is not None
            found = by_id.get(operation.requirement_id)
            if found is None or found.definition != operation.definition.strip():
                raise _fail(
                    "SDAI-SPECPROMO-003",
                    f"proposed specification did not apply ADDED '{operation.requirement_id}' exactly",
                )
        elif operation.op is DeltaOperationKind.MODIFIED:
            assert operation.definition is not None
            found = by_id.get(operation.requirement_id)
            if found is None or found.definition != operation.definition.strip():
                raise _fail(
                    "SDAI-SPECPROMO-003",
                    f"proposed specification did not apply MODIFIED '{operation.requirement_id}' exactly",
                )
        elif operation.op is DeltaOperationKind.REMOVED:
            if operation.requirement_id in by_id:
                raise _fail(
                    "SDAI-SPECPROMO-003",
                    f"proposed specification still contains REMOVED '{operation.requirement_id}'",
                )
        else:
            assert operation.new_requirement_id is not None
            if (
                operation.requirement_id in by_id
                or operation.new_requirement_id not in by_id
            ):
                raise _fail(
                    "SDAI-SPECPROMO-003",
                    f"proposed specification did not apply RENAMED '{operation.requirement_id}' "
                    f"-> '{operation.new_requirement_id}'",
                )
    if operations and not index.requirements:
        raise _fail(
            "SDAI-SPECPROMO-003",
            f"proposed current specification for '{domain}' has no structured requirements",
        )
    return digest


def _relevant_parallel(root: Path, feature_id: str) -> ParallelConflictReport:
    all_conflicts = detect_parallel_change_conflicts(root)
    findings = tuple(
        finding
        for finding in all_conflicts.findings
        if feature_id in finding.related_features
    )
    feature_ids = sorted(
        {feature_id}
        | {
            related
            for finding in findings
            for related in finding.related_features
        }
    )
    return ParallelConflictReport(tuple(feature_ids), findings)


def build_spec_diff(project_root: Path, feature_id: str) -> SpecDiffReport:
    root = project_root.resolve()
    validation = validate_spec_change(root, feature_id)
    _require_valid(validation)
    bundle = load_spec_change(root, feature_id)
    domains: list[DomainSpecDiff] = []
    for delta in bundle.deltas:
        target = current_spec_path(root, delta.domain)
        source = target.relative_to(root).as_posix()
        if bundle.metadata.baselines[delta.domain] is None:
            proposed, changes = _render_new(delta.domain, delta.operations)
            before = None
        else:
            current = load_current_spec(root, delta.domain)
            proposed, changes = _render_existing(current, delta.operations)
            before = current.sha256
        after = _verify_proposed(delta.domain, proposed, delta.operations, source)
        domains.append(
            DomainSpecDiff(
                delta.domain,
                before,
                after,
                source,
                proposed,
                changes,
            )
        )
    return SpecDiffReport(
        bundle.metadata.feature_id,
        spec_change_bundle_sha256(bundle),
        tuple(domains),
        _relevant_parallel(root, bundle.metadata.feature_id),
    )


def _approval_path(root: Path, feature_id: str) -> Path:
    change_root = change_dir(root, feature_id)
    return ensure_within_project(
        change_root,
        change_root / "approvals" / f"{PROMOTION_GATE}.yaml",
        label="spec promotion approval",
    )


def _approval_policy(root: Path) -> ApprovalPolicy:
    return load_approval_policies(root).get(
        PROMOTION_GATE,
        ApprovalPolicy(gate=PROMOTION_GATE),
    )


def _load_approval_document(
    root: Path,
    feature_id: str,
) -> dict[str, object] | None:
    path = _approval_path(root, feature_id)
    if not path.is_file():
        return None
    try:
        raw = yaml.safe_load(read_utf8_text(path)) or {}
    except (yaml.YAMLError, OSError) as exc:
        raise _fail(
            "SDAI-SPECPROMO-004",
            f"unable to read valid promotion approval YAML: {exc}",
        ) from exc
    if not isinstance(raw, dict):
        raise _fail(
            "SDAI-SPECPROMO-004",
            "promotion approval artifact must be a YAML mapping",
        )
    return raw


def _evaluate_document(
    root: Path,
    report: DeltaValidationReport,
    document: dict[str, object] | None,
) -> PromotionApprovalDecision:
    policy = _approval_policy(root)
    current_hashes = report.current_spec_sha256
    if document is None:
        return PromotionApprovalDecision(
            PROMOTION_GATE,
            report.feature_id,
            report.change_sha256,
            current_hashes,
            False,
            0,
            policy.min_approvals,
            policy.required_roles,
            False,
            "no promotion approval artifact",
        )
    stale = (
        document.get("version") != 1
        or document.get("gate") != PROMOTION_GATE
        or document.get("feature_id") != report.feature_id
        or document.get("change_sha256") != report.change_sha256
        or document.get("current_spec_sha256") != current_hashes
    )
    raw = document.get("approvals") or []
    if not isinstance(raw, list) or not all(isinstance(item, dict) for item in raw):
        raise _fail(
            "SDAI-SPECPROMO-004",
            "promotion approval artifact approvals must be a mapping list",
        )
    if stale:
        return PromotionApprovalDecision(
            PROMOTION_GATE,
            report.feature_id,
            report.change_sha256,
            current_hashes,
            False,
            0,
            policy.min_approvals,
            policy.required_roles,
            True,
            "promotion approval is stale because change/current-spec evidence changed",
        )

    eligible: list[dict[object, object]] = []
    disallowed: set[str] = set()
    for item in raw:
        identity = str(item.get("approved_by") or "").strip()
        if not identity:
            continue
        if policy.allowed_approvers and identity not in policy.allowed_approvers:
            disallowed.add(identity)
            continue
        eligible.append(item)
    identities = tuple(
        sorted({str(item.get("approved_by") or "").strip() for item in eligible})
    )
    roles = {
        str(item.get("role") or "").strip()
        for item in eligible
        if str(item.get("role") or "").strip()
    }
    missing_roles = tuple(
        role for role in policy.required_roles if role not in roles
    )
    satisfied = len(identities) >= policy.min_approvals and not missing_roles
    detail = (
        f"{len(identities)}/{policy.min_approvals} distinct approvals; "
        f"missing roles={','.join(missing_roles) or '-'}; "
        f"ignored disallowed={','.join(sorted(disallowed)) or '-'}"
    )
    return PromotionApprovalDecision(
        PROMOTION_GATE,
        report.feature_id,
        report.change_sha256,
        current_hashes,
        satisfied,
        len(identities),
        policy.min_approvals,
        missing_roles,
        False,
        detail,
        identities,
    )


def evaluate_promotion_approval(
    project_root: Path,
    feature_id: str,
) -> PromotionApprovalDecision:
    root = project_root.resolve()
    report = validate_spec_change(root, feature_id)
    _require_valid(report)
    return _evaluate_document(root, report, _load_approval_document(root, feature_id))


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="\n",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    )
    temp = Path(handle.name)
    try:
        with handle:
            handle.write(content.rstrip() + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    except Exception:
        temp.unlink(missing_ok=True)
        raise


def record_promotion_approval(
    project_root: Path,
    feature_id: str,
    *,
    approved_by: str,
    role: str = "",
    note: str = "",
) -> PromotionApprovalDecision:
    root = project_root.resolve()
    approved_by = approved_by.strip()
    role = role.strip()
    if not approved_by:
        raise _fail("SDAI-SPECPROMO-004", "approved_by is required")
    report = validate_spec_change(root, feature_id)
    _require_valid(report)
    policy = _approval_policy(root)
    if policy.allowed_approvers and approved_by not in policy.allowed_approvers:
        raise _fail(
            "SDAI-SPECPROMO-004",
            f"approver '{approved_by}' is not allowed for gate '{PROMOTION_GATE}'",
        )
    if policy.required_roles and role not in policy.required_roles:
        raise _fail(
            "SDAI-SPECPROMO-004",
            f"gate '{PROMOTION_GATE}' requires one of these roles: "
            + ", ".join(policy.required_roles),
        )
    existing = _load_approval_document(root, feature_id)
    if (
        existing is None
        or existing.get("change_sha256") != report.change_sha256
        or existing.get("current_spec_sha256") != report.current_spec_sha256
    ):
        approvals: list[dict[str, str]] = []
    else:
        raw = existing.get("approvals") or []
        if not isinstance(raw, list) or not all(isinstance(item, dict) for item in raw):
            raise _fail(
                "SDAI-SPECPROMO-004",
                "promotion approval artifact approvals must be a mapping list",
            )
        approvals = [
            {
                "approved_by": str(item.get("approved_by") or ""),
                "approved_at": str(item.get("approved_at") or ""),
                "role": str(item.get("role") or ""),
                "note": str(item.get("note") or ""),
            }
            for item in raw
            if str(item.get("approved_by") or "").strip() != approved_by
        ]
    approvals.append(
        {
            "approved_by": approved_by,
            "approved_at": datetime.now(timezone.utc).isoformat(),
            "role": role,
            "note": note.strip(),
        }
    )
    document: dict[str, object] = {
        "version": 1,
        "gate": PROMOTION_GATE,
        "feature_id": report.feature_id,
        "change_sha256": report.change_sha256,
        "current_spec_sha256": report.current_spec_sha256,
        "approvals": approvals,
    }
    decision = _evaluate_document(root, report, document)
    document["status"] = "approved" if decision.satisfied else "pending"
    _atomic_write_text(
        _approval_path(root, feature_id),
        yaml.safe_dump(document, sort_keys=False, allow_unicode=True),
    )
    return decision


def preview_promotion(project_root: Path, feature_id: str) -> PromotionPreview:
    root = project_root.resolve()
    diff = build_spec_diff(root, feature_id)
    approval = evaluate_promotion_approval(root, feature_id)
    if approval.change_sha256 != diff.change_sha256:
        raise _fail(
            "SDAI-SPECPROMO-005",
            "change changed while promotion preview was being constructed; retry",
        )
    return PromotionPreview(diff, approval)


@contextmanager
def _promotion_lock(root: Path, feature_id: str) -> Iterator[None]:
    lock_dir = ensure_within_project(
        root,
        root / "specs" / ".sdai",
        label="spec promotion lock directory",
    )
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = ensure_within_project(
        lock_dir,
        lock_dir / "promotion.lock",
        label="spec promotion lock",
    )
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise _fail(
            "SDAI-SPECPROMO-006",
            "another specification promotion appears to be in progress",
        ) from exc
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(f"feature_id: {feature_id}\n")
            handle.flush()
            os.fsync(handle.fileno())
        yield
    finally:
        lock_path.unlink(missing_ok=True)
        try:
            lock_dir.rmdir()
        except OSError:
            pass


def _stage_text(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="\n",
        dir=path.parent,
        prefix=f".{path.name}.promotion.",
        suffix=".tmp",
        delete=False,
    )
    temp = Path(handle.name)
    with handle:
        handle.write(content.rstrip() + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return temp


def _stage_bytes(path: Path, content: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="wb",
        dir=path.parent,
        prefix=f".{path.name}.rollback.",
        suffix=".tmp",
        delete=False,
    )
    temp = Path(handle.name)
    with handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    return temp


def _normalized_text_from_bytes(content: bytes) -> str:
    return content.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")


def _capture_before(domain: DomainSpecDiff, target: Path) -> bytes | None:
    if domain.before_sha256 is None:
        if target.exists():
            actual = (
                _normalized_hash(_normalized_text_from_bytes(target.read_bytes()))
                if target.is_file()
                else "<non-file>"
            )
            raise _fail(
                "SDAI-SPECPROMO-005",
                f"current specification for new domain '{domain.domain}' appeared before replacement; "
                f"expected=<absent> actual={actual}",
            )
        return None
    if not target.is_file():
        raise _fail(
            "SDAI-SPECPROMO-005",
            f"current specification for domain '{domain.domain}' disappeared before replacement; "
            f"expected={domain.before_sha256} actual=<absent>",
        )
    try:
        original = target.read_bytes()
        actual = _normalized_hash(_normalized_text_from_bytes(original))
    except (OSError, UnicodeDecodeError) as exc:
        raise _fail(
            "SDAI-SPECPROMO-005",
            f"cannot verify current specification for domain '{domain.domain}' before replacement: {exc}",
        ) from exc
    if actual != domain.before_sha256:
        raise _fail(
            "SDAI-SPECPROMO-005",
            f"current specification for domain '{domain.domain}' changed before replacement; "
            f"expected={domain.before_sha256} actual={actual}",
        )
    return original


def _rollback(applied: list[Path], originals: dict[Path, bytes | None]) -> list[str]:
    failures: list[str] = []
    for path in reversed(applied):
        original = originals[path]
        try:
            if original is None:
                path.unlink(missing_ok=True)
            else:
                staged = _stage_bytes(path, original)
                try:
                    os.replace(staged, path)
                finally:
                    staged.unlink(missing_ok=True)
        except Exception as exc:  # pragma: no cover
            failures.append(f"{path}: {exc}")
    return failures


def _promotion_id(change_sha256: str, promoted_at: datetime) -> str:
    return (
        promoted_at.strftime("%Y%m%dT%H%M%S%fZ")
        + "-"
        + change_sha256.removeprefix("sha256:")[:12]
    )


def _promotion_evidence(
    preview: PromotionPreview,
    promotion_id: str,
    promoted_at: datetime,
) -> str:
    payload = {
        "version": 1,
        "promotion_id": promotion_id,
        "feature_id": preview.diff.feature_id,
        "promoted_at": promoted_at.isoformat(),
        "change_sha256": preview.diff.change_sha256,
        "approval": preview.approval.as_dict(),
        "before_current_spec_sha256": {
            item.domain: item.before_sha256 for item in preview.diff.domains
        },
        "after_current_spec_sha256": {
            item.domain: item.after_sha256 for item in preview.diff.domains
        },
        "semantic_changes": [
            change.as_dict()
            for domain in preview.diff.domains
            for change in domain.changes
        ],
        "parallel_conflicts": preview.diff.parallel_conflicts.as_dict(),
    }
    return yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)


def promote_spec_change(project_root: Path, feature_id: str) -> PromotionResult:
    root = project_root.resolve()
    with _promotion_lock(root, feature_id):
        final_diff = build_spec_diff(root, feature_id)
        final_approval = evaluate_promotion_approval(root, feature_id)
        if final_approval.change_sha256 != final_diff.change_sha256:
            raise _fail(
                "SDAI-SPECPROMO-005",
                "change changed during promotion preparation; retry",
            )
        if not final_approval.satisfied:
            raise _fail(
                "SDAI-SPECPROMO-004",
                f"promotion approval is not satisfied: {final_approval.detail}",
            )
        preview = PromotionPreview(final_diff, final_approval)
        targets = {
            item.domain: current_spec_path(root, item.domain)
            for item in preview.diff.domains
        }
        staged: dict[Path, Path] = {}
        originals: dict[Path, bytes | None] = {}
        applied: list[Path] = []
        evidence_path: Path | None = None
        try:
            for domain in preview.diff.domains:
                target = targets[domain.domain]
                staged[target] = _stage_text(target, domain.proposed_content)
            for domain in preview.diff.domains:
                target = targets[domain.domain]
                originals[target] = _capture_before(domain, target)
                os.replace(staged[target], target)
                applied.append(target)
            for domain in preview.diff.domains:
                actual = load_current_spec(root, domain.domain).sha256
                if actual != domain.after_sha256:
                    raise _fail(
                        "SDAI-SPECPROMO-007",
                        f"post-write hash mismatch for domain '{domain.domain}'",
                    )

            promoted_at = datetime.now(timezone.utc)
            promotion_id = _promotion_id(preview.diff.change_sha256, promoted_at)
            source_change = change_dir(root, feature_id)
            evidence_path = ensure_within_project(
                source_change,
                source_change / "promotion.yaml",
                label="promotion evidence",
            )
            if evidence_path.exists():
                raise _fail(
                    "SDAI-SPECPROMO-008",
                    "change workspace already contains promotion.yaml",
                )
            _atomic_write_text(
                evidence_path,
                _promotion_evidence(preview, promotion_id, promoted_at),
            )
            archive_root = ensure_within_project(
                root,
                root / "specs" / "archive" / "changes" / feature_id,
                label="spec change archive",
            )
            archive_root.mkdir(parents=True, exist_ok=True)
            archive_target = ensure_within_project(
                archive_root,
                archive_root / promotion_id,
                label="spec promotion archive target",
            )
            if archive_target.exists():
                raise _fail(
                    "SDAI-SPECPROMO-008",
                    f"archive target already exists: {_portable(root, archive_target)}",
                )
            result = PromotionResult(
                feature_id,
                promotion_id,
                preview.diff.change_sha256,
                _portable(root, archive_target),
                {item.domain: item.before_sha256 for item in preview.diff.domains},
                {item.domain: item.after_sha256 for item in preview.diff.domains},
                preview.approval.identities,
            )
            os.replace(source_change, archive_target)
            return result
        except Exception as exc:
            for temp in staged.values():
                temp.unlink(missing_ok=True)
            if evidence_path is not None and evidence_path.exists():
                evidence_path.unlink(missing_ok=True)
            rollback_failures = _rollback(applied, originals)
            if rollback_failures:
                raise _fail(
                    "SDAI-SPECPROMO-009",
                    "promotion failed and rollback was incomplete: "
                    + "; ".join(rollback_failures),
                ) from exc
            if isinstance(exc, SpecPromotionError):
                raise
            raise _fail(
                "SDAI-SPECPROMO-007",
                f"promotion transaction failed and canonical truth was rolled back: {exc}",
            ) from exc
