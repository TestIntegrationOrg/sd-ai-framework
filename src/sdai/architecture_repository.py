from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Iterable

from sdai.architecture_drift import ArchitectureComponent, ArchitectureDriftError
from sdai.path_safety import PathSafetyError, ensure_within_project


class ArchitectureRepositoryError(ArchitectureDriftError):
    """Raised when repository ownership or safe source access is ambiguous/unsafe."""


def _fail(code: str, message: str) -> ArchitectureRepositoryError:
    return ArchitectureRepositoryError(f"{code}: {message}")


def module_prefix_matches(specifier: str, prefix: str) -> bool:
    if specifier == prefix:
        return True
    return any(specifier.startswith(prefix + delimiter) for delimiter in (".", "/", ":"))


def reject_repository_symlink_chain(root: Path, path: Path, *, label: str) -> None:
    root = root.resolve()
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise _fail("SDAI-ARCH-REPOSITORY-001", f"{label} escapes the project root") from exc
    current = root
    for part in relative.parts:
        current = current / part
        if current.exists() and current.is_symlink():
            raise _fail(
                "SDAI-ARCH-REPOSITORY-001",
                f"{label} must not traverse symbolic links: {relative.as_posix()}",
            )


class ArchitectureRepositoryIndex:
    """Resolve repository paths/modules to approved components with stable longest-match rules."""

    def __init__(self, project_root: Path, components: Iterable[ArchitectureComponent]) -> None:
        self.root = project_root.resolve()
        self._roots: list[tuple[PurePosixPath, str]] = []
        self._prefixes: list[tuple[str, str]] = []
        component_ids: set[str] = set()
        for component in components:
            if not isinstance(component, ArchitectureComponent):
                raise _fail("SDAI-ARCH-REPOSITORY-002", "repository index requires validated architecture components")
            component_ids.add(component.component_id)
            for component_root in component.roots:
                self._roots.append((PurePosixPath(component_root), component.component_id))
            for prefix in component.module_prefixes:
                self._prefixes.append((prefix, component.component_id))
        self._component_ids = frozenset(component_ids)
        self._roots.sort(
            key=lambda item: (
                -len(item[0].parts),
                item[0].as_posix().casefold(),
                item[0].as_posix(),
                item[1],
            )
        )
        self._prefixes.sort(
            key=lambda item: (-len(item[0]), item[0].casefold(), item[0], item[1])
        )

    @property
    def component_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._component_ids))

    def owner_for_relative_path(self, relative: PurePosixPath | str) -> str | None:
        path = relative if isinstance(relative, PurePosixPath) else PurePosixPath(relative)
        matches: list[tuple[int, str]] = []
        for component_root, component_id in self._roots:
            if path == component_root or component_root in path.parents:
                matches.append((len(component_root.parts), component_id))
        if not matches:
            return None
        best = max(length for length, _ in matches)
        owners = sorted({owner for length, owner in matches if length == best})
        if len(owners) != 1:
            raise _fail(
                "SDAI-ARCH-REPOSITORY-002",
                f"repository path {path.as_posix()!r} has ambiguous component ownership: {', '.join(owners)}",
            )
        return owners[0]

    def owner_for_module(self, specifier: str) -> str | None:
        matches: list[tuple[int, str]] = []
        for prefix, component_id in self._prefixes:
            if module_prefix_matches(specifier, prefix):
                matches.append((len(prefix), component_id))
        if not matches:
            return None
        best = max(length for length, _ in matches)
        owners = sorted({owner for length, owner in matches if length == best})
        if len(owners) != 1:
            raise _fail(
                "SDAI-ARCH-REPOSITORY-002",
                f"module {specifier!r} has ambiguous component ownership: {', '.join(owners)}",
            )
        return owners[0]

    def existing_component_roots(self) -> tuple[Path, ...]:
        roots: set[Path] = set()
        for component_root, _ in self._roots:
            candidate = self.root.joinpath(*component_root.parts)
            try:
                safe = ensure_within_project(self.root, candidate, label="architecture component root")
            except PathSafetyError as exc:
                raise _fail(
                    "SDAI-ARCH-REPOSITORY-001",
                    f"component root escapes project workspace: {component_root.as_posix()}",
                ) from exc
            if not safe.exists():
                continue
            reject_repository_symlink_chain(self.root, safe, label="component root")
            if not safe.is_dir():
                raise _fail(
                    "SDAI-ARCH-REPOSITORY-001",
                    f"component root must be a directory: {component_root.as_posix()}",
                )
            roots.add(safe)
        return tuple(sorted(roots, key=lambda item: item.relative_to(self.root).as_posix()))

    def source_files(
        self,
        suffixes: Iterable[str],
        *,
        maximum: int,
    ) -> tuple[Path, ...]:
        allowed = frozenset(item.casefold() for item in suffixes)
        files: set[Path] = set()
        for component_root in self.existing_component_roots():
            for path in component_root.rglob("*"):
                if path.is_symlink():
                    if path.suffix.casefold() in allowed:
                        raise _fail(
                            "SDAI-ARCH-REPOSITORY-001",
                            f"architecture source must not be a symbolic link: {path.relative_to(self.root).as_posix()}",
                        )
                    continue
                if path.is_file() and path.suffix.casefold() in allowed:
                    relative = PurePosixPath(path.relative_to(self.root).as_posix())
                    if self.owner_for_relative_path(relative) is not None:
                        files.add(path)
                if len(files) > maximum:
                    raise _fail(
                        "SDAI-ARCH-REPOSITORY-003",
                        f"architecture source observation exceeds the {maximum}-file limit",
                    )
        return tuple(sorted(files, key=lambda item: item.relative_to(self.root).as_posix()))

    def read_utf8(self, path: Path, *, maximum_bytes: int, label: str) -> str:
        reject_repository_symlink_chain(self.root, path, label=label)
        if path.is_symlink() or not path.is_file():
            raise _fail(
                "SDAI-ARCH-REPOSITORY-001",
                f"{label} must be a regular non-symlink file: {path.relative_to(self.root).as_posix()}",
            )
        try:
            with path.open("rb") as stream:
                data = stream.read(maximum_bytes + 1)
        except OSError as exc:
            raise _fail(
                "SDAI-ARCH-REPOSITORY-001",
                f"unable to read {label} {path.relative_to(self.root).as_posix()}: {exc}",
            ) from exc
        if len(data) > maximum_bytes:
            raise _fail(
                "SDAI-ARCH-REPOSITORY-003",
                f"{label} exceeds the {maximum_bytes}-byte limit: {path.relative_to(self.root).as_posix()}",
            )
        try:
            return data.decode("utf-8-sig", errors="strict").replace("\r\n", "\n").replace("\r", "\n")
        except UnicodeDecodeError as exc:
            raise _fail(
                "SDAI-ARCH-REPOSITORY-001",
                f"{label} must be valid UTF-8: {path.relative_to(self.root).as_posix()}",
            ) from exc

    def safe_local_path(self, importer: Path, specifier: str, *, label: str) -> Path:
        normalized = specifier.replace("\\", "/")
        if normalized.startswith("/"):
            raise _fail("SDAI-ARCH-REPOSITORY-001", f"{label} must not be absolute: {specifier!r}")
        candidate = importer.parent.joinpath(*PurePosixPath(normalized).parts)
        try:
            safe = ensure_within_project(self.root, candidate, label=label)
        except PathSafetyError as exc:
            raise _fail(
                "SDAI-ARCH-REPOSITORY-001",
                f"{label} escapes project root: {specifier!r}",
            ) from exc
        return safe.resolve(strict=False)


__all__ = [
    "ArchitectureRepositoryError",
    "ArchitectureRepositoryIndex",
    "module_prefix_matches",
    "reject_repository_symlink_chain",
]
