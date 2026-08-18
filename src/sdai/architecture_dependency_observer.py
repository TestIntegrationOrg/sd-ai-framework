from __future__ import annotations

import ast
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path, PurePosixPath
import re
from typing import Iterable

from sdai.architecture_drift import (
    ApprovedArchitecture,
    ArchitectureComponent,
    ArchitectureDriftError,
    ArchitectureFactKind,
    ArchitectureObservation,
    ObservedArchitectureFact,
)
from sdai.path_safety import PathSafetyError, ensure_within_project
from sdai.trace_graph import TraceProvenance


DEPENDENCY_OBSERVER_ID = "repository-dependencies"
DEPENDENCY_MAX_FILE_BYTES = 4 * 1024 * 1024
DEPENDENCY_MAX_SOURCE_FILES = 100_000
DEPENDENCY_MAX_IMPORTS_PER_FILE = 10_000

_SOURCE_SUFFIXES = frozenset(
    {
        ".py",
        ".java",
        ".kt",
        ".kts",
        ".cs",
        ".fs",
        ".fsx",
        ".js",
        ".jsx",
        ".mjs",
        ".cjs",
        ".ts",
        ".tsx",
        ".go",
        ".ps1",
        ".psm1",
    }
)
_JS_SUFFIXES = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")
_PYTHON_SUFFIXES = (".py",)
_POWERSHELL_SUFFIXES = (".ps1", ".psm1")


@dataclass(frozen=True, slots=True)
class _ImportReference:
    specifier: str
    line: int
    local: bool
    relative_level: int = 0


class _ComponentMapper:
    def __init__(self, project_root: Path, components: tuple[ArchitectureComponent, ...]) -> None:
        self.root = project_root.resolve()
        self.components = components
        self._roots: list[tuple[PurePosixPath, str]] = []
        self._prefixes: list[tuple[str, str]] = []
        for component in components:
            for root in component.roots:
                self._roots.append((PurePosixPath(root), component.component_id))
            for prefix in component.module_prefixes:
                self._prefixes.append((prefix, component.component_id))
        self._roots.sort(key=lambda item: (-len(item[0].parts), item[0].as_posix(), item[1]))
        self._prefixes.sort(key=lambda item: (-len(item[0]), item[0], item[1]))

    def owner_for_relative_path(self, relative: PurePosixPath) -> str | None:
        matches: list[tuple[int, str]] = []
        for root, component_id in self._roots:
            if relative == root or root in relative.parents:
                matches.append((len(root.parts), component_id))
        if not matches:
            return None
        best = max(length for length, _ in matches)
        owners = sorted({component_id for length, component_id in matches if length == best})
        if len(owners) != 1:
            raise _fail(
                "SDAI-ARCH-DEPENDENCY-002",
                f"repository path {relative.as_posix()!r} has ambiguous component ownership: {', '.join(owners)}",
            )
        return owners[0]

    def owner_for_module(self, specifier: str) -> str | None:
        matches: list[tuple[int, str]] = []
        for prefix, component_id in self._prefixes:
            if _module_prefix_matches(specifier, prefix):
                matches.append((len(prefix), component_id))
        if not matches:
            return None
        best = max(length for length, _ in matches)
        owners = sorted({component_id for length, component_id in matches if length == best})
        if len(owners) != 1:
            raise _fail(
                "SDAI-ARCH-DEPENDENCY-002",
                f"module {specifier!r} has ambiguous component ownership: {', '.join(owners)}",
            )
        return owners[0]

    def existing_component_roots(self) -> tuple[Path, ...]:
        paths: set[Path] = set()
        for root, _ in self._roots:
            candidate = self.root.joinpath(*root.parts)
            try:
                safe = ensure_within_project(self.root, candidate, label="architecture component root")
            except PathSafetyError as exc:
                raise _fail(
                    "SDAI-ARCH-DEPENDENCY-001",
                    f"component root escapes project workspace: {root.as_posix()}",
                ) from exc
            if not safe.exists():
                continue
            _reject_symlink_chain(self.root, safe, label="component root")
            if not safe.is_dir():
                raise _fail(
                    "SDAI-ARCH-DEPENDENCY-001",
                    f"component root must be a directory: {root.as_posix()}",
                )
            paths.add(safe)
        return tuple(sorted(paths, key=lambda item: item.relative_to(self.root).as_posix()))


