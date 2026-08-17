from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
import json
from pathlib import Path
import re
import stat
from typing import Iterable, Mapping
import unicodedata

import yaml
from yaml.constructor import ConstructorError
from yaml.resolver import BaseResolver


FEATURE_REPOSITORIES_API_VERSION = "sdai.feature-repositories/v1"
FEATURE_REPOSITORY_RESOLUTION_API_VERSION = "sdai.feature-repository-resolution/v1"
FEATURE_REPOSITORY_ROUTING_API_VERSION = "sdai.feature-repository-routing/v1"
FEATURE_REPOSITORIES_PATH = ".sdai/feature-repositories.yaml"
FEATURE_REPOSITORIES_MAX_BYTES = 1024 * 1024
FEATURE_REPOSITORIES_MAX_REPOSITORIES = 1024
FEATURE_REPOSITORIES_MAX_SELECTORS = 10_000


class FeatureRepositoryError(RuntimeError):
    """Raised when feature repository ownership cannot be trusted or resolved."""


class FeatureEntityType(StrEnum):
    REQUIREMENT = "requirement"
    CONTRACT = "contract"
    COMPONENT = "component"
    TASK = "task"


_CAPABILITY_BY_ENTITY: Mapping[FeatureEntityType, str] = {
    FeatureEntityType.REQUIREMENT: "requirements",
    FeatureEntityType.CONTRACT: "contracts",
    FeatureEntityType.COMPONENT: "components",
    FeatureEntityType.TASK: "tasks",
}
_SUPPORTED_CAPABILITIES = frozenset(_CAPABILITY_BY_ENTITY.values())
_REPOSITORY_ID = re.compile(r"^[a-z][a-z0-9]*(?:[-_.][a-z0-9]+)*$")
_ENTITY_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/#@+\-]{0,255}$")
_SELECTOR = re.compile(r"^[A-Za-z0-9*?][A-Za-z0-9._:/#@+*?\-]{0,255}$")
_TOP_LEVEL_KEYS = frozenset({"apiVersion", "kind", "repositories"})
_REPOSITORY_KEYS_REQUIRED = frozenset({"id", "path", "capabilities", "ownership"})
_REPOSITORY_KEYS_ALLOWED = _REPOSITORY_KEYS_REQUIRED | {"required"}
_SELECTOR_KEYS = frozenset({"type", "pattern"})


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(
    loader: _UniqueKeyLoader,
    node: yaml.nodes.MappingNode,
    deep: bool = False,
) -> dict[object, object]:
    mapping: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as exc:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found an unhashable mapping key",
                key_node.start_mark,
            ) from exc
        if duplicate:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found a duplicate mapping key",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping)


def _fail(code: str, message: str) -> FeatureRepositoryError:
    return FeatureRepositoryError(f"{code}: {message}")


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
        raise _fail(
            "SDAI-FEATURE-REPO-001",
            "feature repository data must be canonical finite JSON",
        ) from exc


def _sha256_bytes(value: bytes) -> str:
    return "sha256:" + sha256(value).hexdigest()


def _sha256_json(value: object) -> str:
    return _sha256_bytes(_canonical_json(value).encode("utf-8"))


