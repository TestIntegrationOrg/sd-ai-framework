from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Iterable, Mapping

from sdai.agent_platform.definitions import AgentDefinition, load_agent_definition
from sdai.agent_platform.models import Capability, Skill
from sdai.agent_platform.skills import SkillError, list_skills, load_skill
from sdai.config import load_yaml
from sdai.path_safety import ensure_within_project
from sdai.policy import EffectiveConfiguration, load_effective_configuration
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
    dependencies: tuple[str, ...] = ()

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


_ALLOWED_SIDECAR_FIELDS = {
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
        raise _fail("SDAI-SKILL-001", f"{label} must be a non-empty string list")
    result: list[str] = []
    for item in value:
        normalized = item.strip()
        if normalized not in result:
            result.append(normalized)
    return tuple(result)


def _capabilities(value: object, *, label: str) -> tuple[Capability, ...]:
    values = _strings(value, label=label)
    result: list[Capability] = []
    for value in values:
        try:
            capability = Capability(value)
        except ValueError as exc:
            raise _fail(
                "SDAI-SKILL-001",
                f"{label} contains unsupported capability '{value}'",
            ) from exc
        if capability not in result:
            result.append(capability)
    return tuple(result)


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
        category_rules: dict[str, str | None] = {}
        for technology, constraint in raw.items():
            name = str(technology).strip()
            if not name:
                raise _fail(
                    "SDAI-SKILL-001",
                    f"{label}.{category} contains an empty technology id",
                )
            if constraint is None:
                category_rules[name] = None
            elif isinstance(constraint, (str, int, float)):
                text = str(constraint).strip()
                if not text:
                    raise _fail(
                        "SDAI-SKILL-001",
                        f"{label}.{category}.{name} constraint cannot be empty",
                    )
                _parse_constraint(text, label=f"{label}.{category}.{name}")
                category_rules[name] = text
            else:
                raise _fail(
                    "SDAI-SKILL-001",
                    f"{label}.{category}.{name} must be a numeric constraint or null",
                )
        result[str(category)] = category_rules
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
    canonical = ensure_within_project(
        root,
        loaded.root / "sdai.yaml",
        label="canonical skill metadata",
    )
    legacy = ensure_within_project(
        root,
        loaded.root / "skill.yaml",
        label="legacy skill metadata",
    )
    path = canonical if canonical.is_file() else legacy if legacy.is_file() else None
    raw: dict[str, object] = {}
    source = _portable(root, loaded.root / "SKILL.md")
    if path is not None:
        loaded_raw = load_yaml(path)
        if not isinstance(loaded_raw, dict):
            raise _fail("SDAI-SKILL-001", f"{_portable(root, path)} must be a mapping")
        raw = dict(loaded_raw)
        source = _portable(root, path)

    # Legacy skill.yaml carries name/description as part of its historical format.
    allowed = set(_ALLOWED_SIDECAR_FIELDS)
    if path is legacy:
        allowed.update({"name", "description"})
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise _fail(
            "SDAI-SKILL-001",
            f"{source} contains unsupported skill metadata key(s): {', '.join(map(str, unknown))}",
        )

    capabilities = _capabilities(
        raw.get("capabilities"),
        label=f"{source}: capabilities",
    )
    # Keep the loader's existing capability interpretation authoritative when the
    # sidecar omits the field (legacy compatibility).
    if not capabilities:
        capabilities = loaded.capabilities
    return SkillMetadata(
        name=loaded.name,
        capabilities=capabilities,
        compatible_agents=_strings(
            raw.get("compatible_agents"),
            label=f"{source}: compatible_agents",
        ),
        requires=_strings(raw.get("requires"), label=f"{source}: requires"),
        compatibility=_compatibility(
            raw.get("compatibility"),
            label=f"{source}: compatibility",
        ),
        selection=_selection(raw.get("selection"), label=f"{source}: selection"),
        source=source,
    )


def _version_tuple(value: str) -> tuple[int, ...]:
    if not _VERSION.fullmatch(value):
        raise _fail(
            "SDAI-SKILL-003",
            f"version '{value}' is not an exact numeric version supported by resolver v1",
        )
    return tuple(int(part) for part in value.split("."))


def _pad(left: tuple[int, ...], right: tuple[int, ...]) -> tuple[tuple[int, ...], tuple[int, ...]]:
    width = max(len(left), len(right))
    return left + (0,) * (width - len(left)), right + (0,) * (width - len(right))


def _parse_constraint(value: str, *, label: str) -> tuple[tuple[str, tuple[int, ...]], ...]:
    clauses: list[tuple[str, tuple[int, ...]]] = []
    for raw in value.split(","):
        clause = raw.strip()
        if not clause:
            raise _fail("SDAI-SKILL-001", f"{label} contains an empty constraint clause")
        match = _CONSTRAINT.fullmatch(clause)
        if match is None:
            raise _fail(
                "SDAI-SKILL-001",
                f"{label} uses unsupported constraint '{clause}'; use numeric versions with >=, >, <=, <, ==, = and comma-separated clauses",
            )
        clauses.append((match.group(1) or "==", _version_tuple(match.group(2))))
    return tuple(clauses)


def _satisfies(version: str, constraint: str) -> bool:
    actual = _version_tuple(version)
    for operator, required in _parse_constraint(constraint, label="skill compatibility"):
        left, right = _pad(actual, required)
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


def _fact_index(report: TechnologyReport) -> dict[tuple[str, str], TechnologyFact]:
    return {(item.category, item.name): item for item in report.technologies}


def _exact_fact_version(fact: TechnologyFact) -> str | None:
    if fact.version is None or not _VERSION.fullmatch(fact.version):
        return None
    if fact.version_source == "declared":
        return fact.version
    if fact.version_source != "detected":
        return None
    matching = [
        evidence
        for evidence in fact.evidence
        if evidence.version == fact.version and not evidence.detector.startswith("declared")
    ]
    if not matching:
        return None
    if any(evidence.detector not in _EXACT_DETECTED_VERSION_SOURCES for evidence in matching):
        return None
    return fact.version


def _compatibility_reasons(
    metadata: SkillMetadata,
    technology: TechnologyReport,
) -> tuple[bool, tuple[str, ...]]:
    facts = _fact_index(technology)
    reasons: list[str] = []
    for category in sorted(metadata.compatibility):
        for name, constraint in sorted(metadata.compatibility[category].items()):
            fact = facts.get((category, name))
            if fact is None:
                reasons.append(f"requires missing technology {category}.{name}")
                return False, tuple(reasons)
            if constraint is None:
                reasons.append(f"matched {category}.{name} presence")
                continue
            exact = _exact_fact_version(fact)
            if exact is None:
                reasons.append(
                    f"cannot prove version compatibility for {category}.{name}; "
                    f"selected={fact.version or '-'} source={fact.version_source}"
                )
                return False, tuple(reasons)
            if not _satisfies(exact, constraint):
                reasons.append(
                    f"incompatible {category}.{name} version {exact}; requires {constraint}"
                )
                return False, tuple(reasons)
            reasons.append(f"matched {category}.{name} {exact} against {constraint}")
    return True, tuple(reasons)


def _role_and_capability_ok(
    metadata: SkillMetadata,
    agent: AgentDefinition,
    capability: Capability,
) -> tuple[bool, tuple[str, ...]]:
    reasons: list[str] = []
    if metadata.capabilities and capability not in metadata.capabilities:
        return False, (f"skill does not support capability {capability.value}",)
    if metadata.compatible_agents and agent.name not in metadata.compatible_agents:
        return False, (f"skill is not compatible with semantic agent {agent.name}",)
    if metadata.selection.roles and agent.name not in metadata.selection.roles:
        return False, (f"auto-selection role filter excludes {agent.name}",)
    if metadata.selection.capabilities and capability not in metadata.selection.capabilities:
        return False, (f"auto-selection capability filter excludes {capability.value}",)
    if metadata.capabilities:
        reasons.append(f"capability {capability.value} allowed")
    if metadata.compatible_agents:
        reasons.append(f"semantic agent {agent.name} allowed")
    return True, tuple(reasons)


def _auto_context_ok(
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
        task_folded = (task or "").casefold()
        matched = next(
            (
                keyword
                for keyword in metadata.selection.task_keywords
                if keyword.casefold() in task_folded
            ),
            None,
        )
        if matched is None:
            return False, ("task keyword filter did not match",)
        reasons.append(f"task keyword '{matched}' matched")
    return True, tuple(reasons)


def _all_metadata(project_root: Path) -> dict[str, SkillMetadata]:
    result: dict[str, SkillMetadata] = {}
    for skill in list_skills(project_root):
        result[skill.name] = load_skill_metadata(project_root, skill)
    return result


def _validate_dependency_graph(metadata: Mapping[str, SkillMetadata]) -> None:
    visiting: list[str] = []
    visited: set[str] = set()

    def visit(name: str) -> None:
        if name in visited:
            return
        if name in visiting:
            cycle = visiting[visiting.index(name) :] + [name]
            raise _fail(
                "SDAI-SKILL-005",
                "skill dependency cycle: " + " -> ".join(cycle),
            )
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


def _merge_origin(origins: dict[str, list[str]], name: str, origin: str) -> None:
    bucket = origins.setdefault(name, [])
    if origin not in bucket:
        bucket.append(origin)


def _strict_validate(
    name: str,
    metadata: Mapping[str, SkillMetadata],
    agent: AgentDefinition,
    capability: Capability,
    technology: TechnologyReport,
    *,
    chain: tuple[str, ...] = (),
) -> tuple[str, ...]:
    item = metadata.get(name)
    if item is None:
        raise _fail("SDAI-SKILL-006", f"required skill '{name}' is not installed")
    ok, role_reasons = _role_and_capability_ok(item, agent, capability)
    if not ok:
        raise _fail(
            "SDAI-SKILL-002",
            f"skill '{name}' is incompatible: {role_reasons[0]}",
        )
    compatible, compatibility_reasons = _compatibility_reasons(item, technology)
    if not compatible:
        raise _fail(
            "SDAI-SKILL-003",
            f"skill '{name}' is incompatible: {compatibility_reasons[-1]}",
        )
    resolved: list[str] = []
    for dependency in item.requires:
        if dependency in chain:
            raise _fail(
                "SDAI-SKILL-005",
                "skill dependency cycle: " + " -> ".join((*chain, name, dependency)),
            )
        for selected in _strict_validate(
            dependency,
            metadata,
            agent,
            capability,
            technology,
            chain=(*chain, name),
        ):
            if selected not in resolved:
                resolved.append(selected)
    if name not in resolved:
        resolved.append(name)
    return tuple(resolved)


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
    capability_value = capability if isinstance(capability, Capability) else Capability(capability)
    agent = load_agent_definition(root, agent_name)
    if not agent.supports(capability_value):
        raise _fail(
            "SDAI-SKILL-002",
            f"semantic agent '{agent.name}' does not support capability '{capability_value.value}'",
        )
    technology = detect_technologies(root)
    policy: EffectiveConfiguration = load_effective_configuration(root, environ=environ)
    policy_required = policy.required_skills(capability_value)
    explicit_requested = tuple(dict.fromkeys(str(name).strip() for name in requested if str(name).strip()))
    metadata = _all_metadata(root)
    _validate_dependency_graph(metadata)

    origins: dict[str, list[str]] = {}
    direct: list[str] = []
    for origin, names in (
        ("agent", agent.skills),
        ("policy", policy_required),
        ("requested", explicit_requested),
    ):
        for name in names:
            _merge_origin(origins, name, origin)
            if name not in direct:
                direct.append(name)

    selected: list[str] = []
    selected_reason_map: dict[str, list[str]] = {}
    for name in direct:
        closure = _strict_validate(
            name,
            metadata,
            agent,
            capability_value,
            technology,
        )
        for resolved in closure:
            if resolved != name:
                _merge_origin(origins, resolved, f"dependency:{name}")
            if resolved not in selected:
                selected.append(resolved)
            selected_reason_map.setdefault(resolved, []).append("required by explicit resolver seed")

    rejected: dict[str, tuple[str, ...]] = {}
    for name in sorted(metadata):
        if name in selected:
            continue
        item = metadata[name]
        context_ok, context_reasons = _auto_context_ok(item, task=task, domain=domain)
        if not context_ok:
            rejected[name] = context_reasons
            continue
        role_ok, role_reasons = _role_and_capability_ok(item, agent, capability_value)
        if not role_ok:
            rejected[name] = role_reasons
            continue
        compatible, compatibility_reasons = _compatibility_reasons(item, technology)
        if not compatible:
            rejected[name] = compatibility_reasons
            continue
        try:
            closure = _strict_validate(
                name,
                metadata,
                agent,
                capability_value,
                technology,
            )
        except SkillResolutionError as exc:
            rejected[name] = (str(exc),)
            continue
        for resolved in closure:
            _merge_origin(origins, resolved, f"auto:{name}")
            if resolved not in selected:
                selected.append(resolved)
            selected_reason_map.setdefault(resolved, []).extend(
                (*context_reasons, *role_reasons, *compatibility_reasons)
            )

    decisions: list[SkillDecision] = []
    for name in sorted(metadata):
        item = metadata[name]
        is_selected = name in selected
        reasons = (
            tuple(dict.fromkeys(selected_reason_map.get(name, ["selected as dependency"])))
            if is_selected
            else rejected.get(name, ("not selected",))
        )
        decisions.append(
            SkillDecision(
                name=name,
                selected=is_selected,
                origins=tuple(origins.get(name, ())),
                reasons=reasons,
                dependencies=item.requires,
            )
        )

    # Dependency-first deterministic prompt order. Direct/auto roots follow after their
    # requirements, with no duplicate instruction sections.
    ordered = tuple(name for name in selected if name in metadata)
    return SkillResolutionReport(
        agent=agent.name,
        capability=capability_value.value,
        task=task,
        domain=domain,
        selected=ordered,
        decisions=tuple(decisions),
        policy_required=policy_required,
        agent_declared=agent.skills,
        explicitly_requested=explicit_requested,
        technology=technology,
    )


def compose_resolved_skills(project_root: Path, report: SkillResolutionReport) -> str:
    sections: list[str] = []
    for name in report.selected:
        skill = load_skill(project_root, name)
        sections.append(f"## Skill: {skill.name}\n{skill.instructions}")
    return "\n\n".join(sections)
