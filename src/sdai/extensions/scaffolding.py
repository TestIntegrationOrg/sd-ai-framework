from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
import re

import yaml

from sdai.agent_platform.definitions import load_agent_definition
from sdai.agent_platform.skills import load_skill
from sdai.artifacts import write_text
from sdai.extensions.manifests import (
    API_VERSION,
    ExtensionKind,
    ExtensionManifest,
    load_extension_manifest,
    parse_extension_manifest,
)
from sdai.path_safety import ensure_within_project
from sdai.workflows import load_workflow


class ScaffoldKind(StrEnum):
    SKILL = "skill"
    AGENT = "agent"
    WORKFLOW = "workflow"
    WORKFLOW_COMPONENT = "workflow-component"
    VALIDATOR = "validator"
    QUALITY_GATE = "quality-gate"
    INTEGRATION = "integration"
    PACK = "pack"


@dataclass(frozen=True)
class ScaffoldResult:
    kind: ScaffoldKind
    id: str
    paths: tuple[Path, ...]


_KIND_TO_EXTENSION = {
    ScaffoldKind.WORKFLOW_COMPONENT: ExtensionKind.WORKFLOW_COMPONENT,
    ScaffoldKind.VALIDATOR: ExtensionKind.VALIDATOR,
    ScaffoldKind.QUALITY_GATE: ExtensionKind.QUALITY_GATE,
    ScaffoldKind.INTEGRATION: ExtensionKind.INTEGRATION,
    ScaffoldKind.PACK: ExtensionKind.PACK,
}
_KIND_DIRECTORY = {
    ScaffoldKind.WORKFLOW_COMPONENT: "workflow-components",
    ScaffoldKind.VALIDATOR: "validators",
    ScaffoldKind.QUALITY_GATE: "quality-gates",
    ScaffoldKind.INTEGRATION: "integrations",
    ScaffoldKind.PACK: "packs",
}
_AGENT_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


def _kind(value: ScaffoldKind | str) -> ScaffoldKind:
    try:
        return ScaffoldKind(value)
    except ValueError as exc:
        supported = ", ".join(item.value for item in ScaffoldKind)
        raise ValueError(
            f"unsupported extension scaffold kind {value!r}; supported kinds: {supported}"
        ) from exc


def _new_id(value: str, kind: ScaffoldKind) -> str:
    value = value.strip()
    parse_extension_manifest(
        {
            "apiVersion": API_VERSION,
            "kind": ExtensionKind.SKILL.value,
            "metadata": {"id": value, "version": "0.1.0"},
            "spec": {},
        }
    )
    if kind is ScaffoldKind.AGENT and not _AGENT_NAME.fullmatch(value):
        raise ValueError(
            "agent id must be lowercase kebab-case using letters, numbers, and hyphens "
            "and contain at most 64 characters"
        )
    return value


def _safe(root: Path, relative: Path, *, label: str) -> Path:
    root = root.resolve()
    return ensure_within_project(root, root / relative, label=label)


def _preflight(paths: tuple[Path, ...], *, force: bool) -> None:
    if force:
        return
    collisions = [path for path in paths if path.exists()]
    if collisions:
        joined = ", ".join(str(path) for path in collisions)
        raise FileExistsError(f"extension scaffold already exists: {joined}")


def _write(path: Path, content: str, *, force: bool) -> Path:
    return write_text(path, content, overwrite=force)


def _skill_scaffold(root: Path, extension_id: str, *, force: bool) -> tuple[Path, ...]:
    skill_root = _safe(
        root,
        Path(".agents") / "skills" / extension_id,
        label="skill scaffold path",
    )
    paths = (skill_root / "SKILL.md", skill_root / "sdai.yaml")
    _preflight(paths, force=force)
    _write(
        paths[0],
        f"""---
name: {extension_id}
description: Use when this reusable engineering skill applies.
---

# {extension_id}

Describe the engineering technique, constraints, decision points, and examples here.
""",
        force=force,
    )
    sidecar = {
        "version": 1,
        "capabilities": [],
        "compatible_agents": [],
        "requires": [],
        "compatibility": {},
        "selection": {
            "auto": False,
            "roles": [],
            "capabilities": [],
            "task_keywords": [],
            "domains": [],
        },
    }
    _write(
        paths[1],
        yaml.safe_dump(sidecar, sort_keys=False),
        force=force,
    )
    return paths