def _fail(code: str, message: str) -> ArchitectureDriftError:
    return ArchitectureDriftError(f"{code}: {message}")


def _module_prefix_matches(specifier: str, prefix: str) -> bool:
    if specifier == prefix:
        return True
    return any(specifier.startswith(prefix + delimiter) for delimiter in (".", "/", ":"))


def _reject_symlink_chain(root: Path, path: Path, *, label: str) -> None:
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise _fail("SDAI-ARCH-DEPENDENCY-001", f"{label} escapes project root") from exc
    current = root
    for part in relative.parts:
        current = current / part
        if current.exists() and current.is_symlink():
            raise _fail(
                "SDAI-ARCH-DEPENDENCY-001",
                f"{label} must not traverse symbolic links: {relative.as_posix()}",
            )


def _read_source(root: Path, path: Path) -> str:
    _reject_symlink_chain(root, path, label="dependency source")
    if path.is_symlink() or not path.is_file():
        raise _fail(
            "SDAI-ARCH-DEPENDENCY-001",
            f"dependency source must be a regular non-symlink file: {path.relative_to(root).as_posix()}",
        )
    try:
        with path.open("rb") as stream:
            data = stream.read(DEPENDENCY_MAX_FILE_BYTES + 1)
    except OSError as exc:
        raise _fail(
            "SDAI-ARCH-DEPENDENCY-001",
            f"unable to read dependency source {path.relative_to(root).as_posix()}: {exc}",
        ) from exc
    if len(data) > DEPENDENCY_MAX_FILE_BYTES:
        raise _fail(
            "SDAI-ARCH-DEPENDENCY-001",
            f"dependency source exceeds {DEPENDENCY_MAX_FILE_BYTES} bytes: {path.relative_to(root).as_posix()}",
        )
    try:
        return data.decode("utf-8-sig", errors="strict").replace("\r\n", "\n").replace("\r", "\n")
    except UnicodeDecodeError as exc:
        raise _fail(
            "SDAI-ARCH-DEPENDENCY-001",
            f"dependency source must be valid UTF-8: {path.relative_to(root).as_posix()}",
        ) from exc


def _source_files(mapper: _ComponentMapper) -> tuple[Path, ...]:
    root = mapper.root
    files: set[Path] = set()
    for component_root in mapper.existing_component_roots():
        for path in component_root.rglob("*"):
            if path.is_symlink():
                if path.suffix.casefold() in _SOURCE_SUFFIXES:
                    raise _fail(
                        "SDAI-ARCH-DEPENDENCY-001",
                        f"dependency source must not be a symbolic link: {path.relative_to(root).as_posix()}",
                    )
                continue
            if path.is_file() and path.suffix.casefold() in _SOURCE_SUFFIXES:
                relative = PurePosixPath(path.relative_to(root).as_posix())
                if mapper.owner_for_relative_path(relative) is not None:
                    files.add(path)
            if len(files) > DEPENDENCY_MAX_SOURCE_FILES:
                raise _fail(
                    "SDAI-ARCH-DEPENDENCY-001",
                    f"dependency observation exceeds {DEPENDENCY_MAX_SOURCE_FILES} supported source files",
                )
    return tuple(sorted(files, key=lambda item: item.relative_to(root).as_posix()))


def _python_imports(text: str, *, source: str) -> tuple[_ImportReference, ...]:
    try:
        tree = ast.parse(text, filename=source, mode="exec")
    except SyntaxError as exc:
        raise _fail(
            "SDAI-ARCH-DEPENDENCY-003",
            f"unable to parse Python dependency source {source}:{exc.lineno or 1}: {exc.msg}",
        ) from exc
    result: list[_ImportReference] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                result.append(_ImportReference(alias.name, node.lineno, False))
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level:
                result.append(_ImportReference(module, node.lineno, True, node.level))
            elif module:
                result.append(_ImportReference(module, node.lineno, False))
    return _bounded_imports(result, source=source)


