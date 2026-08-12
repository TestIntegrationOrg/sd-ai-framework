from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any, Iterable
from xml.etree import ElementTree

import yaml

from sdai.config import load_yaml
from sdai.models import FeatureContext, LifecycleMode
from sdai.path_safety import ensure_within_project
from sdai.policy import EffectiveConfiguration, load_effective_configuration
from sdai.text import read_utf8_text


@dataclass(frozen=True)
class ArchitectureArtifactFinding:
    level: str
    code: str
    message: str
    requirement: str
    artifacts: tuple[str, ...] = ()


DEFAULT_REQUIREMENTS: dict[str, dict[str, Any]] = {
    "specification": {
        "description": "Approved feature specification",
        "any_of": ["specification.md"],
        "check": "markdown",
    },
    "rfc": {
        "description": "Decision-ready engineering RFC",
        "any_of": ["rfc/RFC-*.md"],
        "check": "markdown",
    },
    "architecture-alternatives": {
        "description": "Architecture alternatives and explicit trade-offs",
        "any_of": ["architecture/architecture.md"],
        "check": "alternatives",
    },
    "decision-matrix": {
        "description": "Architecture decision matrix",
        "any_of": ["architecture/decision-matrix.md"],
        "check": "markdown",
    },
    "adr": {
        "description": "Architecture Decision Record",
        "any_of": ["adr/ADR-*.md"],
        "check": "adr",
    },
    "c4-context": {
        "description": "C4 system-context diagram",
        "any_of": [
            "architecture/diagrams/context.puml",
            "architecture/diagrams/context.mmd",
            "architecture/diagrams/context.drawio",
        ],
        "check": "diagram",
    },
    "component-diagram": {
        "description": "Component-level architecture diagram",
        "any_of": [
            "architecture/diagrams/component*.puml",
            "architecture/diagrams/component*.mmd",
            "architecture/diagrams/component*.drawio",
        ],
        "check": "diagram",
    },
    "sequence-diagram": {
        "description": "Runtime interaction/sequence diagram",
        "any_of": [
            "architecture/diagrams/*sequence*.puml",
            "architecture/diagrams/*sequence*.mmd",
            "architecture/diagrams/*sequence*.drawio",
        ],
        "check": "diagram",
    },
    "security-model": {
        "description": "Threat/security model",
        "any_of": ["security/threat-model.md"],
        "check": "markdown",
    },
    "api-event-contracts": {
        "description": "Version-controlled API/event/schema contract",
        "any_of": [
            "contracts/openapi*.yaml",
            "contracts/openapi*.yml",
            "contracts/openapi*.json",
            "contracts/asyncapi*.yaml",
            "contracts/asyncapi*.yml",
            "contracts/asyncapi*.json",
            "contracts/**/*.proto",
            "contracts/schemas/*.json",
            "contracts/schemas/*.yaml",
            "contracts/schemas/*.yml",
        ],
        "check": "contract",
    },
    "traceability": {
        "description": "Requirement-to-task traceability",
        "any_of": ["tasks.yaml", "specification.md"],
        "check": "traceability",
    },
}


DEFAULT_MODE_REQUIREMENTS: dict[str, tuple[str, ...]] = {
    "light": (),
    "standard": (
        "specification",
        "architecture-alternatives",
        "decision-matrix",
        "adr",
        "traceability",
    ),
    "critical": (
        "specification",
        "rfc",
        "architecture-alternatives",
        "decision-matrix",
        "adr",
        "c4-context",
        "component-diagram",
        "sequence-diagram",
        "security-model",
        "api-event-contracts",
        "traceability",
    ),
}


DEFAULT_CONFIG = {
    "version": 1,
    "modes": {key: {"required": list(value)} for key, value in DEFAULT_MODE_REQUIREMENTS.items()},
    "settings": {
        "allow_waivers": True,
        "waiver_file": "architecture/validation-waivers.yaml",
        "critical_waiver_requires_approval": True,
    },
    "requirements": DEFAULT_REQUIREMENTS,
}

