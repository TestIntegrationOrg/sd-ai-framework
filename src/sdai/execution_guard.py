from __future__ import annotations

from dataclasses import dataclass, field
import fnmatch
from pathlib import Path

from sdai.path_safety import ensure_within_project


class ProtectedPathViolation(RuntimeError):
    pass


def _relative(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _matches(relative: str, patterns: tuple[str, ...]) -> bool:
    return any(
        fnmatch.fnmatchcase(relative, pattern) or _static_prefix(pattern) == relative
        for pattern in patterns
    )


def _static_prefix(pattern: str) -> str:
    parts: list[str] = []
    for part in Path(pattern).parts:
        if any(char in part for char in "*?["):
            break
        parts.append(part)
    return Path(*parts).as_posix() if parts else "."


@dataclass
class WorkspaceMutationGuard:
    """Restore and reject writes to SD-AI protected paths after an external agent run.

    Only the static prefixes of protected patterns are scanned, so the guard does not
    traverse an entire large repository unless policy deliberately protects a root-wide
    wildcard. Framework-owned writes (for example persisted AI output) happen after the
    guard exits.
    """

    project_root: Path
    protected_patterns: tuple[str, ...]
    _before: dict[str, bytes | None] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        self.project_root = self.project_root.resolve()

    def _scan_roots(self) -> list[Path]:
        roots: list[Path] = []
        seen: set[str] = set()
        for pattern in self.protected_patterns:
            prefix = _static_prefix(pattern)
            if prefix == ".":
                root = self.project_root
            else:
                # Walk the static prefix lexically. If an agent replaced any ancestor
                # with a symlink, scan that symlink itself instead of following it.
                root = self.project_root
                for part in Path(prefix).parts:
                    root = root / part
                    if root.is_symlink():
                        break
                else:
                    root = ensure_within_project(
                        self.project_root, root, label=f"protected pattern '{pattern}'"
                    )
            key = root.relative_to(self.project_root).as_posix() if root != self.project_root else "."
            if key not in seen:
                seen.add(key)
                roots.append(root)
        return roots

    def _scan(self, *, allow_symlink: bool) -> dict[str, bytes | None]:
        result: dict[str, bytes | None] = {}
        visited: set[str] = set()
        for scan_root in self._scan_roots():
            if not scan_root.exists() and not scan_root.is_symlink():
                continue
            candidates = [scan_root]
            if scan_root.is_dir() and not scan_root.is_symlink():
                candidates.extend(scan_root.rglob("*"))
            for path in candidates:
                relative = _relative(self.project_root, path)
                is_scan_root_symlink = path == scan_root and path.is_symlink()
                if relative in visited or (
                    not is_scan_root_symlink and not _matches(relative, self.protected_patterns)
                ):
                    continue
                visited.add(relative)
                if path.is_symlink():
                    if not allow_symlink:
                        raise ProtectedPathViolation(
                            f"Protected path '{relative}' must not be a symlink"
                        )
                    result[relative] = None
                    continue
                if not path.is_file():
                    continue
                safe = ensure_within_project(
                    self.project_root, path, label=f"protected path '{relative}'"
                )
                result[relative] = safe.read_bytes()
        return result

    def __enter__(self) -> "WorkspaceMutationGuard":
        self._before = self._scan(allow_symlink=False)
        return self

    def _restore(self, after: dict[str, bytes | None]) -> list[str]:
        changed = sorted(
            relative
            for relative in set(self._before) | set(after)
            if self._before.get(relative) != after.get(relative)
        )
        if not changed:
            return []

        # Remove new protected files/symlinks first.
        for relative in changed:
            if relative not in self._before:
                # `relative` came from a project-root traversal. Unlink a newly-created
                # symlink before resolving it so an agent cannot make containment checks
                # follow a protected directory outside the repository.
                raw_path = self.project_root / relative
                if raw_path.is_symlink():
                    raw_path.unlink()
                    continue
                path = ensure_within_project(
                    self.project_root,
                    raw_path,
                    label=f"protected path '{relative}'",
                )
                if path.exists():
                    if path.is_dir():
                        # Files inside are removed individually; leave an empty directory.
                        continue
                    path.unlink()

        # Restore deleted/modified protected files byte-for-byte. Protected symlinks
        # created by the agent are unlinked before bytes are restored, preventing an
        # attacker-controlled target outside the repository from receiving the write.
        for relative, content in self._before.items():
            if relative not in changed or content is None:
                continue
            raw_path = self.project_root / relative
            current = self.project_root
            for part in Path(relative).parts[:-1]:
                current = current / part
                if current.is_symlink():
                    current.unlink()
                    current.mkdir(parents=True, exist_ok=True)
            if raw_path.is_symlink():
                raw_path.unlink()
            path = ensure_within_project(
                self.project_root,
                raw_path,
                label=f"protected path '{relative}'",
            )
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        return changed

    def __exit__(self, exc_type, exc, tb) -> bool:
        after = self._scan(allow_symlink=True)
        changed = self._restore(after)
        if changed:
            preview = ", ".join(changed[:8])
            suffix = " ..." if len(changed) > 8 else ""
            raise ProtectedPathViolation(
                "External agent modified protected SD-AI/source-of-truth paths; "
                f"changes were restored: {preview}{suffix}"
            )
        return False
