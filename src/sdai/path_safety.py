from __future__ import annotations

from pathlib import Path


class PathSafetyError(RuntimeError):
    pass


def ensure_within_project(project_root: Path, path: Path, *, label: str) -> Path:
    """Reject paths whose resolved location escapes the project root.

    ``resolve(strict=False)`` follows existing symlink components while allowing the
    final path to be created later, so it protects both reads and generated writes.
    """
    root = project_root.resolve()
    resolved = path.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise PathSafetyError(f"{label} must stay inside the project workspace") from exc
    return path
