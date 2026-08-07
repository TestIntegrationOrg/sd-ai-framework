from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from sdai.agent_platform.models import Capability, Skill
from sdai.config import load_yaml


class SkillError(RuntimeError):
    pass


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
    return project_root / ".agents" / "skills" / name


def _legacy_root(project_root: Path, name: str) -> Path:
    return project_root / ".sdai" / "skills" / name


def _load_canonical(project_root: Path, name: str) -> Skill:
    root = _canonical_root(project_root, name)
    path = root / "SKILL.md"
    if not path.exists():
        raise SkillError(f"Canonical skill '{name}' must contain SKILL.md")
    metadata, instructions = _frontmatter(path.read_text(encoding="utf-8"), path)
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
    sidecar = root / "sdai.yaml"
    metadata_sdai = load_yaml(sidecar) if sidecar.exists() else {}
    return Skill(
        name=name,
        description=description,
        capabilities=_capabilities(metadata_sdai.get("capabilities")),
        instructions=instructions,
        root=root,
    )


def _load_legacy(project_root: Path, name: str) -> Skill:
    root = _legacy_root(project_root, name)
    manifest_path = root / "skill.yaml"
    instructions_path = root / "SKILL.md"
    if not manifest_path.exists() or not instructions_path.exists():
        raise SkillError(f"Skill '{name}' must contain skill.yaml and SKILL.md")
    manifest = load_yaml(manifest_path)
    manifest_name = str(manifest.get("name") or name)
    if manifest_name != name:
        raise SkillError(f"Skill directory '{name}' does not match manifest name '{manifest_name}'")
    return Skill(
        name=name,
        description=str(manifest.get("description") or ""),
        capabilities=_capabilities(manifest.get("capabilities") or []),
        instructions=instructions_path.read_text(encoding="utf-8").strip(),
        root=root,
    )


def load_skill(project_root: Path, name: str) -> Skill:
    # v0.5 canonical open-standard skills win over the legacy .sdai/skills format.
    if (_canonical_root(project_root, name) / "SKILL.md").exists():
        return _load_canonical(project_root, name)
    return _load_legacy(project_root, name)


def list_skills(project_root: Path) -> list[Skill]:
    names: set[str] = set()
    canonical = project_root / ".agents" / "skills"
    if canonical.exists():
        names.update(
            path.name
            for path in canonical.iterdir()
            if path.is_dir() and (path / "SKILL.md").exists()
        )
    legacy = project_root / ".sdai" / "skills"
    if legacy.exists():
        names.update(
            path.name
            for path in legacy.iterdir()
            if path.is_dir() and (path / "skill.yaml").exists()
        )
    return [load_skill(project_root, name) for name in sorted(names)]


def compose_skills(project_root: Path, names: tuple[str, ...], capability: Capability) -> str:
    sections: list[str] = []
    for name in names:
        skill = load_skill(project_root, name)
        if skill.capabilities and capability not in skill.capabilities:
            continue
        sections.append(f"## Skill: {skill.name}\n{skill.instructions}")
    return "\n\n".join(sections)


def validate_skills(project_root: Path) -> list[str]:
    """Validate all discovered skills and return their names."""
    return [skill.name for skill in list_skills(project_root)]