def _strip_c_comments(text: str) -> str:
    """Remove C-family comments while preserving newlines and string contents."""
    output: list[str] = []
    index = 0
    state = "code"
    quote = ""
    while index < len(text):
        char = text[index]
        nxt = text[index + 1] if index + 1 < len(text) else ""
        if state == "code":
            if char == "/" and nxt == "/":
                output.extend((" ", " "))
                index += 2
                state = "line-comment"
                continue
            if char == "/" and nxt == "*":
                output.extend((" ", " "))
                index += 2
                state = "block-comment"
                continue
            if char in {"'", '"', "`"}:
                quote = char
                state = "string"
            output.append(char)
            index += 1
            continue
        if state == "line-comment":
            if char == "\n":
                output.append("\n")
                state = "code"
            else:
                output.append(" ")
            index += 1
            continue
        if state == "block-comment":
            if char == "*" and nxt == "/":
                output.extend((" ", " "))
                index += 2
                state = "code"
                continue
            output.append("\n" if char == "\n" else " ")
            index += 1
            continue
        output.append(char)
        if char == "\\" and index + 1 < len(text):
            output.append(text[index + 1])
            index += 2
            continue
        if char == quote:
            state = "code"
        index += 1
    return "".join(output)


def _line_imports(text: str, pattern: re.Pattern[str], *, source: str) -> tuple[_ImportReference, ...]:
    cleaned = _strip_c_comments(text)
    result: list[_ImportReference] = []
    for line_number, line in enumerate(cleaned.splitlines(), start=1):
        match = pattern.match(line)
        if match is not None:
            result.append(_ImportReference(match.group("module"), line_number, False))
    return _bounded_imports(result, source=source)


_JAVA_IMPORT = re.compile(r"^\s*import\s+(?:static\s+)?(?P<module>[A-Za-z_$][A-Za-z0-9_$.]*(?:\.\*)?)\s*;?")
_KOTLIN_IMPORT = re.compile(r"^\s*import\s+(?P<module>[A-Za-z_$][A-Za-z0-9_$.]*(?:\.\*)?)(?:\s+as\s+[A-Za-z_$][A-Za-z0-9_$]*)?\s*;?")
_CSHARP_IMPORT = re.compile(r"^\s*using\s+(?:static\s+)?(?:[A-Za-z_][A-Za-z0-9_]*\s*=\s*)?(?P<module>[A-Za-z_][A-Za-z0-9_.]*)\s*;")
_FSHARP_IMPORT = re.compile(r"^\s*open\s+(?P<module>[A-Za-z_][A-Za-z0-9_.]*)\s*$")


def _javascript_imports(text: str, *, source: str) -> tuple[_ImportReference, ...]:
    cleaned = _strip_c_comments(text)
    result: list[_ImportReference] = []
    import_line = re.compile(
        r"^\s*(?:import|export)\b(?:[^\n]*?\bfrom\s*)?[\"'](?P<module>[^\"']+)[\"']",
    )
    bare_import = re.compile(r"^\s*import\s*[\"'](?P<module>[^\"']+)[\"']")
    require_call = re.compile(r"\brequire\s*\(\s*[\"'](?P<module>[^\"']+)[\"']\s*\)")
    dynamic_import = re.compile(r"\bimport\s*\(\s*[\"'](?P<module>[^\"']+)[\"']\s*\)")
    for line_number, line in enumerate(cleaned.splitlines(), start=1):
        matched: set[tuple[int, str]] = set()
        for pattern in (import_line, bare_import):
            match = pattern.match(line)
            if match is not None:
                specifier = match.group("module")
                matched.add((match.start(), specifier))
        for pattern in (require_call, dynamic_import):
            for match in pattern.finditer(line):
                if _position_outside_string(line, match.start()):
                    matched.add((match.start(), match.group("module")))
        for _, specifier in sorted(matched):
            result.append(_ImportReference(specifier, line_number, _is_local_specifier(specifier)))
    return _bounded_imports(result, source=source)


def _position_outside_string(line: str, position: int) -> bool:
    quote: str | None = None
    escaped = False
    for char in line[:position]:
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if quote is None and char in {"'", '"', "`"}:
            quote = char
        elif quote == char:
            quote = None
    return quote is None


