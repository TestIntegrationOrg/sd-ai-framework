from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path, PurePosixPath
import re
from typing import Iterable, Mapping
from urllib.parse import unquote, urlparse

from sdai.architecture_drift import (
    ApprovedArchitecture,
    ArchitectureDriftError,
    ArchitectureFactKind,
    ArchitectureObservation,
    ObservedArchitectureFact,
)
from sdai.architecture_repository import ArchitectureRepositoryIndex
from sdai.trace_graph import TraceProvenance


DATA_OBSERVER_ID = "repository-data"
DATA_MAX_FILE_BYTES = 4 * 1024 * 1024
DATA_MAX_SOURCE_FILES = 100_000
DATA_MAX_HITS_PER_FILE = 10_000

_SOURCE_SUFFIXES = frozenset(
    {
        ".sql",
        ".py",
        ".java",
        ".kt",
        ".kts",
        ".cs",
        ".ts",
        ".tsx",
        ".js",
        ".jsx",
        ".properties",
        ".yaml",
        ".yml",
        ".json",
        ".toml",
        ".conf",
        ".config",
    }
)
_ORM_SUFFIXES = frozenset({".py", ".java", ".kt", ".kts", ".cs", ".ts", ".tsx", ".js", ".jsx"})
_CONFIG_SUFFIXES = frozenset({".properties", ".yaml", ".yml", ".json", ".toml", ".conf", ".config"})
_SAFE_RESOURCE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,511}$")
_SAFE_PART = re.compile(r"^[A-Za-z0-9_$][A-Za-z0-9_$-]{0,127}$")
_IDENTIFIER_PART = r'(?:[A-Za-z_$][A-Za-z0-9_$]*|"(?:[^"]|"")+"|`[^`]+`|\[[^\]]+\])'
_SQL_IDENTIFIER = rf"(?P<resource>{_IDENTIFIER_PART}(?:\s*\.\s*{_IDENTIFIER_PART}){{0,2}})"
_SQL_CREATE = re.compile(rf"^\s*CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?{_SQL_IDENTIFIER}\b", re.IGNORECASE | re.DOTALL)
_SQL_INSERT = re.compile(rf"^\s*INSERT\s+INTO\s+{_SQL_IDENTIFIER}\b", re.IGNORECASE | re.DOTALL)
_SQL_UPDATE = re.compile(rf"^\s*UPDATE\s+{_SQL_IDENTIFIER}\b", re.IGNORECASE | re.DOTALL)
_SQL_DELETE = re.compile(rf"^\s*DELETE\s+FROM\s+{_SQL_IDENTIFIER}\b", re.IGNORECASE | re.DOTALL)
_SQL_MERGE = re.compile(rf"^\s*MERGE\s+INTO\s+{_SQL_IDENTIFIER}\b", re.IGNORECASE | re.DOTALL)
_SQL_ADMIN = re.compile(rf"^\s*(?:ALTER\s+TABLE|DROP\s+TABLE(?:\s+IF\s+EXISTS)?|TRUNCATE\s+TABLE)\s+{_SQL_IDENTIFIER}\b", re.IGNORECASE | re.DOTALL)
_SQL_FROM_JOIN = re.compile(rf"\b(?:FROM|JOIN)\s+{_SQL_IDENTIFIER}\b", re.IGNORECASE)
_SQL_RECOGNIZED = re.compile(
    r"^\s*(?:CREATE\s+TABLE|INSERT\s+INTO|UPDATE\b|DELETE\s+FROM|MERGE\s+INTO|ALTER\s+TABLE|DROP\s+TABLE|TRUNCATE\s+TABLE)",
    re.IGNORECASE,
)
_DB_SCHEMES = frozenset(
    {
        "postgres",
        "postgresql",
        "mysql",
        "mariadb",
        "mongodb",
        "mongodb+srv",
        "redis",
        "rediss",
        "sqlserver",
        "mssql",
        "oracle",
        "sqlite",
    }
)
_SECRET_KEY = re.compile(r"(?:password|passwd|pwd|secret|token|credential|api[-_.]?key|access[-_.]?key|private[-_.]?key)", re.IGNORECASE)
_CONNECTION_KEY = re.compile(r"(?:database|datasource|connection|jdbc|mongo|redis|sql|db)", re.IGNORECASE)
_DYNAMIC_PLACEHOLDER = re.compile(r"^(?:\$\{(?P<brace>[A-Za-z_][A-Za-z0-9_]*)\}|\$\((?P<paren>[A-Za-z_][A-Za-z0-9_]*)\)|%(?P<percent>[A-Za-z_][A-Za-z0-9_]*)%)$")


