from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Iterable, Mapping

from sdai.agent_platform.definitions import AgentDefinition, load_agent_definition
from sdai.agent_platform.models import Capability, Skill
from sdai.agent_platform.skills import list_skills, load_skill
from sdai.config import load_yaml
from sdai.path_safety import ensure_within_project
from sdai.policy import load_effective_configuration
from sdai.technology import CATEGORIES, TechnologyFact, TechnologyReport, detect_technologies


class SkillResolutionError(RuntimeError):
    pass


@dataclass(frozen=True)
class SkillSelectionRules:
    auto: bool = False
    roles: tuple[str, ...] = ()
    capabilities: tuple[Capability, ...] = ()
    task_keywords: tuple[str, ...] = ()
    domains: tuple[str, ...] = ()


@dataclass(frozen=True)
class SkillMetadata:
    name: str
    capabilities: tuple[Capability, ...]
    compatible_agents: tuple[str, ...]
    requires: tuple[str, ...]
    compatibility: dict[str, dict[str, str | None]]
    selection: SkillSelectionRules
    source: str


@dataclass(frozen=True)
class SkillDecision:
    name: str
    selected: bool
    origins: tuple[str, ...]
    reasons: tuple[str, ...]
    dependencies: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "selected": self.selected,
            "origins": list(self.origins),
            "reasons": list(self.reasons),
            "dependencies": list(self.dependencies),
        }


@dataclass(frozen=True)
class SkillResolutionReport:
    agent: str
    capability: str
    task: str | None
    domain: str | None
    selected: tuple[str, ...]
    decisions: tuple[SkillDecision, ...]
    policy_required: tuple[str, ...]
    agent_declared: tuple[str, ...]
    explicitly_requested: tuple[str, ...]
    technology: TechnologyReport

    def as_dict(self) -> dict[str, object]:
        return {
            "version": 1,
            "agent": self.agent,
            "capability": self.capability,
            "task": self.task,
            "domain": self.domain,
            "selected": list(self.selected),
            "policy_required": list(self.policy_required),
            "agent_declared": list(self.agent_declared),
            "explicitly_requested": list(self.explicitly_requested),
            "decisions": [item.as_dict() for item in self.decisions],
            "technology": self.technology.as_dict(),
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), indent=2, sort_keys=True, ensure_ascii=False)


_ALLOWED_METADATA_FIELDS = {
    "version",
    "capabilities",
    "compatible_agents",
    "requires",
    "compatibility",
    "selection",
}
_ALLOWED_SELECTION_FIELDS = {
    "auto",
    "roles",
    "capabilities",
    "task_keywords",
    "domains",
}
_VERSION = re.compile(r"^[0-9]+(?:\.[0-9]+)*$")
_CONSTRAINT = re.compile(r"^(>=|<=|>|<|==|=)?\s*([0-9]+(?:\.[0-9]+)*)$")
_EXACT_DETECTED_VERSION_SOURCES = {
    "maven-java-version",
    "maven-parent",
    "maven-dependency",
    "gradle-java-version",
    "gradle-plugin",
    "csproj-langversion",
    "csproj-target-framework",
    "csproj-sdk",
    "nuget",
    "go.mod",
    "Cargo.toml",
    "ps1",
    "package-manager",
}


def _fail(code: str, message: str) -> SkillResolutionError:
    return SkillResolutionError(f"{code}: {message}")


def _portable(root: Path, path: Path) -> str:
    safe = ensure_within_project(root, path, label="skill metadata path")
    return safe.relative_to(root.resolve()).as_posix()


