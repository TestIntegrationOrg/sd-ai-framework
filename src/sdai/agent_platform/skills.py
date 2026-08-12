from __future__ import annotations

from pathlib import Path
import re
from typing import Any

import yaml

from sdai.agent_platform.models import Capability, Skill
from sdai.config import load_yaml
from sdai.extensions import (
    API_VERSION,
    ExtensionKind,
    ExtensionManifest,
    ExtensionMetadata,
    ExtensionRegistry,
    RegistryEntry,
    RegistryLayer,
)
from sdai.path_safety import ensure_within_project
from sdai.text import read_utf8_text


class SkillError(RuntimeError):
    pass


_SAFE_SKILL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_CANONICAL_FORMAT = "agents-skill"
_LEGACY_FORMAT = "sdai-legacy-skill"


def _validate_name(name: str) -> str:
    name = name.strip()
    if not _SAFE_SKILL_NAME.fullmatch(name):
        raise SkillError(
            "skill name must use only letters, numbers, dot, underscore, or hyphen"
        )
    return name


def _capabilities(values: object) -> tuple[Capability, ...]:
    if values is None:
        return ()
    if not isinstance(values, list):
        raise SkillError("skill capabilities must be a list")
    try:
        return tuple(Capability(str(value)) for value in values)
    except ValueError as exc:
        raise SkillError(str(exc)) from exc


def _frontmatter(text: str, path: Path) -> tuple[dict[str, Any], str]:
    if not text.startswith("---\n"):
        raise SkillError(f"Skill '{path}' must start with YAML frontmatter")
    end = text.find("\n---\n", 4)
    if end < 0:
        raise SkillError(f"Skill '{path}' has unterminated YAML frontmatter")
    raw = yaml.safe_load(text[4:end]) or {}
    if not isinstance(raw, dict):
        raise SkillError(f"Skill '{path}' frontmatter must be a mapping")
    body = text[end + 5 :].strip()
    if not body:
        raise SkillError(f"Skill '{path}' must contain instructions")
    return raw, body


def _canonical_root(project_root: Path, name: str) -> Path:
    name = _validate_name(name)
    root = project_root.resolve() / ".agents" / "skills" / name
    return ensure_within_project(project_root, root, label="canonical skill path")


def _legacy_root(project_root: Path, name: str) -> Path:
    name = _validate_name(name)
    root = project_root.resolve() / ".sdai" / "skills" / name
    return ensure_within_project(project_root, root, label="legacy skill path")


def _load_canonical(project_root: Path, name: str) -> Skill:
    name = _validate_name(name)
    root = _canonical_root(project_root, name)
    path = ensure_within_project(
        project_root, root / "SKILL.md", label="canonical SKILL.md"
    )
    if not path.exists():
        raise SkillError(f"Canonical skill '{name}' must contain SKILL.md")
    metadata, instructions = _frontmatter(read_utf8_text(path), path)
    metadata_name = str(metadata.get("name") or "").strip()
    if not metadata_name:
        raise SkillError(f"Canonical skill '{name}' requires frontmatter name")
    if metadata_name != name:
        raise SkillError(
            f"Skill directory '{name}' does not match SKILL.md name '{metadata_name}'"
        )
    description = str(metadata.get("description") or "").strip()
    if not description:
        raise SkillError(f"Canonical skill '{name}' requires frontmatter description")
    sidecar = ensure_within_project(
        project_root, root / "sdai.yaml", label="skill sidecar"
    )
    metadata_sdai = load_yaml(sidecar) if sidecar.exists() else {}
    return Skill(
        name=name,
        description=description,
        capabilities=_capabilities(metadata_sdai.get("capabilities")),
        instructions=instructions,
        root=root,
    )


def _load_legacy(project_root: Path, name: str) -> Skill:
    name = _validate_name(name)
    root = _legacy_root(project_root, name)
    manifest_path = ensure_within_project(
        project_root, root / "skill.yaml", label="legacy skill manifest"
    )
    instructions_path = ensure_within_project(
        project_root, root / "SKILL.md", label="legacy SKILL.md"
    )
    if not manifest_path.exists() or not instructions_path.exists():
        raise SkillError(f"Skill '{name}' must contain skill.yaml and SKILL.md")
    manifest = load_yaml(manifest_path)
    manifest_name = str(manifest.get("name") or name)
    if manifest_name != name:
        raise SkillError(
            f"Skill directory '{name}' does not match manifest name '{manifest_name}'"
        )
    return Skill(
        name=name,
        description=str(manifest.get("description") or ""),
        capabilities=_capabilities(manifest.get("capabilities") or []),
        instructions=read_utf8_text(instructions_path).strip(),
        root=root,
    )


