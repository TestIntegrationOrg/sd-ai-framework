from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
import re

from sdai.path_safety import ensure_within_project
from sdai.spec_changes import (
    CurrentSpecification,
    DeltaOperation,
    DeltaOperationKind,
    SpecChangeBundle,
    current_spec_path,
    load_current_spec,
    load_spec_change,
    validate_change_feature_id,
    validate_requirement_id,
)


@dataclass(frozen=True)
class CurrentRequirement:
    requirement_id: str
    definition: str
    sha256: str
    section: str
    line: int

    def as_dict(self) -> dict[str, object]:
        return {
            "requirement_id": self.requirement_id,
            "definition": self.definition,
            "sha256": self.sha256,
            "section": self.section,
            "line": self.line,
        }


@dataclass(frozen=True)
class CurrentRequirementIndex:
    requirements: tuple[CurrentRequirement, ...]
    duplicate_ids: tuple[str, ...]
    requirement_sections: tuple[str, ...]

    def by_id(self) -> dict[str, CurrentRequirement]:
        result: dict[str, CurrentRequirement] = {}
        for requirement in self.requirements:
            result.setdefault(requirement.requirement_id, requirement)
        return result


@dataclass(frozen=True)
class SpecValidationFinding:
    code: str
    kind: str
    severity: str
    domain: str
    message: str
    requirement_id: str | None = None
    expected: str | None = None
    actual: str | None = None
    related_features: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "code": self.code,
            "kind": self.kind,
            "severity": self.severity,
            "domain": self.domain,
            "message": self.message,
        }
        if self.requirement_id is not None:
            payload["requirement_id"] = self.requirement_id
        if self.expected is not None:
            payload["expected"] = self.expected
        if self.actual is not None:
            payload["actual"] = self.actual
        if self.related_features:
            payload["related_features"] = list(self.related_features)
        return payload


@dataclass(frozen=True)
class DeltaValidationReport:
    feature_id: str
    change_sha256: str
    current_spec_sha256: dict[str, str | None]
    findings: tuple[SpecValidationFinding, ...]

    @property
    def valid(self) -> bool:
        return not any(finding.severity == "error" for finding in self.findings)

    def as_dict(self) -> dict[str, object]:
        return {
            "version": 1,
            "feature_id": self.feature_id,
            "change_sha256": self.change_sha256,
            "valid": self.valid,
            "current_spec_sha256": {
                domain: self.current_spec_sha256[domain]
                for domain in sorted(self.current_spec_sha256)
            },
            "findings": [finding.as_dict() for finding in self.findings],
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), indent=2, sort_keys=True, ensure_ascii=False)


@dataclass(frozen=True)
class ParallelConflictReport:
    feature_ids: tuple[str, ...]
    findings: tuple[SpecValidationFinding, ...]

    @property
    def valid(self) -> bool:
        return not self.findings

    def as_dict(self) -> dict[str, object]:
        return {
            "version": 1,
            "feature_ids": list(self.feature_ids),
            "valid": self.valid,
            "findings": [finding.as_dict() for finding in self.findings],
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), indent=2, sort_keys=True, ensure_ascii=False)


_SECTION = re.compile(r"^##\s+(.+?)\s*$")
_REQUIREMENT_LINE = re.compile(
    r"^\s*-\s*([A-Za-z0-9][A-Za-z0-9._-]{0,126})\s*:\s*(.+?)\s*$"
)


CURRENT_SPEC_MISSING = ("SDAI-SPECVAL-001", "CURRENT_SPEC_MISSING")
UNEXPECTED_CURRENT_SPEC = ("SDAI-SPECVAL-002", "UNEXPECTED_CURRENT_SPEC")
STALE_SPEC_BASELINE = ("SDAI-SPECVAL-003", "STALE_SPEC_BASELINE")
DUPLICATE_CURRENT_REQUIREMENT = (
    "SDAI-SPECVAL-004",
    "DUPLICATE_CURRENT_REQUIREMENT",
)
ADDED_REQUIREMENT_EXISTS = ("SDAI-SPECVAL-005", "ADDED_REQUIREMENT_EXISTS")
TARGET_REQUIREMENT_MISSING = ("SDAI-SPECVAL-006", "TARGET_REQUIREMENT_MISSING")
STALE_REQUIREMENT_BASELINE = (
    "SDAI-SPECVAL-007",
    "STALE_REQUIREMENT_BASELINE",
)
RENAME_DESTINATION_EXISTS = ("SDAI-SPECVAL-008", "RENAME_DESTINATION_EXISTS")
PARALLEL_CHANGE_CONFLICT = ("SDAI-SPECVAL-009", "PARALLEL_CHANGE_CONFLICT")
NO_STRUCTURED_REQUIREMENTS = ("SDAI-SPECVAL-010", "NO_STRUCTURED_REQUIREMENTS")


