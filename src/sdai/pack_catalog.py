from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from functools import cmp_to_key
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Iterable, Mapping
import unicodedata
from urllib.parse import urlsplit

from sdai.pack_lock import PackCandidate
from sdai.pack_manifest import PackManifest, PackManifestError


PACK_CATALOG_API_VERSION = "sdai.pack-catalog/v1"
PACK_CATALOG_RESOLUTION_API_VERSION = "sdai.pack-catalog-resolution/v1"


class PackCatalogError(RuntimeError):
    pass


_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9]*(?:[-_.][a-z0-9]+)*$")
_HASH_PREFIX = "sha256:"
_CATALOG_KEYS = frozenset({"apiVersion", "id", "source", "entries"})
_ENTRY_KEYS = frozenset({"manifest", "source", "contentSha256"})


def _fail(code: str, message: str) -> PackCatalogError:
    return PackCatalogError(f"{code}: {message}")


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
        raise _fail("SDAI-PACK-CATALOG-001", "catalog is not canonical finite JSON") from exc


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _fail("SDAI-PACK-CATALOG-001", f"catalog JSON contains duplicate key '{key}'")
        result[key] = value
    return result


def _mapping(value: object, *, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise _fail("SDAI-PACK-CATALOG-001", f"{label} must be a string-keyed mapping")
    return value


def _keys(value: Mapping[str, object], *, expected: frozenset[str], label: str) -> None:
    unknown = sorted(set(value) - expected)
    missing = sorted(expected - set(value))
    if unknown:
        raise _fail(
            "SDAI-PACK-CATALOG-001",
            f"{label} contains unsupported field(s): {', '.join(unknown)}",
        )
    if missing:
        raise _fail(
            "SDAI-PACK-CATALOG-001",
            f"{label} is missing required field(s): {', '.join(missing)}",
        )


def _text(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _fail("SDAI-PACK-CATALOG-001", f"{label} must be a non-empty string")
    if "\x00" in value:
        raise _fail("SDAI-PACK-CATALOG-001", f"{label} must not contain NUL")
    return unicodedata.normalize("NFC", value.strip())


def _identifier(value: object, *, label: str) -> str:
    text = _text(value, label=label)
    if not _IDENTIFIER_RE.fullmatch(text):
        raise _fail(
            "SDAI-PACK-CATALOG-001",
            f"{label} '{text}' is not a portable lowercase identifier",
        )
    return text


def _hash(value: object, *, label: str) -> str:
    text = _text(value, label=label)
    if not text.startswith(_HASH_PREFIX):
        raise _fail("SDAI-PACK-CATALOG-001", f"{label} must be a SHA-256 digest")
    digest = text[len(_HASH_PREFIX) :]
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise _fail("SDAI-PACK-CATALOG-001", f"{label} must be a lowercase SHA-256 digest")
    return text


def canonical_catalog_source(value: object) -> str:
    text = _text(value, label="catalog source")
    if any(ord(char) < 32 or char.isspace() for char in text) or "\\" in text:
        raise _fail("SDAI-PACK-CATALOG-001", f"catalog source '{text}' is not portable")
    parsed = urlsplit(text)
    if not parsed.scheme or not re.fullmatch(r"[a-z][a-z0-9+.-]*", parsed.scheme):
        raise _fail("SDAI-PACK-CATALOG-001", f"catalog source '{text}' must use a lowercase URI scheme")
    if parsed.fragment:
        raise _fail("SDAI-PACK-CATALOG-001", f"catalog source '{text}' must not contain a fragment")
    if parsed.username is not None or parsed.password is not None:
        raise _fail("SDAI-PACK-CATALOG-001", "catalog source must not embed credentials")
    if parsed.scheme in {"http", "https", "catalog"} and not parsed.netloc:
        raise _fail("SDAI-PACK-CATALOG-001", f"catalog source '{text}' requires an authority")
    return text


def _candidate_compare(left: "PackCatalogEntry", right: "PackCatalogEntry") -> int:
    if left.coordinate != right.coordinate:
        return -1 if left.coordinate < right.coordinate else 1
    precedence = left.manifest.version.compare_precedence(right.manifest.version)
    if precedence != 0:
        return -precedence
    left_key = (str(left.manifest.version), left.source, left.content_sha256, left.manifest.sha256)
    right_key = (str(right.manifest.version), right.source, right.content_sha256, right.manifest.sha256)
    if left_key == right_key:
        return 0
    return -1 if left_key < right_key else 1


@dataclass(frozen=True)
class PackCatalogEntry:
    manifest: PackManifest
    source: str
    content_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.manifest, PackManifest):
            raise _fail("SDAI-PACK-CATALOG-001", "catalog entry manifest must be a PackManifest")
        object.__setattr__(self, "source", canonical_catalog_source(self.source))
        object.__setattr__(self, "content_sha256", _hash(self.content_sha256, label="catalog entry contentSha256"))

    @property
    def coordinate(self) -> str:
        return self.manifest.coordinate

    @property
    def identity(self) -> str:
        return self.manifest.identity

    def as_dict(self) -> dict[str, object]:
        return {
            "contentSha256": self.content_sha256,
            "manifest": self.manifest.as_dict(),
            "source": self.source,
        }

    def to_candidate(self) -> PackCandidate:
        return PackCandidate(
            manifest=self.manifest,
            source=self.source,
            content_sha256=self.content_sha256,
        )

    @classmethod
    def from_dict(cls, value: object) -> "PackCatalogEntry":
        raw = _mapping(value, label="catalog entry")
        _keys(raw, expected=_ENTRY_KEYS, label="catalog entry")
        try:
            manifest = PackManifest.from_dict(raw["manifest"])
        except PackManifestError as exc:
            raise _fail("SDAI-PACK-CATALOG-001", "catalog entry contains an invalid Pack manifest") from exc
        return cls(
            manifest=manifest,
            source=raw["source"],  # type: ignore[arg-type]
            content_sha256=raw["contentSha256"],  # type: ignore[arg-type]
        )


@dataclass(frozen=True)
class PackCatalog:
    id: str
    source: str
    entries: tuple[PackCatalogEntry, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _identifier(self.id, label="catalog id"))
        object.__setattr__(self, "source", canonical_catalog_source(self.source))
        ordered = tuple(sorted(self.entries, key=cmp_to_key(_candidate_compare)))
        if ordered != self.entries:
            raise _fail("SDAI-PACK-CATALOG-001", "catalog entries must use canonical query order")
        exact_seen: dict[str, PackCatalogEntry] = {}
        for entry in self.entries:
            previous = exact_seen.get(entry.identity)
            if previous is not None:
                if previous.as_dict() == entry.as_dict():
                    raise _fail("SDAI-PACK-CATALOG-002", f"catalog contains duplicate entry '{entry.identity}'")
                raise _fail("SDAI-PACK-CATALOG-002", f"catalog contains conflicting entry '{entry.identity}'")
            exact_seen[entry.identity] = entry

    def as_dict(self) -> dict[str, object]:
        return {
            "apiVersion": PACK_CATALOG_API_VERSION,
            "entries": [entry.as_dict() for entry in self.entries],
            "id": self.id,
            "source": self.source,
        }

    def to_json(self) -> str:
        return _canonical_json(self.as_dict())

    @property
    def sha256(self) -> str:
        return _HASH_PREFIX + sha256(self.to_json().encode("utf-8")).hexdigest()

    @classmethod
    def create(
        cls,
        *,
        id: str,
        source: str,
        entries: Iterable[PackCatalogEntry],
    ) -> "PackCatalog":
        return cls(
            id=id,
            source=source,
            entries=tuple(sorted(tuple(entries), key=cmp_to_key(_candidate_compare))),
        )

    @classmethod
    def from_dict(cls, value: object) -> "PackCatalog":
        raw = _mapping(value, label="Pack catalog")
        _keys(raw, expected=_CATALOG_KEYS, label="Pack catalog")
        if raw["apiVersion"] != PACK_CATALOG_API_VERSION:
            raise _fail(
                "SDAI-PACK-CATALOG-001",
                f"unsupported apiVersion '{raw['apiVersion']}', expected '{PACK_CATALOG_API_VERSION}'",
            )
        entries_raw = raw["entries"]
        if not isinstance(entries_raw, list):
            raise _fail("SDAI-PACK-CATALOG-001", "catalog entries must be a list")
        return cls(
            id=raw["id"],  # type: ignore[arg-type]
            source=raw["source"],  # type: ignore[arg-type]
            entries=tuple(PackCatalogEntry.from_dict(item) for item in entries_raw),
        )

    @classmethod
    def from_json(cls, value: str) -> "PackCatalog":
        try:
            raw = json.loads(value, object_pairs_hook=_unique_json_object)
        except json.JSONDecodeError as exc:
            raise _fail("SDAI-PACK-CATALOG-001", "Pack catalog JSON is malformed") from exc
        return cls.from_dict(raw)

    def search(self, query: str) -> tuple[PackCatalogEntry, ...]:
        needle = unicodedata.normalize("NFC", query.strip()).casefold()
        if not needle:
            return self.entries
        matches: list[PackCatalogEntry] = []
        for entry in self.entries:
            fields = [
                entry.coordinate,
                entry.identity,
                entry.manifest.description,
                *entry.manifest.capabilities,
            ]
            if any(needle in field.casefold() for field in fields):
                matches.append(entry)
        return tuple(matches)

    def info(
        self,
        publisher: str,
        pack_id: str,
        *,
        version: str | None = None,
    ) -> tuple[PackCatalogEntry, ...]:
        coordinate = f"{_identifier(publisher, label='publisher')}/{_identifier(pack_id, label='pack id')}"
        matches = tuple(entry for entry in self.entries if entry.coordinate == coordinate)
        if version is None:
            return matches
        return tuple(entry for entry in matches if str(entry.manifest.version) == version)


class CatalogScope(str, Enum):
    ORGANIZATION = "organization"
    REPOSITORY = "repository"
    USER = "user"


@dataclass(frozen=True)
class ResolvedCatalog:
    catalog: PackCatalog
    provenance: tuple[CatalogScope, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "catalogId": self.catalog.id,
            "catalogSha256": self.catalog.sha256,
            "provenance": [scope.value for scope in self.provenance],
            "source": self.catalog.source,
        }


@dataclass(frozen=True)
class ResolvedCatalogSet:
    catalogs: tuple[ResolvedCatalog, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "apiVersion": PACK_CATALOG_RESOLUTION_API_VERSION,
            "catalogs": [item.as_dict() for item in self.catalogs],
        }

    def to_json(self) -> str:
        return _canonical_json(self.as_dict())

    @property
    def sha256(self) -> str:
        return _HASH_PREFIX + sha256(self.to_json().encode("utf-8")).hexdigest()

    def candidates(self) -> tuple[PackCandidate, ...]:
        exact: dict[str, PackCatalogEntry] = {}
        for resolved in self.catalogs:
            for entry in resolved.catalog.entries:
                previous = exact.get(entry.identity)
                if previous is not None:
                    if previous.as_dict() == entry.as_dict():
                        continue
                    raise _fail(
                        "SDAI-PACK-CATALOG-003",
                        f"resolved catalogs disagree on exact Pack identity '{entry.identity}'",
                    )
                exact[entry.identity] = entry
        return tuple(
            exact[identity].to_candidate()
            for identity in sorted(exact)
        )

    def search(self, query: str) -> tuple[tuple[ResolvedCatalog, PackCatalogEntry], ...]:
        rows: list[tuple[ResolvedCatalog, PackCatalogEntry]] = []
        for resolved in self.catalogs:
            rows.extend((resolved, entry) for entry in resolved.catalog.search(query))
        return tuple(rows)


def resolve_pack_catalogs(
    *,
    organization: Iterable[PackCatalog] = (),
    repository: Iterable[PackCatalog] = (),
    user: Iterable[PackCatalog] = (),
) -> ResolvedCatalogSet:
    by_source: dict[str, tuple[PackCatalog, set[CatalogScope]]] = {}
    by_id: dict[str, str] = {}
    layers = (
        (CatalogScope.ORGANIZATION, tuple(organization)),
        (CatalogScope.REPOSITORY, tuple(repository)),
        (CatalogScope.USER, tuple(user)),
    )
    for scope, catalogs in layers:
        for catalog in catalogs:
            existing_source = by_id.get(catalog.id)
            if existing_source is not None and existing_source != catalog.source:
                raise _fail(
                    "SDAI-PACK-CATALOG-003",
                    f"catalog id '{catalog.id}' is bound to conflicting sources",
                )
            by_id[catalog.id] = catalog.source
            previous = by_source.get(catalog.source)
            if previous is None:
                by_source[catalog.source] = (catalog, {scope})
                continue
            prior_catalog, provenance = previous
            if prior_catalog.id != catalog.id or prior_catalog.sha256 != catalog.sha256:
                raise _fail(
                    "SDAI-PACK-CATALOG-003",
                    f"catalog source '{catalog.source}' has conflicting identities or bytes across scopes",
                )
            provenance.add(scope)
    resolved = tuple(
        ResolvedCatalog(
            catalog=catalog,
            provenance=tuple(scope for scope in CatalogScope if scope in provenance),
        )
        for _, (catalog, provenance) in sorted(by_source.items())
    )
    return ResolvedCatalogSet(resolved)


def load_pack_catalog(
    path: Path,
    *,
    expected_source: str | None = None,
    expected_sha256: str | None = None,
) -> PackCatalog:
    if path.is_symlink():
        raise _fail("SDAI-PACK-CATALOG-004", "Pack catalog path must not be a symlink")
    if not path.is_file():
        raise _fail("SDAI-PACK-CATALOG-004", f"Pack catalog '{path}' does not exist or is not a file")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise _fail("SDAI-PACK-CATALOG-004", f"unable to read Pack catalog '{path}' as UTF-8") from exc
    catalog = PackCatalog.from_json(text)
    if expected_source is not None:
        expected = canonical_catalog_source(expected_source)
        if catalog.source != expected:
            raise _fail(
                "SDAI-PACK-CATALOG-004",
                f"catalog source mismatch: expected '{expected}', found '{catalog.source}'",
            )
    if expected_sha256 is not None:
        expected_hash = _hash(expected_sha256, label="expected catalog SHA-256")
        if catalog.sha256 != expected_hash:
            raise _fail(
                "SDAI-PACK-CATALOG-004",
                f"catalog integrity mismatch: expected {expected_hash}, found {catalog.sha256}",
            )
    return catalog