def _go_imports(text: str, *, source: str) -> tuple[_ImportReference, ...]:
    cleaned = _strip_c_comments(text)
    result: list[_ImportReference] = []
    in_block = False
    quoted = re.compile(r"(?:[A-Za-z_.][A-Za-z0-9_.]*\s+)?[\"`](?P<module>[^\"`]+)[\"`]")
    for line_number, line in enumerate(cleaned.splitlines(), start=1):
        stripped = line.strip()
        if not in_block:
            if re.match(r"^import\s*\($", stripped):
                in_block = True
                continue
            if not stripped.startswith("import "):
                continue
            match = quoted.search(stripped[len("import ") :])
            if match is not None:
                result.append(_ImportReference(match.group("module"), line_number, False))
            continue
        if stripped == ")":
            in_block = False
            continue
        match = quoted.search(stripped)
        if match is not None:
            result.append(_ImportReference(match.group("module"), line_number, False))
    if in_block:
        raise _fail("SDAI-ARCH-DEPENDENCY-003", f"unterminated Go import block in {source}")
    return _bounded_imports(result, source=source)


def _strip_powershell_comment(line: str) -> str:
    quote: str | None = None
    index = 0
    while index < len(line):
        char = line[index]
        if quote is None and char in {"'", '"'}:
            quote = char
        elif quote == char:
            quote = None
        elif quote is None and char == "#":
            return line[:index]
        elif char == "`" and index + 1 < len(line):
            index += 1
        index += 1
    return line


def _powershell_imports(text: str, *, source: str) -> tuple[_ImportReference, ...]:
    result: list[_ImportReference] = []
    import_module = re.compile(
        r"^\s*Import-Module\s+(?:-Name\s+)?(?P<module>[^\s;]+)",
        re.IGNORECASE,
    )
    dot_source = re.compile(r"^\s*\.\s+(?P<module>[^\s;]+)")
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = _strip_powershell_comment(raw_line)
        for pattern in (import_module, dot_source):
            match = pattern.match(line)
            if match is None:
                continue
            specifier = _unquote(match.group("module"))
            if not specifier or specifier.startswith("$") or "$" in specifier:
                raise _fail(
                    "SDAI-ARCH-DEPENDENCY-003",
                    f"dynamic PowerShell module path cannot be resolved deterministically at {source}:{line_number}",
                )
            specifier = specifier.replace("\\", "/")
            result.append(_ImportReference(specifier, line_number, _is_local_specifier(specifier)))
            break
    return _bounded_imports(result, source=source)


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _bounded_imports(values: Iterable[_ImportReference], *, source: str) -> tuple[_ImportReference, ...]:
    result = tuple(values)
    if len(result) > DEPENDENCY_MAX_IMPORTS_PER_FILE:
        raise _fail(
            "SDAI-ARCH-DEPENDENCY-003",
            f"dependency source exceeds {DEPENDENCY_MAX_IMPORTS_PER_FILE} imports: {source}",
        )
    return result


def _imports_for(path: Path, text: str, *, source: str) -> tuple[_ImportReference, ...]:
    suffix = path.suffix.casefold()
    if suffix == ".py":
        return _python_imports(text, source=source)
    if suffix == ".java":
        return _line_imports(text, _JAVA_IMPORT, source=source)
    if suffix in {".kt", ".kts"}:
        return _line_imports(text, _KOTLIN_IMPORT, source=source)
    if suffix == ".cs":
        return _line_imports(text, _CSHARP_IMPORT, source=source)
    if suffix in {".fs", ".fsx"}:
        return _line_imports(text, _FSHARP_IMPORT, source=source)
    if suffix in _JS_SUFFIXES:
        return _javascript_imports(text, source=source)
    if suffix == ".go":
        return _go_imports(text, source=source)
    if suffix in _POWERSHELL_SUFFIXES:
        return _powershell_imports(text, source=source)
    return ()


def _is_local_specifier(specifier: str) -> bool:
    normalized = specifier.replace("\\", "/")
    return normalized.startswith("./") or normalized.startswith("../") or normalized in {".", ".."}


def _normalize_local_candidate(root: Path, importer: Path, specifier: str) -> Path:
    normalized = specifier.replace("\\", "/")
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
        raise _fail(
            "SDAI-ARCH-DEPENDENCY-004",
            f"local dependency specifier must not be absolute: {specifier!r}",
        )
    candidate = importer.parent.joinpath(*PurePosixPath(normalized).parts)
    try:
        return ensure_within_project(root, candidate, label="local dependency import")
    except PathSafetyError as exc:
        raise _fail(
            "SDAI-ARCH-DEPENDENCY-004",
            f"local dependency import escapes project root: {specifier!r}",
        ) from exc