@dataclass(frozen=True, slots=True)
class _DataHit:
    kind: ArchitectureFactKind
    target: str
    attributes: Mapping[str, object]
    line: int
    detail: str


def _fail(code: str, message: str) -> ArchitectureDriftError:
    return ArchitectureDriftError(f"{code}: {message}")


def _slug(value: str, *, limit: int = 96) -> str:
    normalized = re.sub(r"[^a-z0-9._-]+", "-", value.casefold()).strip("-._") or "resource"
    return normalized[:limit]


def _hashed_identity(prefix: str, value: str) -> str:
    digest = sha256(value.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}:{_slug(value)}:{digest}"


def _unquote_identifier(part: str) -> str:
    value = part.strip()
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        value = value[1:-1].replace('""', '"')
    elif len(value) >= 2 and value[0] == "`" and value[-1] == "`":
        value = value[1:-1]
    elif len(value) >= 2 and value[0] == "[" and value[-1] == "]":
        value = value[1:-1]
    return value


def _resource_name(value: str, *, source: str, line: int) -> str:
    raw_parts = re.split(r"\s*\.\s*", value.strip())
    if not 1 <= len(raw_parts) <= 3:
        raise _fail("SDAI-ARCH-DATA-003", f"data resource identifier is unsupported at {source}:{line}")
    parts: list[str] = []
    for raw in raw_parts:
        part = _unquote_identifier(raw)
        if not part or len(part.encode("utf-8")) > 128 or _SAFE_PART.fullmatch(part) is None:
            raise _fail("SDAI-ARCH-DATA-003", f"data resource identifier is dynamic or unsafe at {source}:{line}")
        parts.append(part.casefold())
    resource = ".".join(parts)
    if len(resource) > 512 or _SAFE_RESOURCE.fullmatch(resource) is None:
        raise _fail("SDAI-ARCH-DATA-003", f"data resource identifier exceeds supported limits at {source}:{line}")
    return resource


def _resource_target(resource: str) -> str:
    return f"data:resource:{resource}"


def _access_hit(resource: str, access: str, line: int, detail: str) -> _DataHit:
    return _DataHit(
        ArchitectureFactKind.DATA_ACCESS,
        _resource_target(resource),
        {"resource": resource, "resourceType": "table", "access": access},
        line,
        detail,
    )


def _ownership_hit(resource: str, line: int, detail: str) -> _DataHit:
    return _DataHit(
        ArchitectureFactKind.DATA_OWNERSHIP,
        _resource_target(resource),
        {"resource": resource, "resourceType": "table"},
        line,
        detail,
    )


def _mask_sql(text: str) -> str:
    """Mask SQL comments and string literals while retaining statement/newline structure."""
    output: list[str] = []
    index = 0
    state = "code"
    while index < len(text):
        char = text[index]
        nxt = text[index + 1] if index + 1 < len(text) else ""
        if state == "code":
            if char == "-" and nxt == "-":
                output.extend((" ", " "))
                index += 2
                state = "line-comment"
                continue
            if char == "/" and nxt == "*":
                output.extend((" ", " "))
                index += 2
                state = "block-comment"
                continue
            if char == "'":
                output.append(" ")
                index += 1
                state = "string"
                continue
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
        # single-quoted SQL string; doubled single quote is an escape
        if char == "'" and nxt == "'":
            output.extend((" ", " "))
            index += 2
            continue
        if char == "'":
            output.append(" ")
            index += 1
            state = "code"
            continue
        output.append("\n" if char == "\n" else " ")
        index += 1
    return "".join(output)


def _sql_statements(text: str) -> tuple[tuple[str, int], ...]:
    cleaned = _mask_sql(text)
    result: list[tuple[str, int]] = []
    start = 0
    line = 1
    start_line = 1
    for index, char in enumerate(cleaned):
        if char == ";":
            statement = cleaned[start:index].strip()
            if statement:
                result.append((statement, start_line))
            segment = cleaned[start : index + 1]
            line += segment.count("\n")
            start = index + 1
            start_line = line
    tail = cleaned[start:].strip()
    if tail:
        result.append((tail, start_line))
    return tuple(result)


