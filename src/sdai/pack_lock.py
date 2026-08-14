from __future__ import annotations

from dataclasses import dataclass
from functools import cmp_to_key
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Iterable, Mapping
import unicodedata

from sdai.pack_manifest import PackManifest, PackManifestError, SemVer, VersionConstraint


PACK_LOCK_API_VERSION = "sdai.pack-lock/v1"


class PackLockError(RuntimeError):
    pass


_HASH_PREFIX = "sha256:"
_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9]*(?:[-_.][a-z0-9]+)*$")
_LOCK_KEYS = frozenset({"apiVersion", "roots", "packages"})
_ENTRY_KEYS = frozenset(
    {
        "id",
        "publisher",
        "version",
        "source",
        "manifestSha256",
        "contentSha256",
        "dependencies",
    }
)


def _fail(code: str, message: str) -> PackLockError:
    return PackLockError(f"{code}: {message}")


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise _fail("SDAI-PACK-LOCK-001", "lock state is not canonical finite JSON") from exc


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _fail("SDAI-PACK-LOCK-001", f"lock JSON contains duplicate key '{key}'")
        result[key] = value
    return result


def _mapping(value: object, *, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise _fail("SDAI-PACK-LOCK-001", f"{label} must be a string-keyed mapping")
    return value


def _keys(value: Mapping[str, object], *, expected: frozenset[str], label: str) -> None:
    unknown = sorted(set(value) - expected)
    missing = sorted(expected - set(value))
    if unknown:
        raise _fail(
            "SDAI-PACK-LOCK-001",
            f"{label} contains unsupported field(s): {', '.join(unknown)}",
        )
    if missing:
        raise _fail(
            "SDAI-PACK-LOCK-001",
            f"{label} is missing required field(s): {', '.join(missing)}",
        )


def _text(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _fail("SDAI-PACK-LOCK-001", f"{label} must be a non-empty string")
    if "\x00" in value:
        raise _fail("SDAI-PACK-LOCK-001", f"{label} must not contain NUL")
    return unicodedata.normalize("NFC", value.strip())


def _identifier(value: object, *, label: str) -> str:
    text = _text(value, label=label)
    if not _IDENTIFIER_RE.fullmatch(text):
        raise _fail(
            "SDAI-PACK-LOCK-001",
            f"{label} '{text}' is not a portable lowercase identifier",
        )
    return text


def _hash(value: object, *, label: str) -> str:
    text = _text(value, label=label)
    if not text.startswith(_HASH_PREFIX):
        raise _fail("SDAI-PACK-LOCK-001", f"{label} must be a SHA-256 digest")
    digest = text[len(_HASH_PREFIX) :]
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise _fail("SDAI-PACK-LOCK-001", f"{label} must be a lowercase SHA-256 digest")
    return text


def _coordinate(publisher: str, pack_id: str) -> str:
    return f"{publisher}/{pack_id}"


def _identity(publisher: str, pack_id: str, version: SemVer) -> str:
    return f"{publisher}/{pack_id}@{version}"


def _parse_identity(value: object, *, label: str) -> tuple[str, str, SemVer]:
    text = _text(value, label=label)
    if text.count("@") != 1 or text.count("/") != 1:
        raise _fail("SDAI-PACK-LOCK-001", f"{label} '{text}' is not publisher/id@version")
    coordinate, version_text = text.rsplit("@", 1)
    publisher_raw, pack_id_raw = coordinate.split("/", 1)
    publisher = _identifier(publisher_raw, label=f"{label} publisher")
    pack_id = _identifier(pack_id_raw, label=f"{label} id")
    try:
        version = SemVer.parse(version_text)
    except PackManifestError as exc:
        raise _fail("SDAI-PACK-LOCK-001", f"{label} '{text}' has an invalid version") from exc
    return publisher, pack_id, version


def _reject_cycles(graph: Mapping[str, tuple[str, ...]]) -> None:
    visiting: list[str] = []
    active: set[str] = set()
    finished: set[str] = set()

    def visit(node: str) -> None:
        if node in finished:
            return
        if node in active:
            start = visiting.index(node)
            cycle = visiting[start:] + [node]
            raise _fail("SDAI-PACK-LOCK-005", "dependency cycle: " + " -> ".join(cycle))
        active.add(node)
        visiting.append(node)
        for dependency in graph.get(node, ()):
            visit(dependency)
        visiting.pop()
        active.remove(node)
        finished.add(node)

    for coordinate in sorted(graph):
        visit(coordinate)


@dataclass(frozen=True)
class PackCandidate:
    manifest: PackManifest
    source: str
    content_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.manifest, PackManifest):
            raise _fail("SDAI-PACK-LOCK-002", "candidate manifest must be a PackManifest")
        object.__setattr__(self, "source", _text(self.source, label="candidate source"))
        object.__setattr__(
            self,
            "content_sha256",
            _hash(self.content_sha256, label="candidate contentSha256"),
        )

    @property
    def coordinate(self) -> str:
        return self.manifest.coordinate

    @property
    def identity(self) -> str:
        return self.manifest.identity

    @property
    def version(self) -> SemVer:
        return self.manifest.version


@dataclass(frozen=True)
class PackLockEntry:
    publisher: str
    id: str
    version: SemVer
    source: str
    manifest_sha256: str
    content_sha256: str
    dependencies: tuple[str, ...]

    @property
    def coordinate(self) -> str:
        return _coordinate(self.publisher, self.id)

    @property
    def identity(self) -> str:
        return _identity(self.publisher, self.id, self.version)

    def as_dict(self) -> dict[str, object]:
        return {
            "contentSha256": self.content_sha256,
            "dependencies": list(self.dependencies),
            "id": self.id,
            "manifestSha256": self.manifest_sha256,
            "publisher": self.publisher,
            "source": self.source,
            "version": str(self.version),
        }

    @classmethod
    def from_dict(cls, value: object) -> "PackLockEntry":
        raw = _mapping(value, label="lock package")
        _keys(raw, expected=_ENTRY_KEYS, label="lock package")
        publisher = _identifier(raw["publisher"], label="lock package publisher")
        pack_id = _identifier(raw["id"], label="lock package id")
        try:
            version = SemVer.parse(raw["version"])
        except PackManifestError as exc:
            raise _fail("SDAI-PACK-LOCK-001", "lock package version is invalid") from exc
        dependencies_raw = raw["dependencies"]
        if not isinstance(dependencies_raw, list):
            raise _fail("SDAI-PACK-LOCK-001", "lock package dependencies must be a list")
        dependencies: list[str] = []
        for index, dependency in enumerate(dependencies_raw):
            dep_publisher, dep_id, dep_version = _parse_identity(
                dependency,
                label=f"lock package dependency[{index}]",
            )
            dependencies.append(_identity(dep_publisher, dep_id, dep_version))
        if len(set(dependencies)) != len(dependencies):
            raise _fail("SDAI-PACK-LOCK-001", "lock package dependencies must not contain duplicates")
        if dependencies != sorted(dependencies):
            raise _fail("SDAI-PACK-LOCK-001", "lock package dependencies must be canonically sorted")
        return cls(
            publisher=publisher,
            id=pack_id,
            version=version,
            source=_text(raw["source"], label="lock package source"),
            manifest_sha256=_hash(raw["manifestSha256"], label="lock package manifestSha256"),
            content_sha256=_hash(raw["contentSha256"], label="lock package contentSha256"),
            dependencies=tuple(dependencies),
        )


@dataclass(frozen=True)
class PackLock:
    roots: tuple[str, ...]
    packages: tuple[PackLockEntry, ...]

    def __post_init__(self) -> None:
        identities = [entry.identity for entry in self.packages]
        coordinates = [entry.coordinate for entry in self.packages]
        if len(set(identities)) != len(identities) or len(set(coordinates)) != len(coordinates):
            raise _fail("SDAI-PACK-LOCK-001", "lock packages must contain one exact version per coordinate")
        if coordinates != sorted(coordinates):
            raise _fail("SDAI-PACK-LOCK-001", "lock packages must be canonically sorted by coordinate")
        if len(set(self.roots)) != len(self.roots) or tuple(sorted(self.roots)) != self.roots:
            raise _fail("SDAI-PACK-LOCK-001", "lock roots must be unique and canonically sorted")
        available = set(identities)
        missing_roots = sorted(set(self.roots) - available)
        if missing_roots:
            raise _fail(
                "SDAI-PACK-LOCK-001",
                "lock roots reference missing package(s): " + ", ".join(missing_roots),
            )
        graph: dict[str, tuple[str, ...]] = {}
        for entry in self.packages:
            missing = sorted(set(entry.dependencies) - available)
            if missing:
                raise _fail(
                    "SDAI-PACK-LOCK-001",
                    f"lock package '{entry.identity}' references missing dependency package(s): "
                    + ", ".join(missing),
                )
            dependency_coordinates = tuple(
                sorted(dependency.rsplit("@", 1)[0] for dependency in entry.dependencies)
            )
            graph[entry.coordinate] = dependency_coordinates
        _reject_cycles(graph)

    def as_dict(self) -> dict[str, object]:
        return {
            "apiVersion": PACK_LOCK_API_VERSION,
            "packages": [entry.as_dict() for entry in self.packages],
            "roots": list(self.roots),
        }

    def to_json(self) -> str:
        return _canonical_json(self.as_dict())

    def to_text(self) -> str:
        return self.to_json() + "\n"

    @property
    def sha256(self) -> str:
        return _HASH_PREFIX + sha256(self.to_json().encode("utf-8")).hexdigest()

    @classmethod
    def from_dict(cls, value: object) -> "PackLock":
        raw = _mapping(value, label="Pack lock")
        _keys(raw, expected=_LOCK_KEYS, label="Pack lock")
        if raw["apiVersion"] != PACK_LOCK_API_VERSION:
            raise _fail(
                "SDAI-PACK-LOCK-001",
                f"unsupported apiVersion '{raw['apiVersion']}', expected '{PACK_LOCK_API_VERSION}'",
            )
        roots_raw = raw["roots"]
        if not isinstance(roots_raw, list):
            raise _fail("SDAI-PACK-LOCK-001", "lock roots must be a list")
        roots: list[str] = []
        for index, root in enumerate(roots_raw):
            publisher, pack_id, version = _parse_identity(root, label=f"lock root[{index}]")
            roots.append(_identity(publisher, pack_id, version))
        packages_raw = raw["packages"]
        if not isinstance(packages_raw, list):
            raise _fail("SDAI-PACK-LOCK-001", "lock packages must be a list")
        return cls(
            roots=tuple(roots),
            packages=tuple(PackLockEntry.from_dict(item) for item in packages_raw),
        )

    @classmethod
    def from_json(cls, value: str) -> "PackLock":
        try:
            raw = json.loads(value, object_pairs_hook=_unique_json_object)
        except json.JSONDecodeError as exc:
            raise _fail("SDAI-PACK-LOCK-001", "Pack lock JSON is malformed") from exc
        return cls.from_dict(raw)


@dataclass(frozen=True)
class PackLockFreshness:
    current_sha256: str
    expected_sha256: str
    outdated: bool
    differences: tuple[str, ...]


def _candidate_compare(left: PackCandidate, right: PackCandidate) -> int:
    precedence = left.version.compare_precedence(right.version)
    if precedence != 0:
        return -precedence
    left_key = (str(left.version), left.source, left.content_sha256, left.manifest.sha256)
    right_key = (str(right.version), right.source, right.content_sha256, right.manifest.sha256)
    if left_key == right_key:
        return 0
    return -1 if left_key < right_key else 1


def _candidate_universe(candidates: Iterable[PackCandidate]) -> dict[str, tuple[PackCandidate, ...]]:
    by_coordinate: dict[str, list[PackCandidate]] = {}
    exact_seen: dict[str, PackCandidate] = {}
    for candidate in candidates:
        previous = exact_seen.get(candidate.identity)
        if previous is not None:
            identical = (
                previous.source == candidate.source
                and previous.content_sha256 == candidate.content_sha256
                and previous.manifest.sha256 == candidate.manifest.sha256
            )
            if identical:
                continue
            raise _fail(
                "SDAI-PACK-LOCK-002",
                f"candidate identity '{candidate.identity}' is ambiguous across sources/hashes",
            )
        exact_seen[candidate.identity] = candidate
        by_coordinate.setdefault(candidate.coordinate, []).append(candidate)
    return {
        coordinate: tuple(sorted(items, key=cmp_to_key(_candidate_compare)))
        for coordinate, items in sorted(by_coordinate.items())
    }


def _exact_constraint(version: SemVer) -> VersionConstraint:
    return VersionConstraint.parse("=" + str(version))


def _constraints_text(constraints: Iterable[VersionConstraint]) -> str:
    return " & ".join(sorted(str(item) for item in constraints))


def _matching_candidates(
    coordinate: str,
    constraints: tuple[VersionConstraint, ...],
    universe: Mapping[str, tuple[PackCandidate, ...]],
) -> tuple[PackCandidate, ...]:
    available = universe.get(coordinate, ())
    if not available:
        raise _fail(
            "SDAI-PACK-LOCK-003",
            f"missing dependency '{coordinate}' required by {_constraints_text(constraints)}",
        )
    matches = tuple(
        candidate
        for candidate in available
        if all(constraint.matches(candidate.version) for constraint in constraints)
    )
    if not matches:
        versions = ", ".join(str(candidate.version) for candidate in available)
        raise _fail(
            "SDAI-PACK-LOCK-004",
            f"version conflict for '{coordinate}': required {_constraints_text(constraints)}; "
            f"available [{versions}]",
        )
    return matches


def _solve(
    universe: Mapping[str, tuple[PackCandidate, ...]],
    selected: dict[str, PackCandidate],
    constraints: dict[str, tuple[VersionConstraint, ...]],
) -> dict[str, PackCandidate]:
    for coordinate, candidate in sorted(selected.items()):
        required = constraints.get(coordinate, ())
        if required and not all(item.matches(candidate.version) for item in required):
            raise _fail(
                "SDAI-PACK-LOCK-004",
                f"selected root/version '{candidate.identity}' conflicts with "
                f"{_constraints_text(required)}",
            )

    unresolved = sorted(set(constraints) - set(selected))
    if not unresolved:
        return selected
    coordinate = unresolved[0]
    candidates = _matching_candidates(coordinate, constraints[coordinate], universe)
    first_failure: PackLockError | None = None

    for candidate in candidates:
        next_selected = dict(selected)
        next_selected[coordinate] = candidate
        next_constraints = dict(constraints)
        for dependency in candidate.manifest.dependencies:
            existing = next_constraints.get(dependency.coordinate, ())
            next_constraints[dependency.coordinate] = (*existing, dependency.version)
        try:
            return _solve(universe, next_selected, next_constraints)
        except PackLockError as exc:
            if first_failure is None:
                first_failure = exc
            continue

    if first_failure is not None:
        raise first_failure
    raise _fail("SDAI-PACK-LOCK-004", f"unable to resolve '{coordinate}'")


def _dependency_graph(selected: Mapping[str, PackCandidate]) -> dict[str, tuple[str, ...]]:
    graph: dict[str, tuple[str, ...]] = {}
    for coordinate, candidate in sorted(selected.items()):
        dependencies = tuple(sorted(dependency.coordinate for dependency in candidate.manifest.dependencies))
        graph[coordinate] = dependencies
    return graph


def resolve_pack_lock(
    roots: Iterable[PackCandidate],
    available: Iterable[PackCandidate],
) -> PackLock:
    root_items = tuple(roots)
    if not root_items:
        raise _fail("SDAI-PACK-LOCK-002", "at least one root Pack candidate is required")
    all_candidates = (*root_items, *tuple(available))
    universe = _candidate_universe(all_candidates)

    selected: dict[str, PackCandidate] = {}
    constraints: dict[str, tuple[VersionConstraint, ...]] = {}
    root_coordinates: set[str] = set()
    for root in sorted(root_items, key=lambda item: item.coordinate):
        previous = selected.get(root.coordinate)
        if previous is not None and previous.identity != root.identity:
            raise _fail(
                "SDAI-PACK-LOCK-002",
                f"root coordinate '{root.coordinate}' requests multiple exact versions",
            )
        selected[root.coordinate] = root
        root_coordinates.add(root.coordinate)
        constraints[root.coordinate] = (*constraints.get(root.coordinate, ()), _exact_constraint(root.version))
        for dependency in root.manifest.dependencies:
            constraints[dependency.coordinate] = (
                *constraints.get(dependency.coordinate, ()),
                dependency.version,
            )

    resolved = _solve(universe, selected, constraints)
    graph = _dependency_graph(resolved)
    _reject_cycles(graph)

    entries: list[PackLockEntry] = []
    for coordinate, candidate in sorted(resolved.items()):
        exact_dependencies = tuple(
            sorted(resolved[dependency.coordinate].identity for dependency in candidate.manifest.dependencies)
        )
        entries.append(
            PackLockEntry(
                publisher=candidate.manifest.publisher,
                id=candidate.manifest.id,
                version=candidate.version,
                source=candidate.source,
                manifest_sha256=candidate.manifest.sha256,
                content_sha256=candidate.content_sha256,
                dependencies=exact_dependencies,
            )
        )
    root_identities = tuple(sorted(selected[coordinate].identity for coordinate in root_coordinates))
    return PackLock(roots=root_identities, packages=tuple(entries))


def compare_pack_lock(current: PackLock, expected: PackLock) -> PackLockFreshness:
    differences: list[str] = []
    current_entries = {entry.coordinate: entry for entry in current.packages}
    expected_entries = {entry.coordinate: entry for entry in expected.packages}
    for coordinate in sorted(set(current_entries) | set(expected_entries)):
        left = current_entries.get(coordinate)
        right = expected_entries.get(coordinate)
        if left is None:
            differences.append(f"missing:{coordinate}")
        elif right is None:
            differences.append(f"extra:{coordinate}")
        elif left.as_dict() != right.as_dict():
            differences.append(f"changed:{coordinate}")
    if current.roots != expected.roots:
        differences.append("changed:roots")
    return PackLockFreshness(
        current_sha256=current.sha256,
        expected_sha256=expected.sha256,
        outdated=bool(differences),
        differences=tuple(differences),
    )


def pack_lock_file_sha256(path: Path) -> str:
    if path.is_symlink():
        raise _fail("SDAI-PACK-LOCK-006", "Pack lock path must not be a symlink")
    if not path.is_file():
        raise _fail("SDAI-PACK-LOCK-006", f"Pack lock '{path}' must be an existing file")
    try:
        return _HASH_PREFIX + sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise _fail("SDAI-PACK-LOCK-006", f"unable to hash Pack lock '{path}'") from exc


def load_pack_lock(path: Path) -> PackLock:
    if path.is_symlink():
        raise _fail("SDAI-PACK-LOCK-006", "Pack lock path must not be a symlink")
    if not path.is_file():
        raise _fail("SDAI-PACK-LOCK-006", f"Pack lock '{path}' does not exist or is not a file")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise _fail("SDAI-PACK-LOCK-006", f"unable to read Pack lock '{path}' as UTF-8") from exc
    return PackLock.from_json(text)


def write_pack_lock(
    path: Path,
    lock: PackLock,
    *,
    expected_current_sha256: str | None = None,
) -> Path:
    if path.is_symlink():
        raise _fail("SDAI-PACK-LOCK-006", "Pack lock path must not be a symlink")
    if path.exists() and not path.is_file():
        raise _fail("SDAI-PACK-LOCK-006", f"Pack lock '{path}' exists but is not a file")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise _fail("SDAI-PACK-LOCK-006", f"unable to create Pack lock directory '{path.parent}'") from exc
    target_bytes = lock.to_text().encode("utf-8")

    if path.exists():
        try:
            current_bytes = path.read_bytes()
        except OSError as exc:
            raise _fail("SDAI-PACK-LOCK-006", f"unable to read existing Pack lock '{path}'") from exc
        if current_bytes == target_bytes:
            return path
        current_sha = _HASH_PREFIX + sha256(current_bytes).hexdigest()
        if expected_current_sha256 is None:
            raise _fail(
                "SDAI-PACK-LOCK-006",
                "existing Pack lock is outdated; explicit expected_current_sha256 is required to replace it",
            )
        expected = _hash(expected_current_sha256, label="expected_current_sha256")
        if current_sha != expected:
            raise _fail(
                "SDAI-PACK-LOCK-006",
                f"Pack lock changed concurrently: expected {expected}, found {current_sha}",
            )
    elif expected_current_sha256 is not None:
        raise _fail(
            "SDAI-PACK-LOCK-006",
            "cannot apply expected_current_sha256 because the Pack lock does not exist",
        )

    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            handle.write(target_bytes)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    except OSError as exc:
        raise _fail("SDAI-PACK-LOCK-006", f"unable to atomically write Pack lock '{path}'") from exc
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
    return path