_SAFE_REQUIREMENT = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_ALT_HEADING = re.compile(r"(?im)^#{1,6}\s+(?:option|alternative)\b")
_SPEC_ID = re.compile(r"\b(?:FR|NFR|AC)-\d+\b")


def scaffold_architecture_validation() -> str:
    return yaml.safe_dump(DEFAULT_CONFIG, sort_keys=False)


def _safe_pattern(value: str) -> str:
    candidate = Path(value)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError("architecture validation patterns must stay inside the feature workspace")
    return value.replace("\\", "/")


def _validate_config(data: dict[str, Any]) -> dict[str, Any]:
    if data.get("version", 1) != 1:
        raise ValueError("architecture validation version must be 1")
    allowed_top = {"version", "modes", "settings", "requirements"}
    unknown = sorted(set(data) - allowed_top)
    if unknown:
        raise ValueError(f"architecture validation contains unsupported key(s): {', '.join(unknown)}")

    modes = data.get("modes") or {}
    settings = data.get("settings") or {}
    requirements = data.get("requirements") or {}
    if not isinstance(modes, dict) or not isinstance(settings, dict) or not isinstance(requirements, dict):
        raise ValueError("architecture validation modes/settings/requirements must be mappings")

    for mode_name, raw in modes.items():
        if str(mode_name) not in {m.value for m in LifecycleMode}:
            raise ValueError(f"unknown architecture validation mode '{mode_name}'")
        if not isinstance(raw, dict) or set(raw) - {"required"}:
            raise ValueError(f"architecture validation mode '{mode_name}' supports only 'required'")
        required = raw.get("required") or []
        if not isinstance(required, list) or not all(isinstance(item, str) for item in required):
            raise ValueError(f"architecture validation mode '{mode_name}'.required must be a string list")

    allowed_settings = {"allow_waivers", "waiver_file", "critical_waiver_requires_approval"}
    unknown_settings = sorted(set(settings) - allowed_settings)
    if unknown_settings:
        raise ValueError(f"architecture validation settings contain unsupported key(s): {', '.join(unknown_settings)}")
    for bool_key in ("allow_waivers", "critical_waiver_requires_approval"):
        if bool_key in settings and not isinstance(settings[bool_key], bool):
            raise ValueError(f"architecture validation settings.{bool_key} must be true or false")
    if "waiver_file" in settings and not isinstance(settings["waiver_file"], str):
        raise ValueError("architecture validation settings.waiver_file must be a string")
    if settings.get("waiver_file"):
        _safe_pattern(settings["waiver_file"])

    for requirement_id, raw in requirements.items():
        requirement_id = str(requirement_id)
        if not _SAFE_REQUIREMENT.fullmatch(requirement_id):
            raise ValueError(f"unsafe architecture requirement id '{requirement_id}'")
        if not isinstance(raw, dict):
            raise ValueError(f"architecture requirement '{requirement_id}' must be a mapping")
        unknown_req = sorted(set(raw) - {"description", "any_of", "check"})
        if unknown_req:
            raise ValueError(
                f"architecture requirement '{requirement_id}' contains unsupported key(s): {', '.join(unknown_req)}"
            )
        any_of = raw.get("any_of") or []
        if not isinstance(any_of, list) or not all(isinstance(item, str) and item.strip() for item in any_of):
            raise ValueError(f"architecture requirement '{requirement_id}'.any_of must be a string list")
        for pattern in any_of:
            _safe_pattern(pattern.strip())
        check = str(raw.get("check") or "presence")
        if check not in {"presence", "markdown", "alternatives", "adr", "diagram", "contract", "traceability"}:
            raise ValueError(f"architecture requirement '{requirement_id}' has unsupported check '{check}'")

    known = set(requirements)
    for mode_name, raw in modes.items():
        unknown_required = sorted(set(raw.get("required") or []) - known)
        if unknown_required:
            raise ValueError(
                f"architecture validation mode '{mode_name}' references unknown requirement(s): {', '.join(unknown_required)}"
            )
    return data