def _skill_registry_manifest(
    name: str,
    source: Path,
    *,
    format_name: str,
) -> ExtensionManifest:
    """Adapt an existing skill layout into the extension registry envelope.

    This compatibility adapter intentionally constructs the in-memory dataclass
    directly instead of applying the stricter external `sdai/v1` ID grammar.
    Existing v0.5 skill names may contain uppercase characters and remain valid.
    """

    return ExtensionManifest(
        api_version=API_VERSION,
        kind=ExtensionKind.SKILL,
        metadata=ExtensionMetadata(id=name, version="0.0.0"),
        spec={"format": format_name},
        source=str(source),
    )


def _register_skill_candidate(
    registry: ExtensionRegistry,
    project_root: Path,
    name: str,
) -> RegistryEntry | None:
    name = _validate_name(name)
    canonical_root = _canonical_root(project_root, name)
    canonical_path = ensure_within_project(
        project_root,
        canonical_root / "SKILL.md",
        label="canonical SKILL.md",
    )
    if canonical_path.exists():
        return registry.register(
            _skill_registry_manifest(
                name,
                canonical_path,
                format_name=_CANONICAL_FORMAT,
            ),
            layer=RegistryLayer.REPO,
            source=str(canonical_path),
            path=canonical_path,
        )

    legacy_root = _legacy_root(project_root, name)
    legacy_manifest = ensure_within_project(
        project_root,
        legacy_root / "skill.yaml",
        label="legacy skill manifest",
    )
    if legacy_manifest.exists():
        return registry.register(
            _skill_registry_manifest(
                name,
                legacy_manifest,
                format_name=_LEGACY_FORMAT,
            ),
            layer=RegistryLayer.REPO,
            source=str(legacy_manifest),
            path=legacy_manifest,
        )
    return None


def build_skill_registry(project_root: Path) -> ExtensionRegistry:
    """Index canonical and legacy repository skills using existing precedence."""

    project_root = project_root.resolve()
    registry = ExtensionRegistry()
    canonical_names: set[str] = set()

    canonical = ensure_within_project(
        project_root,
        project_root / ".agents" / "skills",
        label="canonical skills directory",
    )
    if canonical.exists():
        for path in sorted(canonical.iterdir(), key=lambda item: item.name):
            if path.is_dir() and (path / "SKILL.md").exists():
                name = _validate_name(path.name)
                canonical_names.add(name)
                _register_skill_candidate(registry, project_root, name)

    legacy = ensure_within_project(
        project_root,
        project_root / ".sdai" / "skills",
        label="legacy skills directory",
    )
    if legacy.exists():
        for path in sorted(legacy.iterdir(), key=lambda item: item.name):
            if path.is_dir() and (path / "skill.yaml").exists():
                name = _validate_name(path.name)
                if name not in canonical_names:
                    _register_skill_candidate(registry, project_root, name)
    return registry


def explain_skill(project_root: Path, name: str) -> RegistryEntry:
    """Return registry provenance for one skill without changing its file format."""

    name = _validate_name(name)
    registry = ExtensionRegistry()
    _register_skill_candidate(registry, project_root, name)
    entry = registry.resolve(ExtensionKind.SKILL, name)
    if entry is None:
        raise SkillError(f"Skill '{name}' must contain skill.yaml and SKILL.md")
    return entry


def _load_skill_entry(project_root: Path, entry: RegistryEntry) -> Skill:
    name = entry.manifest.metadata.id
    format_name = str(entry.manifest.spec.get("format") or "")
    if format_name == _CANONICAL_FORMAT:
        return _load_canonical(project_root, name)
    if format_name == _LEGACY_FORMAT:
        return _load_legacy(project_root, name)
    raise SkillError(
        f"Skill '{name}' has unsupported registry format '{format_name or '<missing>'}'"
    )


def load_skill(project_root: Path, name: str) -> Skill:
    name = _validate_name(name)
    entry = explain_skill(project_root, name)
    return _load_skill_entry(project_root, entry)


def list_skills(project_root: Path) -> list[Skill]:
    project_root = project_root.resolve()
    registry = build_skill_registry(project_root)
    return [
        _load_skill_entry(project_root, entry)
        for entry in registry.list_resolved(ExtensionKind.SKILL)
    ]


def compose_skills(
    project_root: Path, names: tuple[str, ...], capability: Capability
) -> str:
    sections: list[str] = []
    for name in names:
        skill = load_skill(project_root, name)
        if skill.capabilities and capability not in skill.capabilities:
            continue
        sections.append(f"## Skill: {skill.name}\n{skill.instructions}")
    return "\n\n".join(sections)


def validate_skills(project_root: Path) -> list[str]:
    return [skill.name for skill in list_skills(project_root)]
