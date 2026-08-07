from __future__ import annotations

from pathlib import Path

from sdai.agent_platform.models import Capability, Skill
from sdai.config import load_yaml


class SkillError(RuntimeError):
    pass


def _capabilities(values: object) -> tuple[Capability, ...]:
    if not isinstance(values, list):
        raise SkillError("skill capabilities must be a list")
    try:
        return tuple(Capability(str(value)) for value in values)
    except ValueError as exc:
        raise SkillError(str(exc)) from exc


def load_skill(project_root: Path, name: str) -> Skill:
    root = project_root / ".sdai" / "skills" / name
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


def list_skills(project_root: Path) -> list[Skill]:
    root = project_root / ".sdai" / "skills"
    if not root.exists():
        return []
    skills: list[Skill] = []
    for path in sorted(root.iterdir()):
        if path.is_dir() and (path / "skill.yaml").exists():
            skills.append(load_skill(project_root, path.name))
    return skills


def compose_skills(project_root: Path, names: tuple[str, ...], capability: Capability) -> str:
    sections: list[str] = []
    for name in names:
        skill = load_skill(project_root, name)
        if skill.capabilities and capability not in skill.capabilities:
            continue
        sections.append(f"## Skill: {skill.name}\n{skill.instructions}")
    return "\n\n".join(sections)
