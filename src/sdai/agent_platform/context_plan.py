from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Iterable, Mapping

from sdai.agent_platform.models import Capability, Skill
from sdai.agent_platform.skills import load_skill
from sdai.cross_artifact import CrossArtifactError, build_feature_artifact_index
from sdai.models import FeatureContext, validate_feature_id
from sdai.path_safety import ensure_within_project
from sdai.text import TextEncodingError, read_utf8_text


CONTEXT_PLAN_API_VERSION = "sdai.context-plan/v1"
CONTEXT_PLAN_MAX_FILES = 96
_CONTEXT_TRUNCATION_MARKER = "\n\n[truncated by SD-AI]"


class ContextPlanError(RuntimeError):
    """Raised when deterministic context planning or rendering is unsafe/stale."""


def _fail(code: str, message: str) -> ContextPlanError:
    return ContextPlanError(f"{code}: {message}")


def _sha256_bytes(value: bytes) -> str:
    return "sha256:" + sha256(value).hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _portable(root: Path, path: Path, *, label: str) -> str:
    safe = ensure_within_project(root, path, label=label)
    return safe.relative_to(root.resolve()).as_posix()


def _unique(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if value))


def _skill_identity(skill: Skill) -> str:
    """Bind instructions and applicability metadata, not just SKILL.md bytes."""
    payload = {
        "name": skill.name,
        "description": skill.description,
        "capabilities": [item.value for item in skill.capabilities],
        "instructions": skill.instructions,
    }
    return _sha256_bytes(_canonical_json(payload).encode("utf-8"))


@dataclass(frozen=True)
class PlannedContextFile:
    category: str
    source: str
    sha256: str
    size_bytes: int
    chars: int
    selected_chars: int
    truncated: bool
    reasons: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "category": self.category,
            "source": self.source,
            "sha256": self.sha256,
            "sizeBytes": self.size_bytes,
            "chars": self.chars,
            "selectedChars": self.selected_chars,
            "truncated": self.truncated,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class ContextExclusion:
    category: str
    source: str
    reason: str

    def as_dict(self) -> dict[str, str]:
        return {"category": self.category, "source": self.source, "reason": self.reason}


@dataclass(frozen=True)
class SkillContextDecision:
    name: str
    selected: bool
    reasons: tuple[str, ...]
    source: str
    sha256: str
    exclusion_reason: str | None = None

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "name": self.name,
            "selected": self.selected,
            "reasons": list(self.reasons),
            "source": self.source,
            "sha256": self.sha256,
        }
        if self.exclusion_reason is not None:
            payload["exclusionReason"] = self.exclusion_reason
        return payload


