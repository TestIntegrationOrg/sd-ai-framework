from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import re
from typing import Any, Mapping

import yaml

from sdai.artifacts import write_text
from sdai.models import FeatureContext
from sdai.path_safety import ensure_within_project
from sdai.text import read_utf8_text


class ConstitutionError(RuntimeError):
    pass


@dataclass(frozen=True)
class ConstitutionPrinciple:
    id: str
    title: str
    severity: str
    required_sections: tuple[str, ...]
    required_terms: tuple[str, ...]


@dataclass(frozen=True)
class Constitution:
    version: int
    principles: tuple[ConstitutionPrinciple, ...]
    body: str
    path: Path
    sha256: str


@dataclass(frozen=True)
class ConstitutionFinding:
    principle_id: str
    title: str
    severity: str
    status: str
    evidence: tuple[str, ...]
    missing: tuple[str, ...]


_PRINCIPLE_ID = re.compile(r"^CON-[0-9]{3}$")
_ALLOWED_SEVERITY = frozenset({"blocking", "warning"})
_FRONTMATTER_END = "\n---\n"


DEFAULT_CONSTITUTION = """---
version: 1
principles:
  - id: CON-001
    title: Requirements are explicit and testable
    severity: blocking
    required_sections:
      - Functional Requirements
      - Acceptance Criteria
    required_terms: []
  - id: CON-002
    title: Security and privacy are considered explicitly
    severity: blocking
    required_sections:
      - Non-Functional Requirements
    required_terms:
      - security
  - id: CON-003
    title: Failure behavior and observability are specified
    severity: blocking
    required_sections:
      - Non-Functional Requirements
    required_terms:
      - failure
      - observability
  - id: CON-004
    title: Compatibility and rollout assumptions are explicit
    severity: warning
    required_sections: []
    required_terms:
      - compatibility
  - id: CON-005
    title: Material architecture decisions require traceable review
    severity: blocking
    required_sections: []
    required_terms: []
---

# SDAI Engineering Constitution

These principles govern engineering quality independently from provider selection or execution policy.

## CON-001 — Requirements are explicit and testable
Feature behavior must be expressed as identifiable requirements with acceptance criteria that can be verified.

## CON-002 — Security and privacy are considered explicitly
Security, trust boundaries, sensitive data, authentication/authorization, and privacy implications must be considered when relevant.

## CON-003 — Failure behavior and observability are specified
Failure modes, retries/timeouts where applicable, logs, metrics, traces, and operational detection must be addressed before implementation is considered complete.

## CON-004 — Compatibility and rollout assumptions are explicit
Backward compatibility, migration, deployment, rollback, and consumer impact must be stated for changes that can affect existing users or integrations.

## CON-005 — Material architecture decisions require traceable review
Material architecture decisions must be documented and reviewed rather than silently encoded by an implementation agent.
"""


def constitution_path(project_root: Path) -> Path:
    root = project_root.resolve()
    return ensure_within_project(
        root,
        root / ".sdai" / "constitution.md",
        label="engineering constitution path",
    )


def init_constitution(project_root: Path, *, force: bool = False) -> Path:
    path = constitution_path(project_root)
    return write_text(path, DEFAULT_CONSTITUTION, overwrite=force)