def _sql_hits(text: str, *, source: str) -> tuple[_DataHit, ...]:
    hits: list[_DataHit] = []
    for statement, line in _sql_statements(text):
        create = _SQL_CREATE.match(statement)
        if create is not None:
            resource = _resource_name(create.group("resource"), source=source, line=line)
            hits.append(_ownership_hit(resource, line, "SQL table ownership declaration"))
            hits.append(_access_hit(resource, "admin", line, "SQL table administration"))
            continue

        matched_command = False
        for pattern, access, detail in (
            (_SQL_INSERT, "write", "SQL INSERT access"),
            (_SQL_UPDATE, "write", "SQL UPDATE access"),
            (_SQL_DELETE, "write", "SQL DELETE access"),
            (_SQL_MERGE, "write", "SQL MERGE access"),
            (_SQL_ADMIN, "admin", "SQL schema administration"),
        ):
            match = pattern.match(statement)
            if match is None:
                continue
            matched_command = True
            resource = _resource_name(match.group("resource"), source=source, line=line)
            hits.append(_access_hit(resource, access, line, detail))
            break
        if not matched_command and _SQL_RECOGNIZED.match(statement):
            raise _fail(
                "SDAI-ARCH-DATA-003",
                f"recognized SQL data operation has a dynamic or unsupported resource at {source}:{line}",
            )

        if re.search(r"\b(?:SELECT|WITH)\b", statement, re.IGNORECASE):
            for match in _SQL_FROM_JOIN.finditer(statement):
                resource = _resource_name(match.group("resource"), source=source, line=line)
                hits.append(_access_hit(resource, "read", line, "SQL read access"))

    return _bounded(hits, source=source)


def _strip_c_comments(text: str) -> str:
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
                state = "string"
                quote = char
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


def _orm_hits(path: Path, text: str, *, source: str) -> tuple[_DataHit, ...]:
    suffix = path.suffix.casefold()
    hits: list[_DataHit] = []
    if suffix == ".py":
        pattern = re.compile(r"^\s*(?:__tablename__|db_table)\s*=\s*[\"'](?P<table>[A-Za-z0-9_$.-]+)[\"']")
        for line_number, line in enumerate(text.splitlines(), start=1):
            match = pattern.match(line)
            if match is not None:
                resource = _resource_name(match.group("table"), source=source, line=line_number)
                hits.extend(
                    (
                        _access_hit(resource, "read", line_number, "Python ORM entity mapping"),
                        _access_hit(resource, "write", line_number, "Python ORM entity mapping"),
                    )
                )
    else:
        cleaned = _strip_c_comments(text)
        patterns: tuple[re.Pattern[str], ...]
        if suffix in {".java", ".kt", ".kts"}:
            patterns = (
                re.compile(r"@Table\s*\(\s*(?:name\s*=\s*)?[\"'](?P<table>[A-Za-z0-9_$.-]+)[\"']", re.IGNORECASE),
            )
        elif suffix == ".cs":
            patterns = (
                re.compile(r"\[Table\s*\(\s*[\"'](?P<table>[A-Za-z0-9_$.-]+)[\"']", re.IGNORECASE),
            )
        elif suffix in {".ts", ".tsx", ".js", ".jsx"}:
            patterns = (
                re.compile(r"@Entity\s*\(\s*[\"'](?P<table>[A-Za-z0-9_$.-]+)[\"']", re.IGNORECASE),
            )
        else:
            patterns = ()
        for line_number, line in enumerate(cleaned.splitlines(), start=1):
            for pattern in patterns:
                match = pattern.search(line)
                if match is None:
                    continue
                resource = _resource_name(match.group("table"), source=source, line=line_number)
                hits.extend(
                    (
                        _access_hit(resource, "read", line_number, "ORM entity mapping"),
                        _access_hit(resource, "write", line_number, "ORM entity mapping"),
                    )
                )
    return _bounded(hits, source=source)


def _clean_config_scalar(value: str) -> str:
    result = value.strip().rstrip(",").strip()
    if len(result) >= 2 and result[0] == result[-1] and result[0] in {"'", '"'}:
        result = result[1:-1]
    return result.strip()


def _config_pairs(text: str) -> tuple[tuple[str, str, int], ...]:
    result: list[tuple[str, str, int]] = []
    pattern = re.compile(r"^\s*[\"']?(?P<key>[A-Za-z0-9_.:-]+)[\"']?\s*[:=]\s*(?P<value>.*?)\s*$")
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith(("#", "//", ";")):
            continue
        match = pattern.match(raw_line)
        if match is None:
            continue
        key = match.group("key")
        value = _clean_config_scalar(match.group("value"))
        result.append((key, value, line_number))
    return tuple(result)


