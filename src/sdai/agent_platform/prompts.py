from __future__ import annotations

import re
from pathlib import Path

from sdai.path_safety import ensure_within_project
from sdai.text import read_utf8_text


_TOKEN = re.compile(r"{{\s*([a-zA-Z0-9_.-]+)\s*}}")


class PromptError(RuntimeError):
    pass


def render_template(template: str, values: dict[str, str], *, strict: bool = True) -> str:
    missing: set[str] = set()

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in values:
            missing.add(key)
            return match.group(0)
        return values[key]

    rendered = _TOKEN.sub(replace, template)
    if strict and missing:
        raise PromptError(f"Missing prompt variables: {', '.join(sorted(missing))}")
    return rendered


def _prompt_path(project_root: Path, name: str) -> Path:
    project_root = project_root.resolve()
    candidate = Path(name)
    if candidate.is_absolute() or ".." in candidate.parts or candidate.suffix != ".md":
        raise PromptError("prompt name must be a relative .md path inside .sdai/prompts")
    root = ensure_within_project(
        project_root, project_root / ".sdai" / "prompts", label="prompt directory"
    )
    return ensure_within_project(root, root / candidate, label="prompt path")


def load_prompt(project_root: Path, name: str) -> str:
    path = _prompt_path(project_root, name)
    if not path.exists() or not path.is_file():
        raise PromptError(f"Prompt not found: {path}")
    return read_utf8_text(path)


def list_prompts(project_root: Path) -> list[str]:
    project_root = project_root.resolve()
    root = ensure_within_project(
        project_root, project_root / ".sdai" / "prompts", label="prompt directory"
    )
    if not root.exists():
        return []
    result: list[str] = []
    for path in root.glob("*.md"):
        safe = ensure_within_project(root, path, label="prompt path")
        if safe.is_file():
            result.append(safe.name)
    return sorted(result)