@dataclass(frozen=True)
class ContextPlan:
    feature_id: str
    capability: Capability
    workspace: str
    max_chars_per_file: int
    files: tuple[PlannedContextFile, ...]
    exclusions: tuple[ContextExclusion, ...]
    skills: tuple[SkillContextDecision, ...]
    diagnostics: tuple[str, ...] = ()

    @property
    def selected_skill_names(self) -> tuple[str, ...]:
        return tuple(item.name for item in self.skills if item.selected)

    def _body(self) -> dict[str, object]:
        return {
            "apiVersion": CONTEXT_PLAN_API_VERSION,
            "featureId": self.feature_id,
            "capability": self.capability.value,
            "workspace": self.workspace,
            "limits": {
                "maxCharsPerFile": self.max_chars_per_file,
                "maxFiles": CONTEXT_PLAN_MAX_FILES,
            },
            "files": [item.as_dict() for item in self.files],
            "exclusions": [item.as_dict() for item in self.exclusions],
            "skills": [item.as_dict() for item in self.skills],
            "diagnostics": list(self.diagnostics),
        }

    @property
    def sha256(self) -> str:
        return _sha256_bytes(_canonical_json(self._body()).encode("utf-8"))

    def as_dict(self) -> dict[str, object]:
        payload = self._body()
        payload["planSha256"] = self.sha256
        return payload

    def to_json(self) -> str:
        return _canonical_json(self.as_dict()) + "\n"

    def _read_verified(self, project_root: Path, item: PlannedContextFile) -> str:
        root = project_root.resolve()
        path = ensure_within_project(root, root / item.source, label="planned context source")
        if path.is_symlink() or not path.is_file():
            raise _fail(
                "SDAI-CONTEXT-PLAN-004",
                f"planned context source is missing or unsafe: {item.source}",
            )
        raw = path.read_bytes()
        if _sha256_bytes(raw) != item.sha256:
            raise _fail(
                "SDAI-CONTEXT-PLAN-005",
                f"planned context source changed after planning: {item.source}",
            )
        try:
            text = read_utf8_text(path)
        except (OSError, TextEncodingError) as exc:
            raise _fail(
                "SDAI-CONTEXT-PLAN-003",
                f"planned context source is not readable UTF-8: {item.source}",
            ) from exc
        if item.truncated:
            return text[: self.max_chars_per_file] + _CONTEXT_TRUNCATION_MARKER
        return text

    def render_feature_context(self, project_root: Path) -> str:
        workspace = Path(self.workspace)
        sections: list[str] = []
        for item in self.files:
            if item.category != "feature":
                continue
            try:
                label = Path(item.source).relative_to(workspace).as_posix()
            except ValueError:
                label = item.source
            sections.append(f"## Artifact: {label}\n{self._read_verified(project_root, item)}")
        return "\n\n".join(sections)

    def render_governance_context(self, project_root: Path) -> str:
        return "\n\n".join(
            f"## {item.source}\n{self._read_verified(project_root, item)}"
            for item in self.files
            if item.category == "governance"
        )

    def render_skills(self, project_root: Path) -> str:
        root = project_root.resolve()
        sections: list[str] = []
        for decision in self.skills:
            if not decision.selected:
                continue
            path = ensure_within_project(root, root / decision.source, label="planned skill source")
            if path.is_symlink() or not path.is_file():
                raise _fail(
                    "SDAI-CONTEXT-PLAN-004",
                    f"planned skill source is missing or unsafe: {decision.name}",
                )
            skill = load_skill(root, decision.name)
            if _skill_identity(skill) != decision.sha256:
                raise _fail(
                    "SDAI-CONTEXT-PLAN-005",
                    f"planned skill changed after planning: {decision.name}",
                )
            sections.append(f"## Skill: {skill.name}\n{skill.instructions}")
        return "\n\n".join(sections)


_FIXED_RULES: tuple[tuple[str, frozenset[Capability] | None], ...] = (
    ("00-intake.md", None),
    ("requirements.md", None),
    ("specification.md", None),
    (
        "architecture/architecture.md",
        frozenset(
            {
                Capability.ARCHITECTURE,
                Capability.PLANNING,
                Capability.CODING,
                Capability.REVIEW,
                Capability.TESTING,
                Capability.SECURITY,
                Capability.DOCUMENTATION,
            }
        ),
    ),
    (
        "architecture/decision-matrix.md",
        frozenset(
            {
                Capability.ARCHITECTURE,
                Capability.PLANNING,
                Capability.REVIEW,
                Capability.DOCUMENTATION,
            }
        ),
    ),
    (
        "adr/ADR-001-initial-architecture.md",
        frozenset(
            {
                Capability.ARCHITECTURE,
                Capability.PLANNING,
                Capability.CODING,
                Capability.REVIEW,
                Capability.TESTING,
                Capability.SECURITY,
                Capability.DOCUMENTATION,
            }
        ),
    ),
    (
        "plan.md",
        frozenset(
            {
                Capability.PLANNING,
                Capability.CODING,
                Capability.REVIEW,
                Capability.TESTING,
                Capability.DOCUMENTATION,
            }
        ),
    ),
    (
        "tasks.yaml",
        frozenset(
            {
                Capability.PLANNING,
                Capability.CODING,
                Capability.REVIEW,
                Capability.TESTING,
                Capability.DOCUMENTATION,
            }
        ),
    ),
    (
        "security-review.md",
        frozenset(
            {
                Capability.CODING,
                Capability.REVIEW,
                Capability.SECURITY,
                Capability.DOCUMENTATION,
            }
        ),
    ),
    (
        "implementation-brief.md",
        frozenset(
            {
                Capability.CODING,
                Capability.REVIEW,
                Capability.TESTING,
                Capability.DOCUMENTATION,
            }
        ),
    ),
)