def _safe_store_part(value: str, *, fallback: str) -> str:
    normalized = value.casefold().strip().rstrip(".")
    if re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,127}", normalized):
        return normalized
    return _hashed_identity(fallback, normalized).split(":", 1)[1]


def _store_hit(
    *,
    kind: str,
    host: str | None,
    port: int | None,
    database: str | None,
    line: int,
    detail: str,
) -> _DataHit:
    safe_kind = _slug(kind, limit=32)
    safe_host = _safe_store_part(host or "local", fallback="host")
    safe_database = _safe_store_part(database or "default", fallback="db")
    port_text = str(port) if port is not None else "default"
    target = f"data:store:{safe_kind}:{safe_host}:{port_text}:{safe_database}"
    attributes: dict[str, object] = {
        "resourceType": "store",
        "storeKind": safe_kind,
        "host": safe_host,
        "database": safe_database,
        "access": "connect",
    }
    if port is not None:
        attributes["port"] = port
    return _DataHit(ArchitectureFactKind.DATA_ACCESS, target, attributes, line, detail)


def _dynamic_store_hit(name: str, *, line: int) -> _DataHit:
    safe_name = _slug(name, limit=64)
    digest = sha256(name.casefold().encode("utf-8")).hexdigest()[:16]
    return _DataHit(
        ArchitectureFactKind.DATA_ACCESS,
        f"data:store:dynamic:{safe_name}:{digest}",
        {"resourceType": "store", "storeKind": "dynamic", "reference": safe_name, "access": "connect"},
        line,
        "dynamic data-store configuration reference",
    )


def _connection_string_hit(value: str, *, line: int) -> _DataHit | None:
    pairs: dict[str, str] = {}
    for segment in value.split(";"):
        if "=" not in segment:
            continue
        key, item = segment.split("=", 1)
        normalized = key.strip().casefold().replace(" ", "")
        if _SECRET_KEY.search(normalized):
            continue
        pairs[normalized] = item.strip()
    host = pairs.get("server") or pairs.get("datasource") or pairs.get("host") or pairs.get("address")
    database = pairs.get("database") or pairs.get("initialcatalog") or pairs.get("databasename")
    if host is None and database is None:
        return None
    if host and host.startswith("tcp:"):
        host = host[4:]
    port: int | None = None
    if host and "," in host:
        host_part, port_part = host.rsplit(",", 1)
        if port_part.isdigit():
            host = host_part
            port = int(port_part)
    return _store_hit(kind="database", host=host, port=port, database=database, line=line, detail="database connection configuration")


def _uri_store_hit(value: str, *, line: int) -> _DataHit | None:
    text = value.strip()
    if text.casefold().startswith("jdbc:"):
        text = text[5:]
    scheme = text.split(":", 1)[0].casefold() if ":" in text else ""
    if scheme not in _DB_SCHEMES:
        return None

    if scheme == "sqlite":
        leaf = Path(text.split(":", 1)[1].split("?", 1)[0]).name or "local"
        database = leaf.rsplit(".", 1)[0] if "." in leaf else leaf
        return _store_hit(kind="sqlite", host="local", port=None, database=database, line=line, detail="database URI configuration")

    if scheme in {"sqlserver", "mssql"} and ";" in text:
        head, tail = text.split(";", 1)
        parsed = urlparse(head)
        tail_hit = _connection_string_hit(tail, line=line)
        database = None
        if tail_hit is not None:
            database_value = tail_hit.attributes.get("database")
            if isinstance(database_value, str):
                database = database_value
        return _store_hit(
            kind=scheme,
            host=parsed.hostname,
            port=parsed.port,
            database=database,
            line=line,
            detail="database URI configuration",
        )

    parsed = urlparse(text)
    if not parsed.scheme:
        return None
    database = unquote(parsed.path.lstrip("/").split("/", 1)[0]) if parsed.path else None
    return _store_hit(
        kind=scheme,
        host=parsed.hostname,
        port=parsed.port,
        database=database,
        line=line,
        detail="database URI configuration",
    )