def _resolve_file_candidates(
    root: Path,
    importer: Path,
    specifier: str,
    *,
    suffixes: tuple[str, ...],
) -> tuple[Path, ...]:
    base = _normalize_local_candidate(root, importer, specifier)
    candidates: list[Path] = [base]
    if base.suffix == "":
        candidates.extend(Path(str(base) + suffix) for suffix in suffixes)
        candidates.extend(base / ("index" + suffix) for suffix in suffixes)
    result: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        if candidate in seen or not candidate.exists():
            continue
        seen.add(candidate)
        _reject_symlink_chain(root, candidate, label="local dependency target")
        if candidate.is_file() or candidate.is_dir():
            result.append(candidate)
    return tuple(sorted(result, key=lambda item: item.relative_to(root).as_posix()))


def _resolve_python_relative(
    root: Path,
    importer: Path,
    reference: _ImportReference,
) -> tuple[Path, ...]:
    base = importer.parent
    for _ in range(max(reference.relative_level - 1, 0)):
        base = base.parent
    module_parts = tuple(part for part in reference.specifier.split(".") if part)
    target = base.joinpath(*module_parts) if module_parts else base
    try:
        safe = ensure_within_project(root, target, label="relative Python import")
    except PathSafetyError as exc:
        raise _fail(
            "SDAI-ARCH-DEPENDENCY-004",
            f"relative Python import escapes project root from {importer.relative_to(root).as_posix()}",
        ) from exc
    candidates: list[Path] = [safe]
    if safe.suffix == "":
        candidates.extend((Path(str(safe) + ".py"), safe / "__init__.py"))
    result: list[Path] = []
    for candidate in candidates:
        if candidate.exists():
            _reject_symlink_chain(root, candidate, label="relative Python import target")
            if candidate.is_file() or candidate.is_dir():
                result.append(candidate)
    unique = {item for item in result}
    return tuple(sorted(unique, key=lambda item: item.relative_to(root).as_posix()))


def _repository_module_candidates(
    mapper: _ComponentMapper,
    specifier: str,
    *,
    language: str,
) -> tuple[Path, ...]:
    parts = tuple(part for part in re.split(r"[./]", specifier.rstrip(".*")) if part)
    if not parts:
        return ()
    suffixes: tuple[str, ...]
    if language == "python":
        suffixes = _PYTHON_SUFFIXES
    else:
        return ()
    result: set[Path] = set()
    for root_path in mapper.existing_component_roots():
        base = root_path.joinpath(*parts)
        candidates = [Path(str(base) + suffix) for suffix in suffixes]
        candidates.append(base / "__init__.py")
        if base.is_dir():
            candidates.append(base)
        for candidate in candidates:
            if candidate.exists():
                _reject_symlink_chain(mapper.root, candidate, label="repository module target")
                result.add(candidate)
    return tuple(sorted(result, key=lambda item: item.relative_to(mapper.root).as_posix()))


def _owner_from_candidates(
    mapper: _ComponentMapper,
    candidates: tuple[Path, ...],
    *,
    specifier: str,
    source: str,
    line: int,
) -> str:
    if not candidates:
        raise _fail(
            "SDAI-ARCH-DEPENDENCY-004",
            f"local dependency cannot be resolved at {source}:{line}: {specifier!r}",
        )
    owners: set[str] = set()
    for candidate in candidates:
        relative = PurePosixPath(candidate.relative_to(mapper.root).as_posix())
        owner = mapper.owner_for_relative_path(relative)
        if owner is None:
            raise _fail(
                "SDAI-ARCH-DEPENDENCY-004",
                f"local dependency target is outside declared component roots at {source}:{line}: {specifier!r}",
            )
        owners.add(owner)
    if len(owners) != 1:
        raise _fail(
            "SDAI-ARCH-DEPENDENCY-004",
            f"local dependency has ambiguous component ownership at {source}:{line}: {specifier!r}",
        )
    return next(iter(owners))


def _external_target(specifier: str) -> str:
    base = specifier.rstrip(".*").split("/")[0] if not specifier.startswith("@") else "/".join(specifier.split("/")[:2])
    base = base.split(".")[0] if "." in base and "/" not in specifier else base
    slug = re.sub(r"[^A-Za-z0-9._@+\-]+", "-", base).strip("-") or "dependency"
    slug = slug[:80]
    digest = sha256(specifier.encode("utf-8")).hexdigest()[:16]
    return f"external:{slug}:{digest}"