_ROOT_RULES: Mapping[str, frozenset[Capability]] = {
    "rfc": frozenset(
        {
            Capability.ARCHITECTURE,
            Capability.PLANNING,
            Capability.CODING,
            Capability.REVIEW,
            Capability.SECURITY,
            Capability.DOCUMENTATION,
        }
    ),
    "architecture": frozenset(
        {
            Capability.ARCHITECTURE,
            Capability.PLANNING,
            Capability.CODING,
            Capability.REVIEW,
            Capability.TESTING,
            Capability.SECURITY,
            Capability.DOCUMENTATION,
        }
    ),
    "adr": frozenset(
        {
            Capability.ARCHITECTURE,
            Capability.PLANNING,
            Capability.CODING,
            Capability.REVIEW,
            Capability.TESTING,
            Capability.SECURITY,
            Capability.DOCUMENTATION,
        }
    ),
    "contracts": frozenset(
        {
            Capability.ARCHITECTURE,
            Capability.PLANNING,
            Capability.CODING,
            Capability.REVIEW,
            Capability.TESTING,
            Capability.SECURITY,
            Capability.DOCUMENTATION,
        }
    ),
    "security": frozenset(
        {
            Capability.ARCHITECTURE,
            Capability.CODING,
            Capability.REVIEW,
            Capability.TESTING,
            Capability.SECURITY,
            Capability.DOCUMENTATION,
        }
    ),
    "evidence": frozenset(
        {
            Capability.CODING,
            Capability.REVIEW,
            Capability.TESTING,
            Capability.SECURITY,
            Capability.DOCUMENTATION,
        }
    ),
    "quality": frozenset(
        {
            Capability.CODING,
            Capability.REVIEW,
            Capability.TESTING,
            Capability.SECURITY,
            Capability.DOCUMENTATION,
        }
    ),
    "quality-gates": frozenset(
        {
            Capability.CODING,
            Capability.REVIEW,
            Capability.TESTING,
            Capability.SECURITY,
            Capability.DOCUMENTATION,
        }
    ),
    "ai": frozenset(
        {
            Capability.CODING,
            Capability.REVIEW,
            Capability.TESTING,
            Capability.SECURITY,
            Capability.DOCUMENTATION,
        }
    ),
}
_CONTEXT_SUFFIXES = frozenset(
    {".md", ".markdown", ".mmd", ".puml", ".plantuml", ".yaml", ".yml", ".json", ".proto", ".txt"}
)
_TRACE_KINDS: Mapping[Capability, frozenset[str]] = {
    Capability.REQUIREMENTS: frozenset({"requirement", "scenario"}),
    Capability.ARCHITECTURE: frozenset(
        {"requirement", "scenario", "adr", "contract", "threat", "mitigation"}
    ),
    Capability.PLANNING: frozenset({"requirement", "scenario", "adr", "contract", "task"}),
    Capability.CODING: frozenset(
        {"requirement", "scenario", "adr", "contract", "task", "threat", "mitigation"}
    ),
    Capability.REVIEW: frozenset(
        {
            "requirement",
            "scenario",
            "adr",
            "contract",
            "task",
            "test",
            "threat",
            "mitigation",
            "approval",
        }
    ),
    Capability.TESTING: frozenset({"requirement", "scenario", "contract", "task", "test"}),
    Capability.SECURITY: frozenset({"requirement", "adr", "contract", "threat", "mitigation"}),
    Capability.DOCUMENTATION: frozenset(
        {
            "requirement",
            "scenario",
            "adr",
            "contract",
            "task",
            "test",
            "threat",
            "mitigation",
            "approval",
        }
    ),
}
_SOURCE_SUFFIXES = frozenset(
    {
        ".py",
        ".java",
        ".kt",
        ".kts",
        ".cs",
        ".fs",
        ".go",
        ".rs",
        ".js",
        ".jsx",
        ".ts",
        ".tsx",
        ".c",
        ".cc",
        ".cpp",
        ".h",
        ".hpp",
        ".sh",
        ".bash",
        ".ps1",
        ".rb",
        ".php",
        ".scala",
        ".swift",
    }
)
_SOURCE_CAPABILITIES = frozenset(
    {
        Capability.CODING,
        Capability.REVIEW,
        Capability.TESTING,
        Capability.SECURITY,
        Capability.DOCUMENTATION,
    }
)
_SOURCE_EXCLUDED_PARTS = frozenset(
    {
        ".git",
        ".sdai",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        "dist",
        "build",
        "target",
        ".idea",
        ".vscode",
        "specs",
    }
)
_GOVERNANCE_FILES = (
    ".sdai/constitution.yaml",
    ".sdai/policies.yaml",
    ".sdai/governance.yaml",
    ".sdai/approval-policies.yaml",
    ".sdai/quality-gates.yaml",
    ".sdai/integrations.yaml",
    ".sdai/policy.yaml",
)


