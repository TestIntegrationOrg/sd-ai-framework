from __future__ import annotations

import re
from pathlib import Path


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


def load_prompt(project_root: Path, name: str) -> str:
    path = project_root / ".sdai" / "prompts" / name
    if not path.exists():
        raise PromptError(f"Prompt not found: {path}")
    return path.read_text(encoding="utf-8")


def list_prompts(project_root: Path) -> list[str]:
    root = project_root / ".sdai" / "prompts"
    if not root.exists():
        return []
    return sorted(path.name for path in root.glob("*.md") if path.is_file())