def requirement_sha256(requirement_id: str, definition: str) -> str:
    identifier = validate_requirement_id(requirement_id)
    normalized_definition = definition.strip()
    if not normalized_definition:
        raise ValueError("requirement definition must be non-empty")
    canonical = f"{identifier}:{normalized_definition}"
    return "sha256:" + sha256(canonical.encode("utf-8")).hexdigest()


def spec_change_bundle_sha256(bundle: SpecChangeBundle) -> str:
    """Hash the complete deterministic change bundle, including every delta."""

    return "sha256:" + sha256(bundle.to_json().encode("utf-8")).hexdigest()


def _is_requirement_section(heading: str) -> bool:
    folded = heading.casefold()
    return "requirement" in folded or folded == "acceptance criteria"


def parse_current_requirements(specification: CurrentSpecification) -> CurrentRequirementIndex:
    requirements: list[CurrentRequirement] = []
    section_names: list[str] = []
    current_section: str | None = None

    for line_number, line in enumerate(specification.content.splitlines(), start=1):
        heading = _SECTION.match(line)
        if heading:
            name = heading.group(1).strip()
            current_section = name if _is_requirement_section(name) else None
            if current_section is not None and current_section not in section_names:
                section_names.append(current_section)
            continue
        if current_section is None:
            continue
        match = _REQUIREMENT_LINE.match(line)
        if not match:
            continue
        requirement_id = validate_requirement_id(match.group(1))
        definition = match.group(2).strip()
        requirements.append(
            CurrentRequirement(
                requirement_id=requirement_id,
                definition=definition,
                sha256=requirement_sha256(requirement_id, definition),
                section=current_section,
                line=line_number,
            )
        )

    counts: dict[str, int] = {}
    for requirement in requirements:
        counts[requirement.requirement_id] = counts.get(requirement.requirement_id, 0) + 1
    duplicates = tuple(sorted(key for key, count in counts.items() if count > 1))
    return CurrentRequirementIndex(
        requirements=tuple(requirements),
        duplicate_ids=duplicates,
        requirement_sections=tuple(section_names),
    )


def _finding(
    pair: tuple[str, str],
    domain: str,
    message: str,
    *,
    requirement_id: str | None = None,
    expected: str | None = None,
    actual: str | None = None,
    related_features: tuple[str, ...] = (),
) -> SpecValidationFinding:
    code, kind = pair
    return SpecValidationFinding(
        code=code,
        kind=kind,
        severity="error",
        domain=domain,
        message=message,
        requirement_id=requirement_id,
        expected=expected,
        actual=actual,
        related_features=related_features,
    )


def _operation_findings(
    domain: str,
    operation: DeltaOperation,
    current: dict[str, CurrentRequirement],
) -> list[SpecValidationFinding]:
    findings: list[SpecValidationFinding] = []
    existing = current.get(operation.requirement_id)

    if operation.op is DeltaOperationKind.ADDED:
        if existing is not None:
            findings.append(
                _finding(
                    ADDED_REQUIREMENT_EXISTS,
                    domain,
                    f"ADDED requirement '{operation.requirement_id}' already exists in current truth",
                    requirement_id=operation.requirement_id,
                    actual=existing.sha256,
                )
            )
        return findings

    if existing is None:
        findings.append(
            _finding(
                TARGET_REQUIREMENT_MISSING,
                domain,
                f"{operation.op.value} target '{operation.requirement_id}' does not exist in current truth",
                requirement_id=operation.requirement_id,
                expected=operation.previous_hash,
            )
        )
        return findings

    if operation.previous_hash != existing.sha256:
        findings.append(
            _finding(
                STALE_REQUIREMENT_BASELINE,
                domain,
                f"{operation.op.value} target '{operation.requirement_id}' changed since the delta was authored",
                requirement_id=operation.requirement_id,
                expected=operation.previous_hash,
                actual=existing.sha256,
            )
        )

    if operation.op is DeltaOperationKind.RENAMED:
        assert operation.new_requirement_id is not None
        destination = current.get(operation.new_requirement_id)
        if destination is not None:
            findings.append(
                _finding(
                    RENAME_DESTINATION_EXISTS,
                    domain,
                    f"RENAMED destination '{operation.new_requirement_id}' already exists in current truth",
                    requirement_id=operation.new_requirement_id,
                    actual=destination.sha256,
                )
            )
    return findings


