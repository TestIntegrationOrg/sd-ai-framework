from __future__ import annotations

from pathlib import Path


def write_text(path: Path, content: str, *, overwrite: bool = True) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not overwrite:
        raise FileExistsError(path)
    path.write_text(content.rstrip() + "\n", encoding="utf-8")
    return path


def read_text(path: Path) -> str:
    if not path.exists():
        raise FileNotFoundError(f"Required artifact not found: {path}")
    return path.read_text(encoding="utf-8")