def _strings(value: object, *, label: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise _fail("SDAI-SKILL-001", f"{label} must be a string list")
    return tuple(dict.fromkeys(item.strip() for item in value))


def _capabilities(value: object, *, label: str) -> tuple[Capability, ...]:
    result: list[Capability] = []
    for raw in _strings(value, label=label):
        try:
            capability = Capability(raw)
        except ValueError as exc:
            raise _fail(
                "SDAI-SKILL-001",
                f"{label} contains unsupported capability '{raw}'",
            ) from exc
        if capability not in result:
            result.append(capability)
    return tuple(result)


def _version_tuple(value: str) -> tuple[int, ...]:
    if not _VERSION.fullmatch(value):
        raise _fail(
            "SDAI-SKILL-003",
            f"version '{value}' is not an exact numeric version supported by resolver v1",
        )
    return tuple(int(part) for part in value.split("."))


def _constraint(value: str, *, label: str) -> tuple[tuple[str, tuple[int, ...]], ...]:
    clauses: list[tuple[str, tuple[int, ...]]] = []
    for raw in value.split(","):
        match = _CONSTRAINT.fullmatch(raw.strip())
        if match is None:
            raise _fail(
                "SDAI-SKILL-001",
                f"{label} uses unsupported constraint '{raw.strip()}'; resolver v1 supports numeric versions with >=, >, <=, <, ==, = and comma-separated clauses",
            )
        clauses.append((match.group(1) or "==", _version_tuple(match.group(2))))
    return tuple(clauses)


def _compatibility(value: object, *, label: str) -> dict[str, dict[str, str | None]]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise _fail("SDAI-SKILL-001", f"{label} must be a mapping")
    unknown = sorted(set(value) - set(CATEGORIES))
    if unknown:
        raise _fail(
            "SDAI-SKILL-001",
            f"{label} contains unsupported category(s): {', '.join(map(str, unknown))}",
        )
    result: dict[str, dict[str, str | None]] = {}
    for category, raw in value.items():
        if not isinstance(raw, dict):
            raise _fail(
                "SDAI-SKILL-001",
                f"{label}.{category} must map technology id to constraint/null",
            )
        rules: dict[str, str | None] = {}
        for technology, constraint in raw.items():
            name = str(technology).strip()
            if not name:
                raise _fail("SDAI-SKILL-001", f"{label}.{category} contains an empty technology id")
            if constraint is None:
                rules[name] = None
                continue
            if not isinstance(constraint, (str, int, float)):
                raise _fail(
                    "SDAI-SKILL-001",
                    f"{label}.{category}.{name} must be a numeric constraint or null",
                )
            text = str(constraint).strip()
            _constraint(text, label=f"{label}.{category}.{name}")
            rules[name] = text
        result[str(category)] = rules
    return result


def _selection(value: object, *, label: str) -> SkillSelectionRules:
    if value is None:
        return SkillSelectionRules()
    if not isinstance(value, dict):
        raise _fail("SDAI-SKILL-001", f"{label} must be a mapping")
    unknown = sorted(set(value) - _ALLOWED_SELECTION_FIELDS)
    if unknown:
        raise _fail(
            "SDAI-SKILL-001",
            f"{label} contains unsupported key(s): {', '.join(map(str, unknown))}",
        )
    auto = value.get("auto", False)
    if not isinstance(auto, bool):
        raise _fail("SDAI-SKILL-001", f"{label}.auto must be true or false")
    return SkillSelectionRules(
        auto=auto,
        roles=_strings(value.get("roles"), label=f"{label}.roles"),
        capabilities=_capabilities(
            value.get("capabilities"), label=f"{label}.capabilities"
        ),
        task_keywords=_strings(
            value.get("task_keywords"), label=f"{label}.task_keywords"
        ),
        domains=_strings(value.get("domains"), label=f"{label}.domains"),
    )


def load_skill_metadata(project_root: Path, skill: Skill | str) -> SkillMetadata:
    root = project_root.resolve()
    loaded = load_skill(root, skill) if isinstance(skill, str) else skill
    canonical = ensure_within_project(root, loaded.root / "sdai.yaml", label="skill sidecar")
    legacy = ensure_within_project(root, loaded.root / "skill.yaml", label="legacy skill manifest")
    path = canonical if canonical.is_file() else legacy if legacy.is_file() else None
    raw: dict[str, object] = {}
    source = _portable(root, loaded.root / "SKILL.md")
    if path is not None:
        parsed = load_yaml(path)
        if not isinstance(parsed, dict):
            raise _fail("SDAI-SKILL-001", f"{_portable(root, path)} must be a mapping")
        raw = dict(parsed)
        source = _portable(root, path)

    allowed = set(_ALLOWED_METADATA_FIELDS)
    if path is legacy:
        allowed.update({"name", "description"})
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise _fail(
            "SDAI-SKILL-001",
            f"{source} contains unsupported skill metadata key(s): {', '.join(map(str, unknown))}",
        )
    if "version" in raw and raw["version"] != 1:
        raise _fail("SDAI-SKILL-001", f"{source} version must be 1")

    capabilities = _capabilities(raw.get("capabilities"), label=f"{source}: capabilities")
    if not capabilities:
        capabilities = loaded.capabilities
    return SkillMetadata(
        name=loaded.name,
        capabilities=capabilities,
        compatible_agents=_strings(
            raw.get("compatible_agents"), label=f"{source}: compatible_agents"
        ),
        requires=_strings(raw.get("requires"), label=f"{source}: requires"),
        compatibility=_compatibility(
            raw.get("compatibility"), label=f"{source}: compatibility"
        ),
        selection=_selection(raw.get("selection"), label=f"{source}: selection"),
        source=source,
    )


def _compare(version: str, constraint: str) -> bool:
    actual = _version_tuple(version)
    for operator, required in _constraint(constraint, label="skill compatibility"):
        width = max(len(actual), len(required))
        left = actual + (0,) * (width - len(actual))
        right = required + (0,) * (width - len(required))
        if operator in {"=", "=="} and left != right:
            return False
        if operator == ">=" and left < right:
            return False
        if operator == ">" and left <= right:
            return False
        if operator == "<=" and left > right:
            return False
        if operator == "<" and left >= right:
            return False
    return True


def _exact_version(fact: TechnologyFact) -> str | None:
    if fact.version is None or not _VERSION.fullmatch(fact.version):
        return None
    if fact.version_source == "declared":
        return fact.version
    if fact.version_source != "detected":
        return None
    evidence = [
        item
        for item in fact.evidence
        if item.version == fact.version and not item.detector.startswith("declared")
    ]
    if not evidence or any(
        item.detector not in _EXACT_DETECTED_VERSION_SOURCES for item in evidence
    ):
        return None
    return fact.version


def _compatibility_status(
    metadata: SkillMetadata,
    technology: TechnologyReport,
) -> tuple[bool, tuple[str, ...]]:
    facts = {(item.category, item.name): item for item in technology.technologies}
    reasons: list[str] = []
    for category in sorted(metadata.compatibility):
        for name, constraint in sorted(metadata.compatibility[category].items()):
            fact = facts.get((category, name))
            if fact is None:
                return False, (f"requires missing technology {category}.{name}",)
            if constraint is None:
                reasons.append(f"matched {category}.{name} presence")
                continue
            exact = _exact_version(fact)
            if exact is None:
                return False, (
                    f"cannot prove version compatibility for {category}.{name}; selected={fact.version or '-'} source={fact.version_source}",
                )
            if not _compare(exact, constraint):
                return False, (
                    f"incompatible {category}.{name} version {exact}; requires {constraint}",
                )
            reasons.append(f"matched {category}.{name} {exact} against {constraint}")
    return True, tuple(reasons)


def _role_status(
    metadata: SkillMetadata,
    agent: AgentDefinition,
    capability: Capability,
    *,
    auto: bool,
) -> tuple[bool, tuple[str, ...]]:
    if metadata.capabilities and capability not in metadata.capabilities:
        return False, (f"skill does not support capability {capability.value}",)
    if metadata.compatible_agents and agent.name not in metadata.compatible_agents:
        return False, (f"skill is not compatible with semantic agent {agent.name}",)
    if auto and metadata.selection.roles and agent.name not in metadata.selection.roles:
        return False, (f"auto-selection role filter excludes {agent.name}",)
    if auto and metadata.selection.capabilities and capability not in metadata.selection.capabilities:
        return False, (f"auto-selection capability filter excludes {capability.value}",)
    return True, ()


def _task_keyword_matches(task: str, keyword: str) -> bool:
    """Match a case-insensitive task token/phrase without arbitrary substrings.

    Unicode casefolding keeps matching deterministic across platforms. Word
    characters are treated as part of a token; punctuation/whitespace may bound a
    keyword or phrase. Thus ``bug`` matches ``fix a bug`` and ``bug-fix`` but does
    not match ``debug`` or ``buggy``.
    """

    folded_task = task.casefold()
    folded_keyword = keyword.casefold()
    pattern = rf"(?<!\w){re.escape(folded_keyword)}(?!\w)"
    return re.search(pattern, folded_task) is not None


def _context_status(
    metadata: SkillMetadata,
    *,
    task: str | None,
    domain: str | None,
) -> tuple[bool, tuple[str, ...]]:
    if not metadata.selection.auto:
        return False, ("auto-selection disabled",)
    reasons: list[str] = ["auto-selection enabled"]
    if metadata.selection.domains:
        if not domain or domain not in metadata.selection.domains:
            return False, (f"domain filter excludes {domain or '<unspecified>'}",)
        reasons.append(f"domain {domain} matched")
    if metadata.selection.task_keywords:
        task_text = task or ""
        keyword = next(
            (
                item
                for item in metadata.selection.task_keywords
                if _task_keyword_matches(task_text, item)
            ),
            None,
        )
        if keyword is None:
            return False, ("task keyword filter did not match",)
        reasons.append(f"task keyword '{keyword}' matched")
    return True, tuple(reasons)


def _metadata_index(project_root: Path) -> dict[str, SkillMetadata]:
    return {
        skill.name: load_skill_metadata(project_root, skill)
        for skill in list_skills(project_root)
    }


def _validate_graph(metadata: Mapping[str, SkillMetadata]) -> None:
    visiting: list[str] = []
    visited: set[str] = set()

    def visit(name: str) -> None:
        if name in visited:
            return
        if name in visiting:
            cycle = visiting[visiting.index(name) :] + [name]
            raise _fail("SDAI-SKILL-005", "skill dependency cycle: " + " -> ".join(cycle))
        item = metadata.get(name)
        if item is None:
            raise _fail("SDAI-SKILL-006", f"missing required skill '{name}'")
        visiting.append(name)
        for dependency in item.requires:
            if dependency not in metadata:
                raise _fail(
                    "SDAI-SKILL-006",
                    f"skill '{name}' requires missing skill '{dependency}'",
                )
            visit(dependency)
        visiting.pop()
        visited.add(name)

    for name in sorted(metadata):
        visit(name)


def _closure(
    name: str,
    metadata: Mapping[str, SkillMetadata],
    agent: AgentDefinition,
    capability: Capability,
    technology: TechnologyReport,
) -> tuple[str, ...]:
    item = metadata.get(name)
    if item is None:
        raise _fail("SDAI-SKILL-006", f"required skill '{name}' is not installed")
    role_ok, role_reasons = _role_status(item, agent, capability, auto=False)
    if not role_ok:
        raise _fail("SDAI-SKILL-002", f"skill '{name}' is incompatible: {role_reasons[0]}")
    compatible, reasons = _compatibility_status(item, technology)
    if not compatible:
        raise _fail("SDAI-SKILL-003", f"skill '{name}' is incompatible: {reasons[-1]}")
    result: list[str] = []
    for dependency in item.requires:
        for resolved in _closure(dependency, metadata, agent, capability, technology):
            if resolved not in result:
                result.append(resolved)
    if name not in result:
        result.append(name)
    return tuple(result)


def resolve_skills(
    project_root: Path,
    *,
    agent_name: str,
    capability: Capability | str,
    task: str | None = None,
    domain: str | None = None,
    requested: Iterable[str] = (),
    environ: Mapping[str, str] | None = None,
) -> SkillResolutionReport:
    root = project_root.resolve()
    try:
        capability_value = capability if isinstance(capability, Capability) else Capability(capability)
    except ValueError as exc:
        raise _fail("SDAI-SKILL-002", f"unsupported capability '{capability}'") from exc
    agent = load_agent_definition(root, agent_name)
    if not agent.supports(capability_value):
        raise _fail(
            "SDAI-SKILL-002",
            f"semantic agent '{agent.name}' does not support capability '{capability_value.value}'",
        )

    technology = detect_technologies(root)
    policy = load_effective_configuration(root, environ=environ)
    policy_required = policy.required_skills(capability_value)
    explicit = tuple(dict.fromkeys(str(item).strip() for item in requested if str(item).strip()))
    metadata = _metadata_index(root)
    _validate_graph(metadata)

    origins: dict[str, list[str]] = {}
    selected: list[str] = []
    reasons: dict[str, list[str]] = {}

    def add_origin(name: str, origin: str) -> None:
        bucket = origins.setdefault(name, [])
        if origin not in bucket:
            bucket.append(origin)

    for origin, seeds in (
        ("agent", agent.skills),
        ("policy", policy_required),
        ("requested", explicit),
    ):
        for seed in seeds:
            add_origin(seed, origin)
            for name in _closure(seed, metadata, agent, capability_value, technology):
                if name != seed:
                    add_origin(name, f"dependency:{seed}")
                if name not in selected:
                    selected.append(name)
                reasons.setdefault(name, []).append("selected by explicit resolver seed")

    rejected: dict[str, tuple[str, ...]] = {}
    for name in sorted(metadata):
        if name in selected:
            continue
        item = metadata[name]
        context_ok, context_reasons = _context_status(item, task=task, domain=domain)
        if not context_ok:
            rejected[name] = context_reasons
            continue
        role_ok, role_reasons = _role_status(item, agent, capability_value, auto=True)
        if not role_ok:
            rejected[name] = role_reasons
            continue
        compatible, compatibility_reasons = _compatibility_status(item, technology)
        if not compatible:
            rejected[name] = compatibility_reasons
            continue
        try:
            resolved_names = _closure(name, metadata, agent, capability_value, technology)
        except SkillResolutionError as exc:
            rejected[name] = (str(exc),)
            continue
        for resolved in resolved_names:
            add_origin(resolved, f"auto:{name}")
            if resolved not in selected:
                selected.append(resolved)
            reasons.setdefault(resolved, []).extend(
                (*context_reasons, *role_reasons, *compatibility_reasons)
            )

    decisions = tuple(
        SkillDecision(
            name=name,
            selected=name in selected,
            origins=tuple(origins.get(name, ())),
            reasons=tuple(
                dict.fromkeys(
                    reasons.get(name, ["selected as dependency"])
                    if name in selected
                    else rejected.get(name, ("not selected",))
                )
            ),
            dependencies=metadata[name].requires,
        )
        for name in sorted(metadata)
    )
    return SkillResolutionReport(
        agent=agent.name,
        capability=capability_value.value,
        task=task,
        domain=domain,
        selected=tuple(selected),
        decisions=decisions,
        policy_required=policy_required,
        agent_declared=agent.skills,
        explicitly_requested=explicit,
        technology=technology,
    )


def compose_resolved_skills(project_root: Path, report: SkillResolutionReport) -> str:
    return "\n\n".join(
        f"## Skill: {skill.name}\n{skill.instructions}"
        for skill in (load_skill(project_root, name) for name in report.selected)
    )