def _mapping(value: object, *, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise _fail("SDAI-FEATURE-REPO-001", f"{label} must be a string-keyed mapping")
    return value


def _keys(
    raw: Mapping[str, object],
    *,
    required: frozenset[str],
    allowed: frozenset[str],
    label: str,
) -> None:
    missing = sorted(required - set(raw))
    unknown = sorted(set(raw) - allowed)
    if missing:
        raise _fail(
            "SDAI-FEATURE-REPO-001",
            f"{label} is missing required field(s): {', '.join(missing)}",
        )
    if unknown:
        raise _fail(
            "SDAI-FEATURE-REPO-001",
            f"{label} contains unsupported field(s): {', '.join(unknown)}",
        )


def _repo_id(value: object) -> str:
    if not isinstance(value, str):
        raise _fail("SDAI-FEATURE-REPO-001", "repository id must be text")
    candidate = unicodedata.normalize("NFC", value.strip())
    if candidate != value or not _REPOSITORY_ID.fullmatch(candidate):
        raise _fail(
            "SDAI-FEATURE-REPO-001",
            "repository id must be a normalized portable lowercase identifier",
        )
    return candidate


def _entity_id(value: object) -> str:
    if not isinstance(value, str):
        raise _fail("SDAI-FEATURE-REPO-006", "entity id must be text")
    candidate = unicodedata.normalize("NFC", value.strip())
    if candidate != value or not _ENTITY_ID.fullmatch(candidate):
        raise _fail(
            "SDAI-FEATURE-REPO-006",
            "entity id must use 1-256 portable identity characters",
        )
    return candidate


def _declared_path(value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise _fail(
            "SDAI-FEATURE-REPO-002",
            "repository path must be explicit non-empty local path text",
        )
    candidate = unicodedata.normalize("NFC", value)
    if (
        candidate != value
        or "\x00" in candidate
        or any(0xD800 <= ord(character) <= 0xDFFF for character in candidate)
        or any(ord(character) < 32 for character in candidate)
        or len(candidate.encode("utf-8")) > 4096
    ):
        raise _fail(
            "SDAI-FEATURE-REPO-002",
            "repository path must be NFC-normalized valid bounded local path text",
        )
    return candidate


def _source_path(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise _fail("SDAI-FEATURE-REPO-002", "declaration source must be a non-empty path")
    candidate = unicodedata.normalize("NFC", value)
    if candidate != value or "\\" in candidate or candidate.startswith("/"):
        raise _fail(
            "SDAI-FEATURE-REPO-002",
            "declaration source must be a normalized project-relative POSIX path",
        )
    parts = candidate.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise _fail(
            "SDAI-FEATURE-REPO-002",
            "declaration source must be a normalized project-relative POSIX path",
        )
    return candidate


def _capabilities(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise _fail(
            "SDAI-FEATURE-REPO-001",
            "repository capabilities must be a non-empty list",
        )
    if not all(isinstance(item, str) for item in value):
        raise _fail(
            "SDAI-FEATURE-REPO-001",
            "repository capabilities must contain only strings",
        )
    items = tuple(sorted(value))
    if len(set(items)) != len(items):
        raise _fail("SDAI-FEATURE-REPO-003", "repository capabilities contain duplicates")
    unsupported = sorted(set(items) - _SUPPORTED_CAPABILITIES)
    if unsupported:
        raise _fail(
            "SDAI-FEATURE-REPO-001",
            "unsupported repository capability: " + ", ".join(unsupported),
        )
    return items


def _selector_pattern(value: object) -> str:
    if not isinstance(value, str):
        raise _fail("SDAI-FEATURE-REPO-001", "ownership selector pattern must be text")
    candidate = unicodedata.normalize("NFC", value.strip())
    if candidate != value or not _SELECTOR.fullmatch(candidate) or "**" in candidate:
        raise _fail(
            "SDAI-FEATURE-REPO-001",
            "selector pattern must use portable entity characters plus single '*'/'?' wildcards",
        )
    return candidate


def _selector_regex(pattern: str) -> re.Pattern[str]:
    pieces: list[str] = ["^"]
    for character in pattern:
        if character == "*":
            pieces.append(".*")
        elif character == "?":
            pieces.append(".")
        else:
            pieces.append(re.escape(character))
    pieces.append("$")
    return re.compile("".join(pieces), flags=re.ASCII)


def _is_redirect(path: Path, *, label: str) -> bool:
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        if is_junction is not None and is_junction():
            return True
        try:
            attributes = getattr(path.lstat(), "st_file_attributes", 0)
        except FileNotFoundError:
            return False
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)
        return bool(attributes & reparse)
    except (OSError, UnicodeError, ValueError) as exc:
        raise _fail(
            "SDAI-FEATURE-REPO-002",
            f"{label} redirect status could not be verified",
        ) from exc


def _reject_redirect_chain(path: Path, *, label: str) -> None:
    absolute = path.absolute()
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        if _is_redirect(current, label=label):
            raise _fail(
                "SDAI-FEATURE-REPO-002",
                f"{label} must not contain symlinks, junctions, or reparse points",
            )


def _resolve_declaration(project_root: Path, path: Path | None) -> Path:
    candidate = Path(path) if path is not None else project_root / FEATURE_REPOSITORIES_PATH
    if not candidate.is_absolute():
        candidate = project_root / candidate
    _reject_redirect_chain(candidate, label="feature repository declaration")
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(project_root)
    except (OSError, RuntimeError, UnicodeError, ValueError) as exc:
        raise _fail(
            "SDAI-FEATURE-REPO-002",
            "feature repository declaration must be an existing file inside the project",
        ) from exc
    if not resolved.is_file():
        raise _fail(
            "SDAI-FEATURE-REPO-002",
            "feature repository declaration must be an existing regular file",
        )
    return resolved


def _resolve_repository(project_root: Path, declared: str, *, required: bool) -> Path | None:
    candidate = Path(declared)
    if not candidate.is_absolute():
        candidate = project_root / candidate
    _reject_redirect_chain(candidate, label="feature repository path")
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError, UnicodeError, ValueError) as exc:
        if required:
            raise _fail(
                "SDAI-FEATURE-REPO-004",
                f"required repository path is missing: {declared}",
            ) from exc
        return None
    if not resolved.is_dir():
        if required:
            raise _fail(
                "SDAI-FEATURE-REPO-004",
                f"required repository path is not a directory: {declared}",
            )
        return None
    git_marker = resolved / ".git"
    if not git_marker.exists():
        if required:
            raise _fail(
                "SDAI-FEATURE-REPO-004",
                f"required repository is not a local Git worktree: {declared}",
            )
        return None
    if _is_redirect(git_marker, label="repository Git metadata"):
        raise _fail(
            "SDAI-FEATURE-REPO-002",
            "repository Git metadata must not be a filesystem redirect",
        )
    return resolved


def _portable_resolved_root_key(path: Path) -> tuple[str, ...]:
    return tuple(unicodedata.normalize("NFC", part).casefold() for part in path.parts)


def _read_bounded(path: Path) -> tuple[bytes, str]:
    try:
        with path.open("rb") as stream:
            data = stream.read(FEATURE_REPOSITORIES_MAX_BYTES + 1)
    except OSError as exc:
        raise _fail("SDAI-FEATURE-REPO-002", "unable to read feature repository declaration") from exc
    if len(data) > FEATURE_REPOSITORIES_MAX_BYTES:
        raise _fail(
            "SDAI-FEATURE-REPO-001",
            "feature repository declaration exceeds the 1 MiB input limit",
        )
    try:
        text = data.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError as exc:
        raise _fail(
            "SDAI-FEATURE-REPO-001",
            "feature repository declaration must be valid UTF-8",
        ) from exc
    return data, text.replace("\r\n", "\n").replace("\r", "\n")


@dataclass(frozen=True)
class OwnershipSelector:
    type: FeatureEntityType
    pattern: str

    def __post_init__(self) -> None:
        try:
            kind = self.type if isinstance(self.type, FeatureEntityType) else FeatureEntityType(self.type)
        except ValueError as exc:
            raise _fail("SDAI-FEATURE-REPO-001", f"unsupported ownership entity type: {self.type!r}") from exc
        object.__setattr__(self, "type", kind)
        object.__setattr__(self, "pattern", _selector_pattern(self.pattern))

    @property
    def capability(self) -> str:
        return _CAPABILITY_BY_ENTITY[self.type]

    def matches(self, entity_id: str) -> bool:
        return _selector_regex(self.pattern).fullmatch(_entity_id(entity_id)) is not None

    def as_dict(self) -> dict[str, str]:
        return {"pattern": self.pattern, "type": self.type.value}

    @classmethod
    def from_dict(cls, value: object) -> "OwnershipSelector":
        raw = _mapping(value, label="ownership selector")
        _keys(raw, required=_SELECTOR_KEYS, allowed=_SELECTOR_KEYS, label="ownership selector")
        try:
            entity_type = FeatureEntityType(raw["type"])
        except (TypeError, ValueError) as exc:
            raise _fail(
                "SDAI-FEATURE-REPO-001",
                f"unsupported ownership entity type: {raw.get('type')!r}",
            ) from exc
        return cls(entity_type, raw["pattern"])  # type: ignore[arg-type]


@dataclass(frozen=True)
class FeatureRepository:
    id: str
    path: str
    capabilities: tuple[str, ...]
    ownership: tuple[OwnershipSelector, ...]
    required: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _repo_id(self.id))
        object.__setattr__(self, "path", _declared_path(self.path))
        capabilities = _capabilities(list(self.capabilities))
        object.__setattr__(self, "capabilities", capabilities)
        if not isinstance(self.required, bool):
            raise _fail("SDAI-FEATURE-REPO-001", "repository required must be boolean")
        if not isinstance(self.ownership, (tuple, list)) or not self.ownership:
            raise _fail(
                "SDAI-FEATURE-REPO-001",
                "repository ownership must contain at least one selector",
            )
        if not all(isinstance(item, OwnershipSelector) for item in self.ownership):
            raise _fail("SDAI-FEATURE-REPO-001", "repository ownership contains invalid selectors")
        ordered = tuple(sorted(self.ownership, key=lambda item: (item.type.value, item.pattern)))
        keys = [(item.type.value, item.pattern) for item in ordered]
        if len(set(keys)) != len(keys):
            raise _fail("SDAI-FEATURE-REPO-003", f"repository '{self.id}' contains duplicate selectors")
        missing = sorted({item.capability for item in ordered} - set(capabilities))
        if missing:
            raise _fail(
                "SDAI-FEATURE-REPO-003",
                f"repository '{self.id}' selectors require undeclared capability: {', '.join(missing)}",
            )
        object.__setattr__(self, "ownership", ordered)

    def as_dict(self) -> dict[str, object]:
        return {
            "capabilities": list(self.capabilities),
            "id": self.id,
            "ownership": [item.as_dict() for item in self.ownership],
            "path": self.path,
            "required": self.required,
        }

    @classmethod
    def from_dict(cls, value: object) -> "FeatureRepository":
        raw = _mapping(value, label="feature repository")
        _keys(
            raw,
            required=_REPOSITORY_KEYS_REQUIRED,
            allowed=_REPOSITORY_KEYS_ALLOWED,
            label="feature repository",
        )
        ownership = raw["ownership"]
        if not isinstance(ownership, list) or not ownership:
            raise _fail(
                "SDAI-FEATURE-REPO-001",
                "repository ownership must be a non-empty list",
            )
        if len(ownership) > FEATURE_REPOSITORIES_MAX_SELECTORS:
            raise _fail("SDAI-FEATURE-REPO-001", "repository ownership has too many selectors")
        return cls(
            id=raw["id"],  # type: ignore[arg-type]
            path=raw["path"],  # type: ignore[arg-type]
            capabilities=_capabilities(raw["capabilities"]),
            ownership=tuple(OwnershipSelector.from_dict(item) for item in ownership),
            required=raw.get("required", True),  # type: ignore[arg-type]
        )


@dataclass(frozen=True)
class FeatureRepositoryManifest:
    repositories: tuple[FeatureRepository, ...]
    source_sha256: str
    source: str = FEATURE_REPOSITORIES_PATH

    def __post_init__(self) -> None:
        if not isinstance(self.repositories, (tuple, list)) or not self.repositories:
            raise _fail("SDAI-FEATURE-REPO-001", "repositories must be a non-empty materialized list")
        if len(self.repositories) > FEATURE_REPOSITORIES_MAX_REPOSITORIES:
            raise _fail("SDAI-FEATURE-REPO-001", "feature repository manifest has too many repositories")
        if not all(isinstance(item, FeatureRepository) for item in self.repositories):
            raise _fail("SDAI-FEATURE-REPO-001", "repositories contain invalid entries")
        selector_count = sum(len(item.ownership) for item in self.repositories)
        if selector_count > FEATURE_REPOSITORIES_MAX_SELECTORS:
            raise _fail(
                "SDAI-FEATURE-REPO-001",
                "feature repository manifest has too many ownership selectors",
            )
        ordered = tuple(sorted(self.repositories, key=lambda item: item.id))
        ids = [item.id for item in ordered]
        if len(set(ids)) != len(ids):
            raise _fail("SDAI-FEATURE-REPO-003", "feature repository ids must be unique")
        paths = [item.path.casefold() for item in ordered]
        if len(set(paths)) != len(paths):
            raise _fail("SDAI-FEATURE-REPO-003", "declared repository paths must be unique")
        selector_owners: dict[tuple[str, str], str] = {}
        for repository in ordered:
            for selector in repository.ownership:
                key = (selector.type.value, selector.pattern)
                previous = selector_owners.get(key)
                if previous is not None:
                    raise _fail(
                        "SDAI-FEATURE-REPO-003",
                        f"selector {selector.type.value}:{selector.pattern} is declared by both '{previous}' and '{repository.id}'",
                    )
                selector_owners[key] = repository.id
        object.__setattr__(self, "repositories", ordered)
        if not isinstance(self.source_sha256, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", self.source_sha256):
            raise _fail("SDAI-FEATURE-REPO-001", "source_sha256 must be a lowercase SHA-256 digest")
        object.__setattr__(self, "source", _source_path(self.source))

    def as_dict(self) -> dict[str, object]:
        return {
            "apiVersion": FEATURE_REPOSITORIES_API_VERSION,
            "kind": "FeatureRepositories",
            "repositories": [item.as_dict() for item in self.repositories],
        }

    def to_json(self) -> str:
        return _canonical_json(self.as_dict())

    @property
    def sha256(self) -> str:
        return _sha256_json(self.as_dict())

    @classmethod
    def from_dict(
        cls,
        value: object,
        *,
        source_sha256: str,
        source: str = FEATURE_REPOSITORIES_PATH,
    ) -> "FeatureRepositoryManifest":
        raw = _mapping(value, label="feature repository manifest")
        _keys(raw, required=_TOP_LEVEL_KEYS, allowed=_TOP_LEVEL_KEYS, label="feature repository manifest")
        if raw["apiVersion"] != FEATURE_REPOSITORIES_API_VERSION:
            raise _fail("SDAI-FEATURE-REPO-001", "unsupported feature repository apiVersion")
        if raw["kind"] != "FeatureRepositories":
            raise _fail("SDAI-FEATURE-REPO-001", "manifest kind must be 'FeatureRepositories'")
        repositories = raw["repositories"]
        if not isinstance(repositories, list) or not repositories:
            raise _fail("SDAI-FEATURE-REPO-001", "repositories must be a non-empty list")
        return cls(
            tuple(FeatureRepository.from_dict(item) for item in repositories),
            source_sha256,
            source,
        )


@dataclass(frozen=True)
class ResolvedFeatureRepository:
    repository: FeatureRepository
    root: Path | None
    ordinal: int

    @property
    def available(self) -> bool:
        return self.root is not None

    def as_dict(self) -> dict[str, object]:
        return {
            "available": self.available,
            "capabilities": list(self.repository.capabilities),
            "id": self.repository.id,
            "ordinal": self.ordinal,
            "ownership": [item.as_dict() for item in self.repository.ownership],
            "required": self.repository.required,
        }


@dataclass(frozen=True)
class ResolvedFeatureRepositories:
    repositories: tuple[ResolvedFeatureRepository, ...]
    manifest_sha256: str
    source_sha256: str
    source: str = FEATURE_REPOSITORIES_PATH

    def __post_init__(self) -> None:
        object.__setattr__(self, "source", _source_path(self.source))

    def as_dict(self) -> dict[str, object]:
        return {
            "apiVersion": FEATURE_REPOSITORY_RESOLUTION_API_VERSION,
            "manifestSha256": self.manifest_sha256,
            "repositories": [item.as_dict() for item in self.repositories],
            "source": self.source,
            "sourceSha256": self.source_sha256,
        }

    def to_json(self) -> str:
        return _canonical_json(self.as_dict())

    @property
    def sha256(self) -> str:
        return _sha256_json(self.as_dict())


@dataclass(frozen=True)
class RoutableEntity:
    type: FeatureEntityType
    entity_id: str
    required: bool = True

    def __post_init__(self) -> None:
        try:
            kind = self.type if isinstance(self.type, FeatureEntityType) else FeatureEntityType(self.type)
        except ValueError as exc:
            raise _fail("SDAI-FEATURE-REPO-006", f"unsupported entity type: {self.type!r}") from exc
        object.__setattr__(self, "type", kind)
        object.__setattr__(self, "entity_id", _entity_id(self.entity_id))
        if not isinstance(self.required, bool):
            raise _fail("SDAI-FEATURE-REPO-006", "entity required must be boolean")

    @property
    def identity(self) -> str:
        return f"{self.type.value}:{self.entity_id}"

    def as_dict(self) -> dict[str, object]:
        return {"entityId": self.entity_id, "required": self.required, "type": self.type.value}


@dataclass(frozen=True)
class RouteDecision:
    entity: RoutableEntity
    repository_id: str
    selector: OwnershipSelector
    repository_ordinal: int
    manifest_sha256: str
    source_sha256: str
    source: str = FEATURE_REPOSITORIES_PATH

    def __post_init__(self) -> None:
        object.__setattr__(self, "source", _source_path(self.source))

    def as_dict(self) -> dict[str, object]:
        return {
            "entity": self.entity.as_dict(),
            "manifestSha256": self.manifest_sha256,
            "provenance": {
                "repositoryId": self.repository_id,
                "repositoryOrdinal": self.repository_ordinal,
                "selector": self.selector.as_dict(),
                "source": self.source,
                "sourceSha256": self.source_sha256,
            },
        }

    @property
    def sha256(self) -> str:
        return _sha256_json(self.as_dict())


@dataclass(frozen=True)
class FeatureRoutingResult:
    decisions: tuple[RouteDecision, ...]
    unmatched_optional: tuple[RoutableEntity, ...]
    manifest_sha256: str
    resolution_sha256: str

    def as_dict(self) -> dict[str, object]:
        return {
            "apiVersion": FEATURE_REPOSITORY_ROUTING_API_VERSION,
            "decisions": [item.as_dict() | {"decisionSha256": item.sha256} for item in self.decisions],
            "manifestSha256": self.manifest_sha256,
            "resolutionSha256": self.resolution_sha256,
            "unmatchedOptional": [item.as_dict() for item in self.unmatched_optional],
        }

    def to_json(self) -> str:
        return _canonical_json(self.as_dict())

    @property
    def sha256(self) -> str:
        return _sha256_json(self.as_dict())


def load_feature_repository_manifest(
    project_root: Path,
    path: Path | None = None,
) -> FeatureRepositoryManifest:
    try:
        root = Path(project_root).resolve(strict=True)
    except (OSError, RuntimeError, TypeError, UnicodeError, ValueError) as exc:
        raise _fail("SDAI-FEATURE-REPO-002", "project root must be an existing local directory") from exc
    if not root.is_dir():
        raise _fail("SDAI-FEATURE-REPO-002", "project root must be an existing local directory")
    declaration = _resolve_declaration(root, path)
    data, text = _read_bounded(declaration)
    try:
        if any(isinstance(event, yaml.events.AliasEvent) for event in yaml.parse(text)):
            raise _fail("SDAI-FEATURE-REPO-001", "feature repository YAML aliases are not allowed")
        raw = yaml.load(text, Loader=_UniqueKeyLoader)
    except FeatureRepositoryError:
        raise
    except (OverflowError, RecursionError, ValueError, yaml.YAMLError) as exc:
        raise _fail("SDAI-FEATURE-REPO-001", "feature repository YAML is malformed") from exc
    source = declaration.relative_to(root).as_posix()
    return FeatureRepositoryManifest.from_dict(
        raw,
        source_sha256=_sha256_bytes(data),
        source=source,
    )


def resolve_feature_repositories(
    project_root: Path,
    path: Path | None = None,
) -> ResolvedFeatureRepositories:
    try:
        root = Path(project_root).resolve(strict=True)
    except (OSError, RuntimeError, TypeError, UnicodeError, ValueError) as exc:
        raise _fail("SDAI-FEATURE-REPO-002", "project root must be an existing local directory") from exc
    if not root.is_dir():
        raise _fail("SDAI-FEATURE-REPO-002", "project root must be an existing local directory")
    manifest = load_feature_repository_manifest(root, path)
    resolved: list[ResolvedFeatureRepository] = []
    seen_roots: list[tuple[str, tuple[str, ...]]] = []
    for ordinal, repository in enumerate(manifest.repositories, start=1):
        repository_root = _resolve_repository(root, repository.path, required=repository.required)
        if repository_root is not None:
            key = _portable_resolved_root_key(repository_root)
            for previous_id, previous_key in seen_roots:
                common = min(len(previous_key), len(key))
                if previous_key[:common] == key[:common]:
                    raise _fail(
                        "SDAI-FEATURE-REPO-003",
                        f"repositories '{previous_id}' and '{repository.id}' resolve to duplicate or nested local paths",
                    )
            seen_roots.append((repository.id, key))
        resolved.append(ResolvedFeatureRepository(repository, repository_root, ordinal))
    return ResolvedFeatureRepositories(
        tuple(resolved),
        manifest.sha256,
        manifest.source_sha256,
        manifest.source,
    )


def route_feature_entities(
    resolved: ResolvedFeatureRepositories,
    entities: Iterable[RoutableEntity],
) -> FeatureRoutingResult:
    materialized = tuple(entities)
    if not all(isinstance(item, RoutableEntity) for item in materialized):
        raise _fail("SDAI-FEATURE-REPO-006", "routing input contains an invalid entity")
    ordered = tuple(sorted(materialized, key=lambda item: (item.type.value, item.entity_id)))
    identities = [item.identity for item in ordered]
    if len(set(identities)) != len(identities):
        raise _fail("SDAI-FEATURE-REPO-006", "routing input contains duplicate entity identities")

    decisions: list[RouteDecision] = []
    unmatched: list[RoutableEntity] = []
    for entity in ordered:
        matches: list[tuple[ResolvedFeatureRepository, OwnershipSelector]] = []
        for repository in resolved.repositories:
            for selector in repository.repository.ownership:
                if selector.type is entity.type and selector.matches(entity.entity_id):
                    matches.append((repository, selector))
        if not matches:
            if entity.required:
                raise _fail(
                    "SDAI-FEATURE-REPO-007",
                    f"required entity '{entity.identity}' is not owned by any repository",
                )
            unmatched.append(entity)
            continue
        repository_ids = {repository.repository.id for repository, _ in matches}
        if len(repository_ids) != 1:
            details = ", ".join(
                f"{repository.repository.id}:{selector.pattern}"
                for repository, selector in sorted(
                    matches,
                    key=lambda item: (item[0].repository.id, item[1].pattern),
                )
            )
            raise _fail(
                "SDAI-FEATURE-REPO-005",
                f"entity '{entity.identity}' has ambiguous ownership ({details})",
            )
        repository = matches[0][0]
        if not repository.available:
            raise _fail(
                "SDAI-FEATURE-REPO-004",
                f"entity '{entity.identity}' routes to unavailable repository '{repository.repository.id}'",
            )
        selectors = tuple(sorted((selector for _, selector in matches), key=lambda item: item.pattern))
        # Multiple selectors in the same repository are valid ownership overlap,
        # but choose the most specific deterministic selector for provenance.
        selector = sorted(
            selectors,
            key=lambda item: (
                item.pattern.count("*") + item.pattern.count("?"),
                -len(item.pattern),
                item.pattern,
            ),
        )[0]
        decisions.append(
            RouteDecision(
                entity=entity,
                repository_id=repository.repository.id,
                selector=selector,
                repository_ordinal=repository.ordinal,
                manifest_sha256=resolved.manifest_sha256,
                source_sha256=resolved.source_sha256,
                source=resolved.source,
            )
        )
    return FeatureRoutingResult(
        decisions=tuple(decisions),
        unmatched_optional=tuple(unmatched),
        manifest_sha256=resolved.manifest_sha256,
        resolution_sha256=resolved.sha256,
    )