def _language(path: Path) -> str:
    suffix = path.suffix.casefold()
    if suffix == ".py":
        return "python"
    if suffix == ".java":
        return "java"
    if suffix in {".kt", ".kts"}:
        return "kotlin"
    if suffix == ".cs":
        return "dotnet"
    if suffix in {".fs", ".fsx"}:
        return "dotnet"
    if suffix in _JS_SUFFIXES:
        return "javascript"
    if suffix == ".go":
        return "go"
    if suffix in _POWERSHELL_SUFFIXES:
        return "powershell"
    return "unknown"


def _resolve_reference(
    mapper: _ComponentMapper,
    importer: Path,
    reference: _ImportReference,
    *,
    source: str,
) -> str:
    specifier = reference.specifier.strip()
    if not specifier:
        raise _fail("SDAI-ARCH-DEPENDENCY-003", f"empty import specifier at {source}:{reference.line}")
    language = _language(importer)
    if language == "python" and reference.relative_level:
        return _owner_from_candidates(
            mapper,
            _resolve_python_relative(mapper.root, importer, reference),
            specifier=("." * reference.relative_level) + specifier,
            source=source,
            line=reference.line,
        )
    if reference.local:
        suffixes = _JS_SUFFIXES if language == "javascript" else _POWERSHELL_SUFFIXES
        return _owner_from_candidates(
            mapper,
            _resolve_file_candidates(mapper.root, importer, specifier, suffixes=suffixes),
            specifier=specifier,
            source=source,
            line=reference.line,
        )
    owner = mapper.owner_for_module(specifier.rstrip(".*"))
    if owner is not None:
        return owner
    repository_candidates = _repository_module_candidates(mapper, specifier, language=language)
    if repository_candidates:
        return _owner_from_candidates(
            mapper,
            repository_candidates,
            specifier=specifier,
            source=source,
            line=reference.line,
        )
    return _external_target(specifier)


def _aggregate_provenance(values: Iterable[TraceProvenance]) -> tuple[TraceProvenance, ...]:
    by_location: dict[tuple[str, int], TraceProvenance] = {}
    for item in values:
        previous = by_location.get(item.location)
        if previous is not None and previous != item:
            # Multiple specifiers at one source line are still one deterministic source location.
            detail = min(previous.detail or "", item.detail or "") or previous.detail or item.detail
            by_location[item.location] = TraceProvenance(item.source, item.line, detail=detail)
        else:
            by_location[item.location] = item
    return tuple(sorted(by_location.values(), key=lambda item: (item.source, item.line, item.detail or "")))


class DependencyImportObserver:
    """Observe source-level component dependencies without executing repository tooling."""

    observer_id = DEPENDENCY_OBSERVER_ID

    def observe(
        self,
        project_root: Path,
        approved: ApprovedArchitecture,
    ) -> ArchitectureObservation:
        root = project_root.resolve()
        if not isinstance(approved, ApprovedArchitecture):
            raise _fail("SDAI-ARCH-DEPENDENCY-005", "dependency observation requires approved architecture truth")
        mapper = _ComponentMapper(root, approved.topology.components)
        provenance_by_edge: dict[tuple[str, str], list[TraceProvenance]] = {}

        for path in _source_files(mapper):
            relative = PurePosixPath(path.relative_to(root).as_posix())
            source_component = mapper.owner_for_relative_path(relative)
            if source_component is None:
                continue
            source_text = _read_source(root, path)
            source = relative.as_posix()
            for reference in _imports_for(path, source_text, source=source):
                target_component = _resolve_reference(
                    mapper,
                    path,
                    reference,
                    source=source,
                )
                if target_component == source_component:
                    continue
                provenance_by_edge.setdefault((source_component, target_component), []).append(
                    TraceProvenance(
                        source,
                        reference.line,
                        detail=f"{_language(path)} import {reference.specifier}",
                    )
                )

        facts = tuple(
            ObservedArchitectureFact(
                kind=ArchitectureFactKind.DEPENDENCY,
                source=source,
                target=target,
                attributes={},
                provenance=_aggregate_provenance(provenance),
            )
            for (source, target), provenance in sorted(provenance_by_edge.items())
        )
        return ArchitectureObservation(self.observer_id, facts)


__all__ = [
    "DEPENDENCY_OBSERVER_ID",
    "DependencyImportObserver",
]