def _frontmatter(text: str, path: Path) -> tuple[Mapping[str, Any], str]:
    if not text.startswith("---\n"):
        raise ConstitutionError(
            f"Engineering constitution '{path}' must start with YAML frontmatter"
        )
    end = text.find(_FRONTMATTER_END, 4)
    if end < 0:
        raise ConstitutionError(
            f"Engineering constitution '{path}' has unterminated YAML frontmatter"
        )
    try:
        raw = yaml.safe_load(text[4:end]) or {}
    except yaml.YAMLError as exc:
        raise ConstitutionError(f"Invalid constitution YAML: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise ConstitutionError("Constitution frontmatter must be a mapping")
    body = text[end + len(_FRONTMATTER_END) :].strip()
    if not body:
        raise ConstitutionError("Constitution must contain explanatory Markdown")
    return raw, body


def _string_list(value: object, label: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise ConstitutionError(f"{label} must be a list of non-empty strings")
    return tuple(item.strip() for item in value)


def load_constitution(project_root: Path) -> Constitution:
    path = constitution_path(project_root)
    if not path.exists():
        raise ConstitutionError(
            "Engineering constitution is missing. Run `sdai constitution init`."
        )
    text = read_utf8_text(path)
    raw, body = _frontmatter(text, path)

    version = raw.get("version")
    if version != 1:
        raise ConstitutionError("Constitution version must be 1")
    raw_principles = raw.get("principles")
    if not isinstance(raw_principles, list) or not raw_principles:
        raise ConstitutionError("Constitution principles must be a non-empty list")

    principles: list[ConstitutionPrinciple] = []
    seen: set[str] = set()
    for index, raw_principle in enumerate(raw_principles, start=1):
        if not isinstance(raw_principle, Mapping):
            raise ConstitutionError(f"Constitution principle #{index} must be a mapping")
        principle_id = str(raw_principle.get("id") or "").strip()
        if not _PRINCIPLE_ID.fullmatch(principle_id):
            raise ConstitutionError(
                f"Constitution principle #{index} id must use CON-NNN format"
            )
        if principle_id in seen:
            raise ConstitutionError(f"Duplicate constitution principle id '{principle_id}'")
        seen.add(principle_id)
        title = str(raw_principle.get("title") or "").strip()
        if not title:
            raise ConstitutionError(f"Constitution principle '{principle_id}' needs a title")
        severity = str(raw_principle.get("severity") or "blocking").strip().lower()
        if severity not in _ALLOWED_SEVERITY:
            raise ConstitutionError(
                f"Constitution principle '{principle_id}' severity must be blocking or warning"
            )
        principles.append(
            ConstitutionPrinciple(
                id=principle_id,
                title=title,
                severity=severity,
                required_sections=_string_list(
                    raw_principle.get("required_sections"),
                    f"{principle_id}.required_sections",
                ),
                required_terms=tuple(
                    term.casefold()
                    for term in _string_list(
                        raw_principle.get("required_terms"),
                        f"{principle_id}.required_terms",
                    )
                ),
            )
        )

    digest = sha256(text.encode("utf-8")).hexdigest()
    return Constitution(
        version=1,
        principles=tuple(principles),
        body=body,
        path=path,
        sha256=digest,
    )


def _sections(markdown: str) -> dict[str, str]:
    headings = list(re.finditer(r"^##\s+(.+?)\s*$", markdown, flags=re.MULTILINE))
    sections: dict[str, str] = {}
    for index, match in enumerate(headings):
        start = match.end()
        end = headings[index + 1].start() if index + 1 < len(headings) else len(markdown)
        sections[match.group(1).strip()] = markdown[start:end].strip()
    return sections


def check_constitution(project_root: Path, feature_id: str) -> tuple[ConstitutionFinding, ...]:
    constitution = load_constitution(project_root)
    context = FeatureContext(project_root, feature_id)
    spec_path = context.artifact("specification.md")
    if not spec_path.exists():
        raise ConstitutionError(
            f"Feature '{feature_id}' has no specification.md; create requirements first"
        )
    specification = read_utf8_text(spec_path)
    sections = _sections(specification)
    specification_folded = specification.casefold()

    findings: list[ConstitutionFinding] = []
    for principle in constitution.principles:
        has_deterministic_checks = bool(
            principle.required_sections or principle.required_terms
        )
        missing_sections = [
            section
            for section in principle.required_sections
            if section not in sections or not sections[section].strip()
        ]
        missing_terms = [
            term for term in principle.required_terms if term not in specification_folded
        ]
        missing = tuple(
            [f"section:{section}" for section in missing_sections]
            + [f"term:{term}" for term in missing_terms]
        )
        evidence = tuple(
            [
                f"section:{section}"
                for section in principle.required_sections
                if section not in missing_sections
            ]
            + [
                f"term:{term}"
                for term in principle.required_terms
                if term not in missing_terms
            ]
        )
        if not has_deterministic_checks:
            status = "review"
        else:
            status = "pass" if not missing else "fail"
        findings.append(
            ConstitutionFinding(
                principle_id=principle.id,
                title=principle.title,
                severity=principle.severity,
                status=status,
                evidence=evidence,
                missing=missing,
            )
        )
    return tuple(findings)


def write_constitution_evidence(project_root: Path, feature_id: str) -> Path:
    constitution = load_constitution(project_root)
    findings = check_constitution(project_root, feature_id)
    context = FeatureContext(project_root, feature_id)
    path = context.artifact("quality/constitution-check.yaml")
    payload = {
        "version": 1,
        "feature": context.feature_id,
        "constitution_sha256": constitution.sha256,
        "constitution_path": ".sdai/constitution.md",
        "review_owner": "requirements-analyst",
        "approval_status": "pending",
        "implementation_self_approval": "forbidden",
        "findings": [
            {
                "principle_id": finding.principle_id,
                "title": finding.title,
                "severity": finding.severity,
                "status": finding.status,
                "evidence": list(finding.evidence),
                "missing": list(finding.missing),
            }
            for finding in findings
        ],
    }
    return write_text(path, yaml.safe_dump(payload, sort_keys=False), overwrite=True)