def _skill_decisions(
    project_root: Path,
    capability: Capability,
    *,
    profile_skills: Iterable[str],
    agent_skills: Iterable[str],
    policy_skills: Iterable[str],
) -> tuple[SkillContextDecision, ...]:
    root = project_root.resolve()
    reasons_by_name: dict[str, list[str]] = {}
    order: list[str] = []
    for reason, names in (
        ("profile", profile_skills),
        ("semantic-agent", agent_skills),
        ("policy-required", policy_skills),
    ):
        for name in names:
            if name not in reasons_by_name:
                reasons_by_name[name] = []
                order.append(name)
            if reason not in reasons_by_name[name]:
                reasons_by_name[name].append(reason)

    decisions: list[SkillContextDecision] = []
    for name in order:
        skill = load_skill(root, name)
        instructions_path = ensure_within_project(
            root, skill.root / "SKILL.md", label="skill instructions"
        )
        if instructions_path.is_symlink() or not instructions_path.is_file():
            raise _fail("SDAI-CONTEXT-PLAN-006", f"skill source is missing or unsafe: {name}")
        selected = not skill.capabilities or capability in skill.capabilities
        decisions.append(
            SkillContextDecision(
                name=name,
                selected=selected,
                reasons=tuple(reasons_by_name[name]),
                source=_portable(root, instructions_path, label="skill instructions"),
                sha256=_skill_identity(skill),
                exclusion_reason=None if selected else "capability-not-applicable",
            )
        )
    return tuple(decisions)


def selected_skill_names(
    project_root: Path,
    capability: Capability,
    *,
    profile_skills: Iterable[str] = (),
    agent_skills: Iterable[str] = (),
    policy_skills: Iterable[str] = (),
) -> tuple[str, ...]:
    return tuple(
        item.name
        for item in _skill_decisions(
            project_root,
            capability,
            profile_skills=profile_skills,
            agent_skills=agent_skills,
            policy_skills=policy_skills,
        )
        if item.selected
    )