def _agent_scaffold(root: Path, extension_id: str, *, force: bool) -> tuple[Path, ...]:
    path = _safe(
        root,
        Path(".sdai") / "agents" / f"{extension_id}.agent.md",
        label="agent scaffold path",
    )
    paths = (path,)
    _preflight(paths, force=force)
    _write(
        path,
        f"""---
name: {extension_id}
description: Semantic agent for {extension_id} responsibilities.
capabilities:
  - coding
skills: []
execution_mode: advisory
providers: {{}}
---

Operate only within the assigned capability and approved SDAI artifacts. State assumptions, conflicts, and required human decisions explicitly.
""",
        force=force,
    )
    return paths


def _workflow_scaffold(root: Path, extension_id: str, *, force: bool) -> tuple[Path, ...]:
    path = _safe(
        root,
        Path(".sdai") / "workflows" / f"{extension_id}.yaml",
        label="workflow scaffold path",
    )
    paths = (path,)
    _preflight(paths, force=force)
    _write(
        path,
        f"""version: 5
name: {extension_id}
validation_mode: standard
steps:
  - id: validate
    type: validate
""",
        force=force,
    )
    return paths


def _manifest_scaffold(
    root: Path,
    kind: ScaffoldKind,
    extension_id: str,
    *,
    force: bool,
) -> tuple[Path, ...]:
    manifest_kind = _KIND_TO_EXTENSION[kind]
    directory = _KIND_DIRECTORY[kind]
    path = _safe(
        root,
        Path(".sdai") / "extensions" / directory / f"{extension_id}.yaml",
        label=f"{kind.value} scaffold path",
    )
    paths = (path,)
    _preflight(paths, force=force)
    if kind is ScaffoldKind.WORKFLOW_COMPONENT:
        spec: dict[str, object] = {
            "inputs": {
                "step-id": {
                    "type": "string",
                    "default": f"{extension_id}-validate",
                }
            },
            "requires": [],
            "steps": [
                {
                    "id": "${{ inputs.step-id }}",
                    "type": "validate",
                }
            ],
        }
    else:
        spec = {}
    payload = {
        "apiVersion": API_VERSION,
        "kind": manifest_kind.value,
        "metadata": {
            "id": extension_id,
            "version": "0.1.0",
            "description": f"{kind.value} extension {extension_id}",
        },
        "spec": spec,
    }
    _write(path, yaml.safe_dump(payload, sort_keys=False), force=force)
    return paths


def create_extension_scaffold(
    project_root: Path,
    kind: ScaffoldKind | str,
    extension_id: str,
    *,
    force: bool = False,
) -> ScaffoldResult:
    root = project_root.resolve()
    scaffold_kind = _kind(kind)
    extension_id = _new_id(extension_id, scaffold_kind)

    if scaffold_kind is ScaffoldKind.SKILL:
        paths = _skill_scaffold(root, extension_id, force=force)
    elif scaffold_kind is ScaffoldKind.AGENT:
        paths = _agent_scaffold(root, extension_id, force=force)
    elif scaffold_kind is ScaffoldKind.WORKFLOW:
        paths = _workflow_scaffold(root, extension_id, force=force)
    else:
        paths = _manifest_scaffold(root, scaffold_kind, extension_id, force=force)

    validate_extension_scaffold(root, scaffold_kind, extension_id)
    return ScaffoldResult(scaffold_kind, extension_id, paths)


def _manifest_path(root: Path, kind: ScaffoldKind, target: str) -> Path:
    supplied = Path(target)
    if supplied.suffix.lower() in {".yaml", ".yml"} or len(supplied.parts) > 1:
        return supplied
    return Path(".sdai") / "extensions" / _KIND_DIRECTORY[kind] / f"{target}.yaml"


def validate_extension_scaffold(
    project_root: Path,
    kind: ScaffoldKind | str,
    target: str,
) -> ExtensionManifest | None:
    root = project_root.resolve()
    scaffold_kind = _kind(kind)

    if scaffold_kind is ScaffoldKind.AGENT:
        load_agent_definition(root, target)
        return None
    if scaffold_kind is ScaffoldKind.SKILL:
        load_skill(root, target)
        return None
    if scaffold_kind is ScaffoldKind.WORKFLOW:
        load_workflow(root, target)
        return None

    expected_kind = _KIND_TO_EXTENSION[scaffold_kind]
    manifest = load_extension_manifest(
        root, _manifest_path(root, scaffold_kind, target)
    )
    if manifest.kind is not expected_kind:
        raise ValueError(
            f"expected {expected_kind.value} manifest for {scaffold_kind.value}, "
            f"found {manifest.kind.value}"
        )
    return manifest