def load_architecture_validation_config(project_root: Path) -> dict[str, Any]:
    root = project_root.resolve()
    path = ensure_within_project(
        root,
        root / ".sdai" / "architecture-validation.yaml",
        label="architecture validation config",
    )
    if not path.exists():
        return _validate_config(DEFAULT_CONFIG)
    return _validate_config(load_yaml(path))


def _matches(context: FeatureContext, patterns: Iterable[str]) -> list[Path]:
    results: dict[str, Path] = {}
    root = context.feature_dir
    for raw in patterns:
        pattern = _safe_pattern(raw.strip())
        for path in root.glob(pattern):
            if not path.is_file():
                continue
            safe = ensure_within_project(root, path, label="architecture validation artifact")
            results[safe.relative_to(root).as_posix()] = safe
    return [results[key] for key in sorted(results)]


def _text(path: Path) -> str:
    return read_utf8_text(path)


def _non_placeholder_markdown(path: Path) -> bool:
    text = _text(path).strip()
    if len(text) < 20:
        return False
    meaningful = [line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    return any(line.lower() not in {"todo", "tbd", "n/a", "na"} for line in meaningful)


def _check_drawio(path: Path) -> str | None:
    raw = _text(path)
    if "<!DOCTYPE" in raw.upper() or "<!ENTITY" in raw.upper():
        return "Draw.io XML must not contain DTD/entity declarations"
    try:
        root = ElementTree.fromstring(raw)
    except (ElementTree.ParseError, UnicodeDecodeError) as exc:
        return f"invalid Draw.io XML: {exc}"
    tag = root.tag.rsplit("}", 1)[-1]
    if tag != "mxfile":
        return "Draw.io root must be <mxfile>"
    diagrams = [node for node in root.iter() if node.tag.rsplit("}", 1)[-1] == "diagram"]
    graphs = [node for node in root.iter() if node.tag.rsplit("}", 1)[-1] == "mxGraphModel"]
    cells = [
        node for node in root.iter()
        if node.tag.rsplit("}", 1)[-1] == "mxCell"
        and (node.attrib.get("vertex") == "1" or node.attrib.get("edge") == "1")
    ]
    if not diagrams or not graphs:
        return "Draw.io must contain <diagram> and <mxGraphModel>"
    if not cells:
        return "Draw.io diagram contains no editable vertex/edge cells"
    return None


def _check_puml(path: Path) -> str | None:
    text = _text(path).strip()
    if not text.startswith("@startuml") or not text.endswith("@enduml"):
        return "PlantUML must start with @startuml and end with @enduml"
    return None


def _check_mermaid(path: Path) -> str | None:
    text = _text(path).lstrip()
    accepted = ("graph ", "flowchart ", "sequenceDiagram", "C4Context", "C4Container", "C4Component")
    if not text.startswith(accepted):
        return "Mermaid source must start with a supported diagram declaration"
    return None


def _check_diagram(path: Path) -> str | None:
    suffix = path.suffix.lower()
    if suffix == ".puml":
        return _check_puml(path)
    if suffix == ".drawio":
        return _check_drawio(path)
    if suffix == ".mmd":
        return _check_mermaid(path)
    return f"unsupported diagram format '{suffix}'"


def _load_mapping(path: Path) -> dict[str, Any] | None:
    try:
        if path.suffix.lower() == ".json":
            import json
            data = json.loads(_text(path))
        else:
            data = yaml.safe_load(_text(path))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _check_contract(path: Path) -> str | None:
    if path.suffix.lower() == ".proto":
        text = _text(path)
        if "syntax" not in text or not re.search(r"(?m)^\s*(message|service)\s+\w+", text):
            return "Proto contract must declare syntax and at least one message/service"
        return None
    data = _load_mapping(path)
    if data is None:
        return "contract must be valid YAML or JSON"
    if not any(key in data for key in ("openapi", "swagger", "asyncapi", "$schema")):
        return "contract must declare openapi/swagger/asyncapi/$schema"
    return None


def _check_traceability(context: FeatureContext) -> str | None:
    spec = context.artifact("specification.md")
    tasks = context.artifact("tasks.yaml")
    if not spec.exists() or not tasks.exists():
        return "traceability requires specification.md and tasks.yaml"
    spec_ids = set(_SPEC_ID.findall(_text(spec)))
    if not spec_ids:
        return "specification contains no FR-/NFR-/AC- identifiers"
    try:
        data = load_yaml(tasks)
    except Exception as exc:
        return f"tasks.yaml is invalid: {exc}"
    raw_tasks = data.get("tasks") or []
    if not isinstance(raw_tasks, list) or not raw_tasks:
        return "tasks.yaml contains no tasks"
    missing: list[str] = []
    unknown: list[str] = []
    for item in raw_tasks:
        if not isinstance(item, dict):
            return "tasks.yaml tasks must be mappings"
        task_id = str(item.get("id") or "?")
        traces = item.get("traces_to")
        if isinstance(traces, str):
            traces = [traces]
        if not isinstance(traces, list) or not any(isinstance(value, str) and value.strip() for value in traces):
            missing.append(task_id)
            continue
        referenced = {value.strip() for value in traces if isinstance(value, str) and value.strip()}
        if not referenced.intersection(spec_ids):
            unknown.append(task_id)
    if missing:
        return f"task(s) without traces_to: {', '.join(missing)}"
    if unknown:
        return f"task(s) do not trace to a specification FR/NFR/AC id: {', '.join(unknown)}"
    return None


def _artifact_problem(context: FeatureContext, check: str, paths: list[Path]) -> str | None:
    if check == "presence":
        return None
    if check == "traceability":
        return _check_traceability(context)
    if check == "markdown":
        return None if any(_non_placeholder_markdown(path) for path in paths) else "artifact is empty or placeholder-only"
    if check == "alternatives":
        for path in paths:
            text = _text(path)
            if len(_ALT_HEADING.findall(text)) >= 2:
                return None
        return "architecture must document at least two explicit Option/Alternative sections"
    if check == "adr":
        return None if any(_non_placeholder_markdown(path) for path in paths) else "ADR is empty or placeholder-only"
    if check == "diagram":
        problems = [problem for path in paths if (problem := _check_diagram(path)) is not None]
        return None if len(problems) < len(paths) else "; ".join(problems[:3])
    if check == "contract":
        problems = [problem for path in paths if (problem := _check_contract(path)) is not None]
        return None if len(problems) < len(paths) else "; ".join(problems[:3])
    return f"unsupported validator check '{check}'"


def _load_waivers(context: FeatureContext, config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    settings = config.get("settings") or {}
    relative = str(settings.get("waiver_file") or "architecture/validation-waivers.yaml")
    path = context.artifact(_safe_pattern(relative))
    if not path.exists():
        return {}
    data = load_yaml(path)
    if data.get("version", 1) != 1:
        raise ValueError("architecture validation waiver version must be 1")
    waivers = data.get("waivers") or {}
    if not isinstance(waivers, dict):
        raise ValueError("architecture validation waivers must be a mapping")
    result: dict[str, dict[str, Any]] = {}
    for key, raw in waivers.items():
        if not isinstance(raw, dict):
            raise ValueError(f"waiver '{key}' must be a mapping")
        result[str(key)] = raw
    return result


def _required_for_mode(
    mode: LifecycleMode,
    config: dict[str, Any],
    effective: EffectiveConfiguration | None,
) -> tuple[str, ...]:
    configured = list(((config.get("modes") or {}).get(mode.value) or {}).get("required") or [])
    if effective is not None:
        for requirement in effective.required_architecture_artifacts(mode.value):
            if requirement not in configured:
                configured.append(requirement)
    return tuple(configured)


def validate_architecture_artifacts(
    context: FeatureContext,
    mode: LifecycleMode,
    *,
    effective_configuration: EffectiveConfiguration | None = None,
) -> list[ArchitectureArtifactFinding]:
    config = load_architecture_validation_config(context.project_root)
    effective = effective_configuration
    if effective is None and (context.project_root / ".sdai" / "config.yaml").exists():
        effective = load_effective_configuration(context.project_root)
    required = _required_for_mode(mode, config, effective)
    requirements = config.get("requirements") or {}
    settings = config.get("settings") or {}
    allow_waivers = bool(settings.get("allow_waivers", True)) and (
        effective.architecture_allow_waivers if effective is not None else True
    )
    require_waiver_approval = bool(settings.get("critical_waiver_requires_approval", True)) and mode == LifecycleMode.CRITICAL
    waivers = _load_waivers(context, config) if allow_waivers else {}

    findings: list[ArchitectureArtifactFinding] = []
    for requirement_id in required:
        raw = requirements.get(requirement_id)
        if not isinstance(raw, dict):
            findings.append(
                ArchitectureArtifactFinding(
                    "ERROR",
                    "ARCH_REQUIREMENT_UNDEFINED",
                    f"Required architecture artifact '{requirement_id}' has no repository validation definition",
                    requirement_id,
                )
            )
            continue

        paths = _matches(context, raw.get("any_of") or [])
        problem = "no matching artifact" if not paths else _artifact_problem(context, str(raw.get("check") or "presence"), paths)
        if problem is None:
            findings.append(
                ArchitectureArtifactFinding(
                    "INFO",
                    "ARCH_ARTIFACT_OK",
                    f"{raw.get('description') or requirement_id}: satisfied",
                    requirement_id,
                    tuple(path.relative_to(context.feature_dir).as_posix() for path in paths),
                )
            )
            continue

        waiver = waivers.get(requirement_id)
        if waiver is not None:
            reason = str(waiver.get("reason") or "").strip()
            approved_by = str(waiver.get("approved_by") or "").strip()
            if reason and (approved_by or not require_waiver_approval):
                suffix = f"; approved_by={approved_by}" if approved_by else ""
                findings.append(
                    ArchitectureArtifactFinding(
                        "WARN",
                        "ARCH_ARTIFACT_WAIVED",
                        f"{raw.get('description') or requirement_id} waived: {reason}{suffix}",
                        requirement_id,
                    )
                )
                continue

        findings.append(
            ArchitectureArtifactFinding(
                "ERROR",
                "ARCH_ARTIFACT_INVALID" if paths else "ARCH_ARTIFACT_MISSING",
                f"{raw.get('description') or requirement_id}: {problem}",
                requirement_id,
                tuple(path.relative_to(context.feature_dir).as_posix() for path in paths),
            )
        )

    return findings


def has_architecture_blockers(findings: Iterable[ArchitectureArtifactFinding]) -> bool:
    return any(finding.level == "ERROR" for finding in findings)


def format_architecture_artifact_report(
    feature_id: str,
    mode: LifecycleMode,
    findings: Iterable[ArchitectureArtifactFinding],
) -> str:
    """Render a stable human-readable checklist for CLI/CI surfaces."""
    rows = list(findings)
    labels = {"INFO": "PASS", "WARN": "WAIVE", "ERROR": "FAIL"}
    lines = [f"Architecture artifact validation — {feature_id} ({mode.value})"]
    for finding in rows:
        label = labels.get(finding.level, finding.level)
        artifacts = f" [{', '.join(finding.artifacts)}]" if finding.artifacts else ""
        lines.append(f"{label:5} {finding.requirement:28} {finding.message}{artifacts}")
    lines.append("Result: BLOCKED" if has_architecture_blockers(rows) else "Result: PASS")
    return "\n".join(lines)
