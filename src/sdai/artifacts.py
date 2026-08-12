from __future__ import annotations

from pathlib import Path

from sdai.text import read_utf8_text, write_utf8_text


def write_text(path: Path, content: str, *, overwrite: bool = True) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not overwrite:
        raise FileExistsError(path)
    write_utf8_text(path, content.rstrip() + "\n")
    return path


def read_text(path: Path) -> str:
    if not path.exists():
        raise FileNotFoundError(f"Required artifact not found: {path}")
    return read_utf8_text(path)