def _validate_domain(
    project_root: Path,
    bundle: SpecChangeBundle,
    domain: str,
) -> tuple[str | None, list[SpecValidationFinding]]:
    delta = next(item for item in bundle.deltas if item.domain == domain)
    expected_spec_hash = bundle.metadata.baselines[domain]
    path = current_spec_path(project_root, domain)
    current_exists = path.is_file()
    findings: list[SpecValidationFinding] = []

    if expected_spec_hash is None:
        if current_exists:
            current = load_current_spec(project_root, domain)
            findings.append(
                _finding(
                    UNEXPECTED_CURRENT_SPEC,
                    domain,
                    f"change expects new domain '{domain}', but current truth already exists",
                    expected="<absent>",
                    actual=current.sha256,
                )
            )
            index = parse_current_requirements(current)
            current_by_id = index.by_id()
            for operation in delta.operations:
                findings.extend(_operation_findings(domain, operation, current_by_id))
            return current.sha256, findings

        for operation in delta.operations:
            if operation.op is not DeltaOperationKind.ADDED:
                findings.append(
                    _finding(
                        TARGET_REQUIREMENT_MISSING,
                        domain,
                        f"{operation.op.value} cannot target '{operation.requirement_id}' because domain '{domain}' has no current truth",
                        requirement_id=operation.requirement_id,
                        expected=operation.previous_hash,
                    )
                )
        return None, findings

    if not current_exists:
        findings.append(
            _finding(
                CURRENT_SPEC_MISSING,
                domain,
                f"change expects an existing current specification for domain '{domain}'",
                expected=expected_spec_hash,
                actual="<absent>",
            )
        )
        return None, findings

    current = load_current_spec(project_root, domain)
    if current.sha256 != expected_spec_hash:
        findings.append(
            _finding(
                STALE_SPEC_BASELINE,
                domain,
                f"current specification for domain '{domain}' changed since the change was authored",
                expected=expected_spec_hash,
                actual=current.sha256,
            )
        )

    index = parse_current_requirements(current)
    if not index.requirement_sections or not index.requirements:
        findings.append(
            _finding(
                NO_STRUCTURED_REQUIREMENTS,
                domain,
                f"current specification for domain '{domain}' has no structured requirement records",
            )
        )
    for duplicate in index.duplicate_ids:
        findings.append(
            _finding(
                DUPLICATE_CURRENT_REQUIREMENT,
                domain,
                f"current specification contains duplicate requirement id '{duplicate}'",
                requirement_id=duplicate,
            )
        )

    current_by_id = index.by_id()
    for operation in delta.operations:
        findings.extend(_operation_findings(domain, operation, current_by_id))
    return current.sha256, findings


def validate_spec_change(project_root: Path, feature_id: str) -> DeltaValidationReport:
    root = project_root.resolve()
    bundle = load_spec_change(root, feature_id)
    current_hashes: dict[str, str | None] = {}
    findings: list[SpecValidationFinding] = []

    for domain in bundle.metadata.domains:
        current_hash, domain_findings = _validate_domain(root, bundle, domain)
        current_hashes[domain] = current_hash
        findings.extend(domain_findings)

    ordered_findings = tuple(
        sorted(
            findings,
            key=lambda finding: (
                finding.domain,
                finding.requirement_id or "",
                finding.code,
                finding.message,
            ),
        )
    )
    return DeltaValidationReport(
        feature_id=bundle.metadata.feature_id,
        change_sha256=spec_change_bundle_sha256(bundle),
        current_spec_sha256=current_hashes,
        findings=ordered_findings,
    )


def _discover_feature_ids(project_root: Path) -> tuple[str, ...]:
    root = project_root.resolve()
    changes = ensure_within_project(
        root,
        root / "specs" / "changes",
        label="spec changes directory",
    )
    if not changes.is_dir():
        return ()
    result: list[str] = []
    for path in sorted(changes.iterdir(), key=lambda item: item.name.casefold()):
        if path.is_dir() and (path / "change.yaml").is_file():
            result.append(validate_change_feature_id(path.name))
    return tuple(sorted(result))


def detect_parallel_change_conflicts(
    project_root: Path,
    feature_ids: tuple[str, ...] | None = None,
) -> ParallelConflictReport:
    root = project_root.resolve()
    selected = (
        _discover_feature_ids(root)
        if feature_ids is None
        else tuple(sorted({validate_change_feature_id(item) for item in feature_ids}))
    )
    bundles = tuple(load_spec_change(root, feature_id) for feature_id in selected)
    footprint: dict[tuple[str, str], set[str]] = {}

    for bundle in bundles:
        for delta in bundle.deltas:
            for operation in delta.operations:
                identities = [operation.requirement_id]
                if operation.new_requirement_id is not None:
                    identities.append(operation.new_requirement_id)
                for requirement_id in identities:
                    footprint.setdefault((delta.domain, requirement_id), set()).add(
                        bundle.metadata.feature_id
                    )

    findings: list[SpecValidationFinding] = []
    for (domain, requirement_id), features in sorted(footprint.items()):
        if len(features) < 2:
            continue
        related = tuple(sorted(features))
        findings.append(
            _finding(
                PARALLEL_CHANGE_CONFLICT,
                domain,
                f"parallel changes {', '.join(related)} address the same requirement identity '{requirement_id}'",
                requirement_id=requirement_id,
                related_features=related,
            )
        )

    return ParallelConflictReport(
        feature_ids=selected,
        findings=tuple(findings),
    )