def _record_file(
    root: Path,
    path: Path,
    *,
    category: str,
    reasons: tuple[str, ...],
    max_chars_per_file: int,
) -> PlannedContextFile:
    safe = ensure_within_project(root, path, label="context plan source")
    if safe.is_symlink() or not safe.is_file():
        raise _fail(
            "SDAI-CONTEXT-PLAN-002",
            f"context source is missing or unsafe: {_portable(root, safe, label='context source')}",
        )
    raw = safe.read_bytes()
    try:
        text = read_utf8_text(safe)
    except (OSError, TextEncodingError) as exc:
        raise _fail(
            "SDAI-CONTEXT-PLAN-003",
            f"context source is not readable UTF-8: {_portable(root, safe, label='context source')}",
        ) from exc
    truncated = len(text) > max_chars_per_file
    return PlannedContextFile(
        category=category,
        source=_portable(root, safe, label="context source"),
        sha256=_sha256_bytes(raw),
        size_bytes=len(raw),
        chars=len(text),
        selected_chars=min(len(text), max_chars_per_file),
        truncated=truncated,
        reasons=reasons,
    )


def build_context_plan(
    project_root: Path,
    feature_id: str,
    capability: Capability,
    *,
    max_chars_per_file: int,
    profile_skills: Iterable[str] = (),
    agent_skills: Iterable[str] = (),
    policy_skills: Iterable[str] = (),
) -> ContextPlan:
    root = project_root.resolve()
    feature = validate_feature_id(feature_id)
    if not isinstance(capability, Capability):
        capability = Capability(str(capability))
    if max_chars_per_file < 1 or max_chars_per_file > 1_000_000:
        raise _fail(
            "SDAI-CONTEXT-PLAN-001",
            "max_chars_per_file must be between 1 and 1000000",
        )

    context = FeatureContext(root, feature)
    workspace_path = ensure_within_project(
        root, context.feature_dir, label="feature context workspace"
    )
    workspace = _portable(root, workspace_path, label="feature context workspace")
    selected: dict[str, tuple[Path, str, list[str]]] = {}
    exclusions: dict[tuple[str, str], ContextExclusion] = {}
    diagnostics: list[str] = []

    def include(path: Path, category: str, reason: str) -> None:
        safe = ensure_within_project(root, path, label="context candidate")
        if not safe.exists():
            return
        source = _portable(root, safe, label="context candidate")
        if safe.is_symlink() or not safe.is_file():
            exclusions[(category, source)] = ContextExclusion(
                category, source, "unsafe-or-non-file"
            )
            return
        existing = selected.get(source)
        if existing is None:
            selected[source] = (safe, category, [reason])
        elif reason not in existing[2]:
            existing[2].append(reason)
        exclusions.pop((category, source), None)

    def exclude(path: Path, category: str, reason: str) -> None:
        safe = ensure_within_project(root, path, label="context candidate")
        if not safe.exists() or not safe.is_file() or safe.is_symlink():
            return
        source = _portable(root, safe, label="context candidate")
        if source not in selected:
            exclusions[(category, source)] = ContextExclusion(category, source, reason)

    for relative, capabilities in _FIXED_RULES:
        path = workspace_path / relative
        if capabilities is None or capability in capabilities:
            include(path, "feature", "lifecycle-authority")
        else:
            exclude(path, "feature", "capability-not-relevant")

    for relative_root, capabilities in _ROOT_RULES.items():
        directory = ensure_within_project(
            root, workspace_path / relative_root, label="context root"
        )
        if not directory.exists() or not directory.is_dir() or directory.is_symlink():
            continue
        paths = tuple(
            sorted(
                (path for path in directory.rglob("*") if path.is_file()),
                key=lambda item: (
                    _portable(root, item, label="context root file").casefold(),
                    _portable(root, item, label="context root file"),
                ),
            )
        )
        for path in paths:
            if path.suffix.casefold() not in _CONTEXT_SUFFIXES:
                continue
            if capability in capabilities:
                include(path, "feature", f"capability-root:{relative_root}")
            else:
                exclude(path, "feature", "capability-not-relevant")

    relevant_ids: set[str] = set()
    modern = ensure_within_project(
        root,
        root / "specs" / "changes" / feature,
        label="current feature workspace",
    )
    if workspace_path == modern and modern.is_dir():
        try:
            index = build_feature_artifact_index(root, feature)
        except (CrossArtifactError, RuntimeError, ValueError):
            # Do not let an optional explanatory index become a second execution
            # authority. Deterministic lifecycle/root rules remain usable.
            diagnostics.append("trace-index-unavailable")
        else:
            relevant_kinds = _TRACE_KINDS[capability]
            for entity in index.entities:
                source_path = ensure_within_project(
                    root, root / entity.source, label="trace context source"
                )
                if entity.kind in relevant_kinds:
                    relevant_ids.add(entity.id)
                    include(source_path, "feature", f"trace-kind:{entity.kind}")
                else:
                    exclude(source_path, "feature", "trace-kind-not-relevant")
    else:
        diagnostics.append("legacy-workspace-trace-fallback")

    if relevant_ids and capability in _SOURCE_CAPABILITIES:
        escaped = "|".join(
            re.escape(item)
            for item in sorted(relevant_ids, key=lambda value: (-len(value), value))
        )
        reference = re.compile(
            rf"(?<![A-Za-z0-9])(?:{escaped})(?![A-Za-z0-9])",
            re.IGNORECASE,
        )
        source_matches: list[tuple[Path, str]] = []
        for path in root.rglob("*"):
            if (
                not path.is_file()
                or path.is_symlink()
                or path.suffix.casefold() not in _SOURCE_SUFFIXES
            ):
                continue
            relative = path.relative_to(root)
            if any(part in _SOURCE_EXCLUDED_PARTS for part in relative.parts):
                continue
            try:
                text = read_utf8_text(path)
            except (OSError, TextEncodingError):
                continue
            match = reference.search(text)
            if match is not None:
                source_matches.append((path, match.group(0).upper()))
        for path, referenced_id in sorted(
            source_matches,
            key=lambda item: (
                _portable(root, item[0], label="trace source reference").casefold(),
                _portable(root, item[0], label="trace source reference"),
            ),
        ):
            include(path, "feature", f"trace-source-reference:{referenced_id}")

    # Governance is execution authority, not optional relevance context. Add it to
    # the same bounded plan and order it before feature files so file pressure can
    # never silently remove constitution/policy material.
    for relative in _GOVERNANCE_FILES:
        include(root / relative, "governance", "governance-authority")

    ordered_entries = sorted(
        selected.values(),
        key=lambda item: (
            0 if item[1] == "governance" else 1,
            _portable(root, item[0], label="context selected source").casefold(),
            _portable(root, item[0], label="context selected source"),
        ),
    )
    files: list[PlannedContextFile] = []
    for index, (path, category, reasons) in enumerate(ordered_entries):
        if index >= CONTEXT_PLAN_MAX_FILES:
            source = _portable(root, path, label="context budget source")
            exclusions[(category, source)] = ContextExclusion(
                category, source, "file-budget-exceeded"
            )
            continue
        files.append(
            _record_file(
                root,
                path,
                category=category,
                reasons=_unique(reasons),
                max_chars_per_file=max_chars_per_file,
            )
        )

    skills = _skill_decisions(
        root,
        capability,
        profile_skills=profile_skills,
        agent_skills=agent_skills,
        policy_skills=policy_skills,
    )
    return ContextPlan(
        feature_id=feature,
        capability=capability,
        workspace=workspace,
        max_chars_per_file=max_chars_per_file,
        files=tuple(files),
        exclusions=tuple(
            sorted(
                exclusions.values(),
                key=lambda item: (
                    item.category,
                    item.source.casefold(),
                    item.source,
                    item.reason,
                ),
            )
        ),
        skills=skills,
        diagnostics=_unique(diagnostics),
    )


__all__ = [
    "CONTEXT_PLAN_API_VERSION",
    "CONTEXT_PLAN_MAX_FILES",
    "ContextExclusion",
    "ContextPlan",
    "ContextPlanError",
    "PlannedContextFile",
    "SkillContextDecision",
    "build_context_plan",
    "selected_skill_names",
]
