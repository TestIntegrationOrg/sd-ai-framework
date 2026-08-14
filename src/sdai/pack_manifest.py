from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path, PurePosixPath
import re
from typing import Any, Callable, Mapping
import unicodedata

import yaml
from yaml.constructor import ConstructorError
from yaml.resolver import BaseResolver

from sdai.path_safety import ensure_within_project


PACK_MANIFEST_API_VERSION = "sdai.pack-manifest/v1"


class PackManifestError(RuntimeError):
    pass


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(loader: _UniqueKeyLoader, node: yaml.nodes.MappingNode, deep: bool = False) -> dict[object, object]:
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
                f"found duplicate key {key!r}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping)


_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9]*(?:[-_.][a-z0-9]+)*$")
_API_RE = re.compile(r"^[a-z][a-z0-9.-]*/v[1-9][0-9]*$")
_SEMVER_RE = re.compile(
    r"^(0|[1-9][0-9]*)\."
    r"(0|[1-9][0-9]*)\."
    r"(0|[1-9][0-9]*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
_CONSTRAINT_RE = re.compile(r"^(==|=|>=|<=|>|<)?(.+)$")
_OPERATOR_ORDER = {"=": 0, ">": 1, ">=": 2, "<": 3, "<=": 4}

_MANIFEST_KEYS = frozenset(
    {
        "apiVersion",
        "id",
        "publisher",
        "version",
        "description",
        "capabilities",
        "contentRoots",
        "dependencies",
        "compatibility",
    }
)
_DEPENDENCY_KEYS = frozenset({"id", "publisher", "version"})
_COMPATIBILITY_KEYS = frozenset({"framework", "apis"})


def _fail(code: str, message: str) -> PackManifestError:
    return PackManifestError(f"{code}: {message}")


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
        raise _fail("SDAI-PACK-001", "manifest is not canonical finite JSON") from exc


def _expect_mapping(value: object, *, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise _fail("SDAI-PACK-001", f"{label} must be a string-keyed mapping")
    return value


def _validate_keys(
    value: Mapping[str, object],
    *,
    required: frozenset[str],
    label: str,
) -> None:
    unknown = sorted(set(value) - required)
    missing = sorted(required - set(value))
    if unknown:
        raise _fail(
            "SDAI-PACK-001",
            f"{label} contains unsupported field(s): {', '.join(unknown)}",
        )
    if missing:
        raise _fail(
            "SDAI-PACK-001",
            f"{label} is missing required field(s): {', '.join(missing)}",
        )


def _non_empty_string(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _fail("SDAI-PACK-001", f"{label} must be a non-empty string")
    if "\x00" in value:
        raise _fail("SDAI-PACK-001", f"{label} must not contain NUL")
    return unicodedata.normalize("NFC", value.strip())


def _identifier(value: object, *, label: str) -> str:
    text = _non_empty_string(value, label=label)
    if not _IDENTIFIER_RE.fullmatch(text):
        raise _fail(
            "SDAI-PACK-001",
            f"{label} '{text}' is not a portable lowercase identifier",
        )
    return text


def _string_list(
    value: object,
    *,
    label: str,
    validator: Callable[[str], str] | None = None,
    allow_empty: bool = True,
) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise _fail("SDAI-PACK-001", f"{label} must be a list")
    result: list[str] = []
    for index, raw in enumerate(value):
        text = _non_empty_string(raw, label=f"{label}[{index}]")
        result.append(validator(text) if validator is not None else text)
    if not allow_empty and not result:
        raise _fail("SDAI-PACK-001", f"{label} must not be empty")
    if len(set(result)) != len(result):
        raise _fail("SDAI-PACK-001", f"{label} must not contain duplicates")
    return tuple(sorted(result))


def _portable_relative_path(value: str) -> str:
    if not value or "\x00" in value:
        raise _fail("SDAI-PACK-002", "content root must be a non-empty safe path")
    if "\\" in value:
        raise _fail("SDAI-PACK-002", f"content root '{value}' must use POSIX '/' separators")
    if re.match(r"^[A-Za-z]:", value):
        raise _fail("SDAI-PACK-002", f"content root '{value}' must be repository-relative")
    if any(part in {"", ".", ".."} for part in value.split("/")):
        raise _fail("SDAI-PACK-002", f"content root '{value}' contains an unsafe path segment")
    path = PurePosixPath(value)
    if path.is_absolute():
        raise _fail("SDAI-PACK-002", f"content root '{value}' must be repository-relative")
    return path.as_posix()


def _api_version(value: str) -> str:
    if not _API_RE.fullmatch(value):
        raise _fail("SDAI-PACK-001", f"compatibility API '{value}' is not a portable versioned API id")
    return value


@dataclass(frozen=True)
class SemVer:
    major: int
    minor: int
    patch: int
    prerelease: tuple[str, ...] = ()
    build: tuple[str, ...] = ()

    @classmethod
    def parse(cls, value: object) -> "SemVer":
        text = _non_empty_string(value, label="semantic version")
        match = _SEMVER_RE.fullmatch(text)
        if match is None:
            raise _fail("SDAI-PACK-003", f"invalid semantic version '{text}'")
        prerelease = tuple((match.group(4) or "").split(".")) if match.group(4) else ()
        build = tuple((match.group(5) or "").split(".")) if match.group(5) else ()
        for identifier in prerelease:
            if identifier.isdigit() and len(identifier) > 1 and identifier.startswith("0"):
                raise _fail(
                    "SDAI-PACK-003",
                    f"semantic version '{text}' has a prerelease numeric identifier with a leading zero",
                )
        return cls(int(match.group(1)), int(match.group(2)), int(match.group(3)), prerelease, build)

    def __str__(self) -> str:
        value = f"{self.major}.{self.minor}.{self.patch}"
        if self.prerelease:
            value += "-" + ".".join(self.prerelease)
        if self.build:
            value += "+" + ".".join(self.build)
        return value

    def compare_precedence(self, other: "SemVer") -> int:
        left_core = (self.major, self.minor, self.patch)
        right_core = (other.major, other.minor, other.patch)
        if left_core != right_core:
            return -1 if left_core < right_core else 1
        if self.prerelease == other.prerelease:
            return 0
        if not self.prerelease:
            return 1
        if not other.prerelease:
            return -1
        for left, right in zip(self.prerelease, other.prerelease):
            if left == right:
                continue
            left_numeric = left.isdigit()
            right_numeric = right.isdigit()
            if left_numeric and right_numeric:
                return -1 if int(left) < int(right) else 1
            if left_numeric != right_numeric:
                return -1 if left_numeric else 1
            return -1 if left < right else 1
        return -1 if len(self.prerelease) < len(other.prerelease) else 1

    def same_precedence(self, other: "SemVer") -> bool:
        return self.compare_precedence(other) == 0

    def exactly_equals(self, other: "SemVer") -> bool:
        return (
            self.major,
            self.minor,
            self.patch,
            self.prerelease,
            self.build,
        ) == (
            other.major,
            other.minor,
            other.patch,
            other.prerelease,
            other.build,
        )

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, SemVer):
            return NotImplemented
        return self.compare_precedence(other) < 0


@dataclass(frozen=True)
class VersionComparator:
    operator: str
    version: SemVer

    @classmethod
    def parse(cls, value: str) -> "VersionComparator":
        match = _CONSTRAINT_RE.fullmatch(value.strip())
        if match is None:
            raise _fail("SDAI-PACK-004", f"invalid version comparator '{value}'")
        operator = match.group(1) or "="
        if operator == "==":
            operator = "="
        version = SemVer.parse(match.group(2).strip())
        return cls(operator, version)

    def __str__(self) -> str:
        return f"{self.operator}{self.version}"

    def matches(self, candidate: SemVer) -> bool:
        comparison = candidate.compare_precedence(self.version)
        if self.operator == "=":
            if self.version.build:
                return candidate.exactly_equals(self.version)
            return comparison == 0
        if self.operator == ">":
            return comparison > 0
        if self.operator == ">=":
            return comparison >= 0
        if self.operator == "<":
            return comparison < 0
        if self.operator == "<=":
            return comparison <= 0
        raise _fail("SDAI-PACK-004", f"unsupported version comparator '{self.operator}'")


@dataclass(frozen=True)
class VersionConstraint:
    comparators: tuple[VersionComparator, ...]

    @classmethod
    def parse(cls, value: object) -> "VersionConstraint":
        text = _non_empty_string(value, label="version constraint")
        if text == "*":
            return cls(())
        raw_parts = [part.strip() for part in text.split(",")]
        if any(not part for part in raw_parts):
            raise _fail("SDAI-PACK-004", f"invalid version constraint '{text}'")
        parsed = [VersionComparator.parse(part) for part in raw_parts]
        unique = {(item.operator, str(item.version)): item for item in parsed}
        if len(unique) != len(parsed):
            raise _fail("SDAI-PACK-004", f"version constraint '{text}' contains duplicate comparators")
        ordered = sorted(
            parsed,
            key=lambda item: (
                item.version.major,
                item.version.minor,
                item.version.patch,
                item.version.prerelease,
                item.version.build,
                _OPERATOR_ORDER[item.operator],
            ),
        )
        return cls(tuple(ordered))

    def __str__(self) -> str:
        if not self.comparators:
            return "*"
        return ",".join(str(item) for item in self.comparators)

    def matches(self, candidate: SemVer | str) -> bool:
        version = SemVer.parse(candidate) if isinstance(candidate, str) else candidate
        return all(item.matches(version) for item in self.comparators)


@dataclass(frozen=True)
class PackDependency:
    publisher: str
    id: str
    version: VersionConstraint

    @property
    def coordinate(self) -> str:
        return f"{self.publisher}/{self.id}"

    def as_dict(self) -> dict[str, object]:
        return {"id": self.id, "publisher": self.publisher, "version": str(self.version)}

    @classmethod
    def from_dict(cls, value: object) -> "PackDependency":
        raw = _expect_mapping(value, label="pack dependency")
        _validate_keys(raw, required=_DEPENDENCY_KEYS, label="pack dependency")
        return cls(
            publisher=_identifier(raw["publisher"], label="dependency publisher"),
            id=_identifier(raw["id"], label="dependency id"),
            version=VersionConstraint.parse(raw["version"]),
        )


@dataclass(frozen=True)
class PackCompatibility:
    framework: VersionConstraint
    apis: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {"apis": list(self.apis), "framework": str(self.framework)}

    @classmethod
    def from_dict(cls, value: object) -> "PackCompatibility":
        raw = _expect_mapping(value, label="pack compatibility")
        _validate_keys(raw, required=_COMPATIBILITY_KEYS, label="pack compatibility")
        apis = _string_list(
            raw["apis"],
            label="compatibility.apis",
            validator=_api_version,
            allow_empty=True,
        )
        return cls(framework=VersionConstraint.parse(raw["framework"]), apis=apis)


@dataclass(frozen=True)
class PackManifest:
    id: str
    publisher: str
    version: SemVer
    description: str
    capabilities: tuple[str, ...]
    content_roots: tuple[str, ...]
    dependencies: tuple[PackDependency, ...]
    compatibility: PackCompatibility

    @property
    def coordinate(self) -> str:
        return f"{self.publisher}/{self.id}"

    @property
    def identity(self) -> str:
        return f"{self.coordinate}@{self.version}"

    def as_dict(self) -> dict[str, object]:
        return {
            "apiVersion": PACK_MANIFEST_API_VERSION,
            "capabilities": list(self.capabilities),
            "compatibility": self.compatibility.as_dict(),
            "contentRoots": list(self.content_roots),
            "dependencies": [item.as_dict() for item in self.dependencies],
            "description": self.description,
            "id": self.id,
            "publisher": self.publisher,
            "version": str(self.version),
        }

    def to_json(self) -> str:
        return _canonical_json(self.as_dict())

    @property
    def sha256(self) -> str:
        return "sha256:" + sha256(self.to_json().encode("utf-8")).hexdigest()

    def supports_framework(self, version: str | SemVer) -> bool:
        return self.compatibility.framework.matches(version)

    def requires_api(self, api_version: str) -> bool:
        return api_version in self.compatibility.apis

    @classmethod
    def from_dict(cls, value: object) -> "PackManifest":
        raw = _expect_mapping(value, label="Pack manifest")
        _validate_keys(raw, required=_MANIFEST_KEYS, label="Pack manifest")
        if raw["apiVersion"] != PACK_MANIFEST_API_VERSION:
            raise _fail(
                "SDAI-PACK-001",
                f"unsupported apiVersion '{raw['apiVersion']}', expected '{PACK_MANIFEST_API_VERSION}'",
            )

        capabilities = _string_list(
            raw["capabilities"],
            label="capabilities",
            validator=lambda item: _identifier(item, label="capability"),
            allow_empty=False,
        )
        content_roots = _string_list(
            raw["contentRoots"],
            label="contentRoots",
            validator=_portable_relative_path,
            allow_empty=False,
        )

        raw_dependencies = raw["dependencies"]
        if not isinstance(raw_dependencies, list):
            raise _fail("SDAI-PACK-001", "dependencies must be a list")
        dependencies = tuple(PackDependency.from_dict(item) for item in raw_dependencies)
        coordinates = [item.coordinate for item in dependencies]
        if len(set(coordinates)) != len(coordinates):
            raise _fail("SDAI-PACK-001", "dependencies must not repeat a publisher/id coordinate")
        dependencies = tuple(sorted(dependencies, key=lambda item: item.coordinate))

        manifest = cls(
            id=_identifier(raw["id"], label="pack id"),
            publisher=_identifier(raw["publisher"], label="publisher"),
            version=SemVer.parse(raw["version"]),
            description=_non_empty_string(raw["description"], label="description"),
            capabilities=capabilities,
            content_roots=content_roots,
            dependencies=dependencies,
            compatibility=PackCompatibility.from_dict(raw["compatibility"]),
        )
        _canonical_json(manifest.as_dict())
        return manifest

    @classmethod
    def from_json(cls, value: str) -> "PackManifest":
        try:
            raw = json.loads(value)
        except json.JSONDecodeError as exc:
            raise _fail("SDAI-PACK-001", "Pack manifest JSON is malformed") from exc
        return cls.from_dict(raw)


def _reject_symlink_components(root: Path, relative: str, *, label: str) -> Path:
    if root.is_symlink():
        raise _fail("SDAI-PACK-002", f"{label} root must not be a symlink")
    candidate = root
    for part in PurePosixPath(relative).parts:
        candidate = candidate / part
        if candidate.is_symlink():
            raise _fail("SDAI-PACK-002", f"{label} '{relative}' contains a symlink component")
    try:
        return ensure_within_project(root, candidate, label=label)
    except RuntimeError as exc:
        raise _fail("SDAI-PACK-002", f"{label} '{relative}' escapes the Pack root") from exc


def validate_pack_layout(pack_root: Path, manifest: PackManifest) -> None:
    root = pack_root
    if not root.exists() or not root.is_dir():
        raise _fail("SDAI-PACK-002", f"Pack root '{root}' must be an existing directory")
    if root.is_symlink():
        raise _fail("SDAI-PACK-002", "Pack root must not be a symlink")
    root = root.resolve()
    for relative in manifest.content_roots:
        path = _reject_symlink_components(root, relative, label="Pack content root")
        if not path.exists() or not path.is_dir():
            raise _fail("SDAI-PACK-002", f"Pack content root '{relative}' must be an existing directory")


def load_pack_manifest(path: Path, *, pack_root: Path | None = None) -> PackManifest:
    cwd = Path.cwd()
    root_input = pack_root if pack_root is not None else (path.parent if path.is_absolute() else cwd / path.parent)
    if not root_input.is_absolute():
        root_input = cwd / root_input
    manifest_input = path if path.is_absolute() else ((root_input / path) if pack_root is not None else cwd / path)

    if manifest_input.is_symlink():
        raise _fail("SDAI-PACK-002", "Pack manifest must not be a symlink")
    if not manifest_input.is_file():
        raise _fail("SDAI-PACK-001", f"Pack manifest '{manifest_input}' does not exist")
    if root_input.is_symlink():
        raise _fail("SDAI-PACK-002", "Pack root must not be a symlink")

    root_resolved = root_input.resolve()
    try:
        safe_manifest = ensure_within_project(root_resolved, manifest_input, label="Pack manifest")
    except RuntimeError as exc:
        raise _fail("SDAI-PACK-002", "Pack manifest escapes the Pack root") from exc

    relative_manifest = safe_manifest.relative_to(root_resolved).as_posix()
    _reject_symlink_components(root_resolved, relative_manifest, label="Pack manifest")

    try:
        raw: Any = yaml.load(safe_manifest.read_text(encoding="utf-8"), Loader=_UniqueKeyLoader)
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise _fail("SDAI-PACK-001", f"unable to read Pack manifest '{safe_manifest}' as UTF-8 YAML") from exc
    manifest = PackManifest.from_dict(raw)
    validate_pack_layout(root_input, manifest)
    return manifest