def _config_hits(text: str, *, source: str) -> tuple[_DataHit, ...]:
    hits: list[_DataHit] = []
    for key, value, line in _config_pairs(text):
        if _SECRET_KEY.search(key):
            continue
        placeholder = _DYNAMIC_PLACEHOLDER.fullmatch(value)
        if placeholder is not None:
            name = next(item for item in placeholder.groups() if item is not None)
            if _CONNECTION_KEY.search(key) or _CONNECTION_KEY.search(name):
                hits.append(_dynamic_store_hit(name, line=line))
            continue
        uri_hit = _uri_store_hit(value, line=line)
        if uri_hit is not None:
            hits.append(uri_hit)
            continue
        if _CONNECTION_KEY.search(key) and ";" in value and "=" in value:
            connection_hit = _connection_string_hit(value, line=line)
            if connection_hit is not None:
                hits.append(connection_hit)
    return _bounded(hits, source=source)


def _bounded(values: Iterable[_DataHit], *, source: str) -> tuple[_DataHit, ...]:
    result = tuple(values)
    if len(result) > DATA_MAX_HITS_PER_FILE:
        raise _fail(
            "SDAI-ARCH-DATA-003",
            f"data source exceeds {DATA_MAX_HITS_PER_FILE} observable data sites: {source}",
        )
    return result


def _hit_fact(component: str, hit: _DataHit, *, source: str) -> ObservedArchitectureFact:
    return ObservedArchitectureFact(
        kind=hit.kind,
        source=component,
        target=hit.target,
        attributes=hit.attributes,
        provenance=(TraceProvenance(source, hit.line, detail=hit.detail),),
    )


def _aggregate(values: Iterable[ObservedArchitectureFact]) -> tuple[ObservedArchitectureFact, ...]:
    grouped: dict[str, list[ObservedArchitectureFact]] = {}
    for item in values:
        grouped.setdefault(item.semantic_key, []).append(item)
    result: list[ObservedArchitectureFact] = []
    for key in sorted(grouped):
        items = grouped[key]
        first = items[0]
        provenance: dict[tuple[str, int], TraceProvenance] = {}
        for item in items:
            for source in item.provenance:
                existing = provenance.get(source.location)
                if existing is None or (source.detail or "") < (existing.detail or ""):
                    provenance[source.location] = source
        result.append(
            ObservedArchitectureFact(
                kind=first.kind,
                source=first.source,
                target=first.target,
                attributes=first.attributes,
                provenance=tuple(
                    sorted(
                        provenance.values(),
                        key=lambda item: (item.source.casefold(), item.source, item.line, item.detail or ""),
                    )
                ),
            )
        )
    return tuple(result)


class RepositoryDataObserver:
    """Observe repository-declared data ownership/access without connecting to data stores."""

    observer_id = DATA_OBSERVER_ID

    def observe(self, project_root: Path, approved: ApprovedArchitecture) -> ArchitectureObservation:
        if not isinstance(approved, ApprovedArchitecture):
            raise _fail("SDAI-ARCH-DATA-001", "data observation requires approved architecture truth")
        repository = ArchitectureRepositoryIndex(project_root, approved.topology.components)
        facts: list[ObservedArchitectureFact] = []
        for path in repository.source_files(_SOURCE_SUFFIXES, maximum=DATA_MAX_SOURCE_FILES):
            relative = PurePosixPath(path.relative_to(repository.root).as_posix())
            component = repository.owner_for_relative_path(relative)
            if component is None:
                continue
            source = relative.as_posix()
            text = repository.read_utf8(path, maximum_bytes=DATA_MAX_FILE_BYTES, label="data architecture source")
            suffix = path.suffix.casefold()
            hits: list[_DataHit] = []
            if suffix == ".sql":
                hits.extend(_sql_hits(text, source=source))
            if suffix in _ORM_SUFFIXES:
                hits.extend(_orm_hits(path, text, source=source))
            if suffix in _CONFIG_SUFFIXES:
                hits.extend(_config_hits(text, source=source))
            if len(hits) > DATA_MAX_HITS_PER_FILE:
                raise _fail(
                    "SDAI-ARCH-DATA-003",
                    f"data source exceeds {DATA_MAX_HITS_PER_FILE} observable data sites: {source}",
                )
            facts.extend(_hit_fact(component, hit, source=source) for hit in hits)
        return ArchitectureObservation(self.observer_id, _aggregate(facts))


__all__ = [
    "DATA_OBSERVER_ID",
    "RepositoryDataObserver",
]
