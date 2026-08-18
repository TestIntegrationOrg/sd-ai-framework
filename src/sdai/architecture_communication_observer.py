from __future__ import annotations

import ast
from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path, PurePosixPath
import re
from typing import Iterable, Mapping
from urllib.parse import urlparse

from sdai.architecture_drift import (
    ApprovedArchitecture,
    ArchitectureDriftError,
    ArchitectureFact,
    ArchitectureFactKind,
    ArchitectureObservation,
    ObservedArchitectureFact,
)
from sdai.architecture_repository import ArchitectureRepositoryIndex
from sdai.contract_trace import ContractTraceError, build_contract_trace_index
from sdai.trace_graph import TraceProvenance


COMMUNICATION_OBSERVER_ID = "repository-communications"
COMMUNICATION_MAX_FILE_BYTES = 4 * 1024 * 1024
COMMUNICATION_MAX_SOURCE_FILES = 100_000
COMMUNICATION_MAX_HITS_PER_FILE = 10_000

_SOURCE_SUFFIXES = frozenset(
    {
        ".py",
        ".java",
        ".kt",
        ".kts",
        ".cs",
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
_HTTP_METHODS = frozenset({"DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT", "TRACE"})
_SERVER_METHOD_NAMES = frozenset(method.casefold() for method in _HTTP_METHODS)
_CLIENT_METHOD_NAMES = _SERVER_METHOD_NAMES


@dataclass(frozen=True, slots=True)
class _Hit:
    protocol: str
    direction: str
    line: int
    method: str | None = None
    endpoint: str | None = None
    host: str | None = None
    action: str | None = None
    channel: str | None = None


def _fail(code: str, message: str) -> ArchitectureDriftError:
    return ArchitectureDriftError(f"{code}: {message}")


def _strip_c_family_comments(text: str) -> str:
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


def _strip_powershell_comment(line: str) -> str:
    quote: str | None = None
    escaped = False
    for index, char in enumerate(line):
        if escaped:
            escaped = False
            continue
        if char == "`":
            escaped = True
            continue
        if quote is None and char in {"'", '"'}:
            quote = char
        elif quote == char:
            quote = None
        elif quote is None and char == "#":
            return line[:index]
    return line


def _bounded(hits: Iterable[_Hit], *, source: str) -> tuple[_Hit, ...]:
    result = tuple(hits)
    if len(result) > COMMUNICATION_MAX_HITS_PER_FILE:
        raise _fail(
            "SDAI-ARCH-COMM-003",
            f"communication source exceeds {COMMUNICATION_MAX_HITS_PER_FILE} observable communication sites: {source}",
        )
    return result


def _literal_string(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _call_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _call_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return None


def _normalize_server_path(value: str, *, source: str, line: int) -> str:
    path = value.strip()
    if not path.startswith("/") or "\x00" in path or "\\" in path:
        raise _fail(
            "SDAI-ARCH-COMM-004",
            f"HTTP server endpoint must be a literal absolute route path at {source}:{line}: {value!r}",
        )
    if len(path) > 2048:
        raise _fail("SDAI-ARCH-COMM-004", f"HTTP server endpoint is too long at {source}:{line}")
    return path


def _normalize_url(value: str, *, source: str, line: int) -> tuple[str, str, str]:
    text = value.strip()
    parsed = urlparse(text)
    scheme = parsed.scheme.casefold()
    if scheme not in {"http", "https"} or not parsed.hostname:
        raise _fail(
            "SDAI-ARCH-COMM-004",
            f"outbound HTTP target must be a literal http/https URL at {source}:{line}: {value!r}",
        )
    host = parsed.hostname.casefold().rstrip(".")
    port = parsed.port
    authority = f"{host}:{port}" if port is not None else host
    endpoint = parsed.path or "/"
    if parsed.query:
        endpoint += "?" + parsed.query
    if len(authority) > 512 or len(endpoint) > 4096:
        raise _fail("SDAI-ARCH-COMM-004", f"outbound HTTP target exceeds limits at {source}:{line}")
    return scheme, authority, endpoint


def _python_hits(text: str, *, source: str) -> tuple[_Hit, ...]:
    try:
        tree = ast.parse(text, filename=source, mode="exec")
    except SyntaxError as exc:
        raise _fail(
            "SDAI-ARCH-COMM-003",
            f"unable to parse Python communication source {source}:{exc.lineno or 1}: {exc.msg}",
        ) from exc
    hits: list[_Hit] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for decorator in node.decorator_list:
                if not isinstance(decorator, ast.Call):
                    continue
                name = (_call_name(decorator.func) or "").casefold()
                method = name.rsplit(".", 1)[-1]
                if method in _SERVER_METHOD_NAMES:
                    literal = _literal_string(decorator.args[0]) if decorator.args else None
                    if literal is None:
                        raise _fail(
                            "SDAI-ARCH-COMM-003",
                            f"dynamic Python HTTP route cannot be resolved deterministically at {source}:{decorator.lineno}",
                        )
                    hits.append(
                        _Hit(
                            "http",
                            "inbound",
                            decorator.lineno,
                            method=method.upper(),
                            endpoint=_normalize_server_path(literal, source=source, line=decorator.lineno),
                        )
                    )
                elif method == "route":
                    literal = _literal_string(decorator.args[0]) if decorator.args else None
                    if literal is None:
                        raise _fail(
                            "SDAI-ARCH-COMM-003",
                            f"dynamic Python HTTP route cannot be resolved deterministically at {source}:{decorator.lineno}",
                        )
                    methods: list[str] = []
                    for keyword in decorator.keywords:
                        if keyword.arg != "methods":
                            continue
                        if not isinstance(keyword.value, (ast.List, ast.Tuple)):
                            raise _fail(
                                "SDAI-ARCH-COMM-003",
                                f"dynamic Flask route methods cannot be resolved at {source}:{decorator.lineno}",
                            )
                        for item in keyword.value.elts:
                            value = _literal_string(item)
                            if value is None or value.upper() not in _HTTP_METHODS:
                                raise _fail(
                                    "SDAI-ARCH-COMM-003",
                                    f"unsupported Flask route method at {source}:{decorator.lineno}",
                                )
                            methods.append(value.upper())
                    for http_method in sorted(set(methods or ["GET"])):
                        hits.append(
                            _Hit(
                                "http",
                                "inbound",
                                decorator.lineno,
                                method=http_method,
                                endpoint=_normalize_server_path(literal, source=source, line=decorator.lineno),
                            )
                        )
        if isinstance(node, ast.Call):
            name = (_call_name(node.func) or "").casefold()
            method = name.rsplit(".", 1)[-1]
            recognized_client = (
                method in _CLIENT_METHOD_NAMES
                and any(token in name for token in ("requests.", "httpx.", "session.", "client."))
            )
            if recognized_client:
                literal = _literal_string(node.args[0]) if node.args else None
                if literal is None:
                    raise _fail(
                        "SDAI-ARCH-COMM-003",
                        f"dynamic Python HTTP client URL cannot be resolved deterministically at {source}:{node.lineno}",
                    )
                scheme, host, endpoint = _normalize_url(literal, source=source, line=node.lineno)
                hits.append(
                    _Hit(
                        scheme,
                        "outbound",
                        node.lineno,
                        method=method.upper(),
                        endpoint=endpoint,
                        host=host,
                    )
                )
            if name.endswith((".send", ".publish")) and node.args:
                literal = _literal_string(node.args[0])
                if literal is not None:
                    hits.append(
                        _Hit(
                            "event",
                            "outbound",
                            node.lineno,
                            action="publish",
                            channel=_normalize_channel(literal, source=source, line=node.lineno),
                        )
                    )
    return _bounded(hits, source=source)


def _normalize_channel(value: str, *, source: str, line: int) -> str:
    channel = value.strip()
    if not channel or len(channel) > 1024 or "\x00" in channel or any(ord(char) < 32 for char in channel):
        raise _fail("SDAI-ARCH-COMM-004", f"invalid literal event channel at {source}:{line}")
    return channel


def _java_kotlin_hits(text: str, *, source: str) -> tuple[_Hit, ...]:
    cleaned = _strip_c_family_comments(text)
    hits: list[_Hit] = []
    mapping = {
        "DeleteMapping": "DELETE",
        "GetMapping": "GET",
        "PatchMapping": "PATCH",
        "PostMapping": "POST",
        "PutMapping": "PUT",
    }
    annotation = re.compile(
        r"@(?P<name>DeleteMapping|GetMapping|PatchMapping|PostMapping|PutMapping)\s*\(\s*(?:value\s*=\s*)?[\"'](?P<path>[^\"']+)[\"']",
    )
    dynamic_annotation = re.compile(r"@(DeleteMapping|GetMapping|PatchMapping|PostMapping|PutMapping)\s*\(")
    event_send = re.compile(r"\b(?:kafkaTemplate\.send|rabbitTemplate\.convertAndSend|mqttTemplate\.send)\s*\(\s*[\"'](?P<channel>[^\"']+)[\"']")
    for line_number, line in enumerate(cleaned.splitlines(), start=1):
        match = annotation.search(line)
        if match is not None:
            hits.append(
                _Hit(
                    "http",
                    "inbound",
                    line_number,
                    method=mapping[match.group("name")],
                    endpoint=_normalize_server_path(match.group("path"), source=source, line=line_number),
                )
            )
        elif dynamic_annotation.search(line):
            raise _fail(
                "SDAI-ARCH-COMM-003",
                f"dynamic Spring route cannot be resolved deterministically at {source}:{line_number}",
            )
        for event in event_send.finditer(line):
            hits.append(
                _Hit(
                    "event",
                    "outbound",
                    line_number,
                    action="publish",
                    channel=_normalize_channel(event.group("channel"), source=source, line=line_number),
                )
            )
    return _bounded(hits, source=source)


def _dotnet_hits(text: str, *, source: str) -> tuple[_Hit, ...]:
    cleaned = _strip_c_family_comments(text)
    hits: list[_Hit] = []
    attribute = re.compile(r"\[Http(?P<method>Delete|Get|Head|Options|Patch|Post|Put)\s*\(\s*[\"'](?P<path>[^\"']+)[\"']")
    minimal = re.compile(r"\b(?:app|routes|endpoints)\.Map(?P<method>Delete|Get|Patch|Post|Put)\s*\(\s*[\"'](?P<path>[^\"']+)[\"']")
    dynamic = re.compile(r"(?:\[Http(?:Delete|Get|Head|Options|Patch|Post|Put)\s*\(|\.Map(?:Delete|Get|Patch|Post|Put)\s*\()")
    client = re.compile(r"\b(?P<name>GetAsync|DeleteAsync|PostAsync|PutAsync|PatchAsync)\s*\(\s*[\"'](?P<url>https?://[^\"']+)[\"']")
    for line_number, line in enumerate(cleaned.splitlines(), start=1):
        matched_route = False
        for pattern in (attribute, minimal):
            match = pattern.search(line)
            if match is None:
                continue
            matched_route = True
            hits.append(
                _Hit(
                    "http",
                    "inbound",
                    line_number,
                    method=match.group("method").upper(),
                    endpoint=_normalize_server_path(match.group("path"), source=source, line=line_number),
                )
            )
        if not matched_route and dynamic.search(line):
            raise _fail(
                "SDAI-ARCH-COMM-003",
                f"dynamic .NET HTTP route cannot be resolved deterministically at {source}:{line_number}",
            )
        for match in client.finditer(line):
            method = match.group("name").removesuffix("Async").removesuffix("Async").replace("Get", "GET").replace("Post", "POST").replace("Put", "PUT").replace("Delete", "DELETE").replace("Patch", "PATCH")
            scheme, host, endpoint = _normalize_url(match.group("url"), source=source, line=line_number)
            hits.append(_Hit(scheme, "outbound", line_number, method=method, endpoint=endpoint, host=host))
    return _bounded(hits, source=source)


def _outside_string(line: str, position: int) -> bool:
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


def _javascript_hits(text: str, *, source: str) -> tuple[_Hit, ...]:
    cleaned = _strip_c_family_comments(text)
    hits: list[_Hit] = []
    server = re.compile(
        r"\b(?:app|router|server)\.(?P<method>delete|get|head|options|patch|post|put)\s*\(\s*[\"'](?P<path>/[^\"']*)[\"']",
        re.IGNORECASE,
    )
    dynamic_server = re.compile(r"\b(?:app|router|server)\.(?:delete|get|head|options|patch|post|put)\s*\(", re.IGNORECASE)
    fetch = re.compile(r"\bfetch\s*\(\s*[\"'](?P<url>https?://[^\"']+)[\"']", re.IGNORECASE)
    axios = re.compile(r"\baxios\.(?P<method>delete|get|head|options|patch|post|put)\s*\(\s*[\"'](?P<url>https?://[^\"']+)[\"']", re.IGNORECASE)
    dynamic_client = re.compile(r"\b(?:fetch\s*\(|axios\.(?:delete|get|head|options|patch|post|put)\s*\()", re.IGNORECASE)
    event = re.compile(r"\b(?:producer\.)?(?P<action>publish|send)\s*\(\s*[\"'](?P<channel>[^\"']+)[\"']", re.IGNORECASE)
    for line_number, line in enumerate(cleaned.splitlines(), start=1):
        literal_starts: set[int] = set()
        for match in server.finditer(line):
            if not _outside_string(line, match.start()):
                continue
            literal_starts.add(match.start())
            hits.append(
                _Hit(
                    "http",
                    "inbound",
                    line_number,
                    method=match.group("method").upper(),
                    endpoint=_normalize_server_path(match.group("path"), source=source, line=line_number),
                )
            )
        fetch_match = fetch.search(line)
        if fetch_match is not None and _outside_string(line, fetch_match.start()):
            literal_starts.add(fetch_match.start())
            scheme, host, endpoint = _normalize_url(fetch_match.group("url"), source=source, line=line_number)
            hits.append(_Hit(scheme, "outbound", line_number, method="GET", endpoint=endpoint, host=host))
        for match in axios.finditer(line):
            if not _outside_string(line, match.start()):
                continue
            literal_starts.add(match.start())
            scheme, host, endpoint = _normalize_url(match.group("url"), source=source, line=line_number)
            hits.append(
                _Hit(
                    scheme,
                    "outbound",
                    line_number,
                    method=match.group("method").upper(),
                    endpoint=endpoint,
                    host=host,
                )
            )
        for match in dynamic_server.finditer(line):
            if not _outside_string(line, match.start()) or match.start() in literal_starts:
                continue
            remainder = line[match.end() :].lstrip()
            if not remainder.startswith(("'", '"')):
                raise _fail(
                    "SDAI-ARCH-COMM-003",
                    f"dynamic JavaScript/TypeScript HTTP route cannot be resolved at {source}:{line_number}",
                )
        for match in dynamic_client.finditer(line):
            if not _outside_string(line, match.start()) or match.start() in literal_starts:
                continue
            remainder = line[match.end() :].lstrip()
            if not remainder.startswith(("'", '"')):
                raise _fail(
                    "SDAI-ARCH-COMM-003",
                    f"dynamic JavaScript/TypeScript HTTP target cannot be resolved at {source}:{line_number}",
                )
        for match in event.finditer(line):
            if _outside_string(line, match.start()):
                hits.append(
                    _Hit(
                        "event",
                        "outbound",
                        line_number,
                        action="publish",
                        channel=_normalize_channel(match.group("channel"), source=source, line=line_number),
                    )
                )
    return _bounded(hits, source=source)


def _go_hits(text: str, *, source: str) -> tuple[_Hit, ...]:
    cleaned = _strip_c_family_comments(text)
    hits: list[_Hit] = []
    server = re.compile(r"\b(?:http\.)?HandleFunc\s*\(\s*[\"'](?P<path>/[^\"']*)[\"']")
    client = re.compile(r"\bhttp\.(?P<method>Get|Post|Head)\s*\(\s*[\"'](?P<url>https?://[^\"']+)[\"']")
    for line_number, line in enumerate(cleaned.splitlines(), start=1):
        for match in server.finditer(line):
            hits.append(
                _Hit(
                    "http",
                    "inbound",
                    line_number,
                    method="ANY",
                    endpoint=_normalize_server_path(match.group("path"), source=source, line=line_number),
                )
            )
        for match in client.finditer(line):
            scheme, host, endpoint = _normalize_url(match.group("url"), source=source, line=line_number)
            hits.append(_Hit(scheme, "outbound", line_number, method=match.group("method").upper(), endpoint=endpoint, host=host))
    return _bounded(hits, source=source)


def _powershell_hits(text: str, *, source: str) -> tuple[_Hit, ...]:
    hits: list[_Hit] = []
    command = re.compile(r"\bInvoke-(?:RestMethod|WebRequest)\b", re.IGNORECASE)
    uri = re.compile(r"-(?:Uri|Url)\s+(?P<quote>[\"'])(?P<url>https?://.*?)(?P=quote)", re.IGNORECASE)
    method = re.compile(r"-Method\s+(?P<method>Delete|Get|Head|Options|Patch|Post|Put)\b", re.IGNORECASE)
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = _strip_powershell_comment(raw_line)
        if command.search(line) is None:
            continue
        uri_match = uri.search(line)
        if uri_match is None:
            raise _fail(
                "SDAI-ARCH-COMM-003",
                f"dynamic PowerShell HTTP target cannot be resolved deterministically at {source}:{line_number}",
            )
        method_match = method.search(line)
        http_method = method_match.group("method").upper() if method_match is not None else "GET"
        scheme, host, endpoint = _normalize_url(uri_match.group("url"), source=source, line=line_number)
        hits.append(_Hit(scheme, "outbound", line_number, method=http_method, endpoint=endpoint, host=host))
    return _bounded(hits, source=source)


def _hits_for(path: Path, text: str, *, source: str) -> tuple[_Hit, ...]:
    suffix = path.suffix.casefold()
    if suffix == ".py":
        return _python_hits(text, source=source)
    if suffix in {".java", ".kt", ".kts"}:
        return _java_kotlin_hits(text, source=source)
    if suffix == ".cs":
        return _dotnet_hits(text, source=source)
    if suffix in {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"}:
        return _javascript_hits(text, source=source)
    if suffix == ".go":
        return _go_hits(text, source=source)
    if suffix in {".ps1", ".psm1"}:
        return _powershell_hits(text, source=source)
    return ()


def _slug(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._@+\-]+", "-", value).strip("-") or "target"
    return normalized[:80]


def _external_identity(protocol: str, value: str) -> str:
    digest = sha256(value.encode("utf-8")).hexdigest()[:16]
    return f"external:{protocol}:{_slug(value)}:{digest}"


def _fact_attributes(fact: ArchitectureFact) -> dict[str, object]:
    return json.loads(
        json.dumps(
            dict(fact.attributes),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    )


def _communication_aliases(approved: ApprovedArchitecture) -> tuple[dict[str, str], dict[str, str]]:
    hosts: dict[str, str] = {}
    channels: dict[str, str] = {}
    for fact in approved.topology.facts:
        if fact.kind is not ArchitectureFactKind.COMMUNICATION:
            continue
        attributes = _fact_attributes(fact)
        host = attributes.get("host")
        channel = attributes.get("channel")
        if isinstance(host, str) and host:
            key = host.casefold().rstrip(".")
            previous = hosts.get(key)
            if previous is not None and previous != fact.target:
                raise _fail(
                    "SDAI-ARCH-COMM-002",
                    f"approved communication host alias {host!r} maps to multiple targets: {previous!r}, {fact.target!r}",
                )
            hosts[key] = fact.target
        if isinstance(channel, str) and channel:
            previous = channels.get(channel)
            if previous is not None and previous != fact.target:
                raise _fail(
                    "SDAI-ARCH-COMM-002",
                    f"approved event channel alias {channel!r} maps to multiple targets: {previous!r}, {fact.target!r}",
                )
            channels[channel] = fact.target
    return hosts, channels


def _hit_fact(
    component: str,
    hit: _Hit,
    *,
    source: str,
    host_aliases: Mapping[str, str],
    channel_aliases: Mapping[str, str],
) -> ObservedArchitectureFact:
    attributes: dict[str, object]
    if hit.protocol in {"http", "https"}:
        if hit.direction == "inbound":
            target = "endpoint:http"
            attributes = {
                "direction": "inbound",
                "protocol": "http",
                "method": hit.method,
                "endpoint": hit.endpoint,
            }
        else:
            assert hit.host is not None
            target = host_aliases.get(hit.host.casefold().rstrip(".")) or _external_identity("http", hit.host)
            attributes = {
                "direction": "outbound",
                "protocol": "http",
                "method": hit.method,
                "endpoint": hit.endpoint,
                "host": hit.host,
                "transport": hit.protocol,
            }
    else:
        assert hit.channel is not None
        target = channel_aliases.get(hit.channel) or _external_identity("event", hit.channel)
        attributes = {
            "direction": hit.direction,
            "protocol": "event",
            "action": hit.action,
            "channel": hit.channel,
        }
    return ObservedArchitectureFact(
        kind=ArchitectureFactKind.COMMUNICATION,
        source=component,
        target=target,
        attributes=attributes,
        provenance=(
            TraceProvenance(
                source,
                hit.line,
                detail=(
                    f"{attributes['direction']} {attributes['protocol']} "
                    + str(attributes.get("method") or attributes.get("action") or "communication")
                ),
            ),
        ),
    )


def _aggregate_facts(values: Iterable[ObservedArchitectureFact]) -> tuple[ObservedArchitectureFact, ...]:
    grouped: dict[str, list[ObservedArchitectureFact]] = {}
    for item in values:
        grouped.setdefault(item.semantic_key, []).append(item)
    result: list[ObservedArchitectureFact] = []
    for key in sorted(grouped):
        items = grouped[key]
        first = items[0]
        provenance_by_location: dict[tuple[str, int], TraceProvenance] = {}
        for item in items:
            for provenance in item.provenance:
                provenance_by_location[provenance.location] = provenance
        result.append(
            ObservedArchitectureFact(
                kind=first.kind,
                source=first.source,
                target=first.target,
                attributes=first.attributes,
                provenance=tuple(
                    sorted(
                        provenance_by_location.values(),
                        key=lambda value: (value.source.casefold(), value.source, value.line, value.detail or ""),
                    )
                ),
            )
        )
    return tuple(result)


def _contract_facts(
    project_root: Path,
    approved: ApprovedArchitecture,
) -> tuple[ObservedArchitectureFact, ...]:
    approved_contracts = [
        fact for fact in approved.topology.facts if fact.kind is ArchitectureFactKind.CONTRACT
    ]
    if not approved_contracts:
        return ()
    try:
        index = build_contract_trace_index(project_root)
    except ContractTraceError as exc:
        raise _fail("SDAI-ARCH-COMM-005", f"unable to build current contract trace index: {exc}") from exc
    if index.gaps:
        details = "; ".join(f"{item.kind}:{item.target}" for item in index.gaps[:10])
        raise _fail("SDAI-ARCH-COMM-005", f"current contract truth has validation gaps: {details}")
    results: list[ObservedArchitectureFact] = []
    for fact in approved_contracts:
        attributes = _fact_attributes(fact)
        source_id = attributes.get("sourceId")
        address = attributes.get("address")
        if not isinstance(source_id, str) or not source_id:
            raise _fail(
                "SDAI-ARCH-COMM-005",
                f"approved contract fact {fact.fact_id!r} requires attributes.sourceId",
            )
        source_node = index.sources.get(source_id)
        if source_node is None:
            continue
        current_source_sha = source_node.metadata.get("source_sha256")
        if not isinstance(current_source_sha, str):
            raise _fail("SDAI-ARCH-COMM-005", f"contract source {source_id!r} has no canonical source hash")
        observed_attributes: dict[str, object] = {
            "sourceId": source_id,
            "sourceSha256": current_source_sha,
        }
        if address is not None:
            if not isinstance(address, str) or not address:
                raise _fail(
                    "SDAI-ARCH-COMM-005",
                    f"approved contract fact {fact.fact_id!r} has invalid attributes.address",
                )
            symbol = index.symbols.get((source_id, address))
            if symbol is None:
                continue
            observed_attributes["address"] = address
            observed_attributes["symbolSha256"] = symbol.symbol_sha256
            detail = f"current {symbol.contract_kind} contract symbol {source_id}:{address}"
        else:
            detail = f"current contract source {source_id}"
        results.append(
            ObservedArchitectureFact(
                kind=ArchitectureFactKind.CONTRACT,
                source=fact.source,
                target=fact.target,
                attributes=observed_attributes,
                provenance=(
                    TraceProvenance(
                        source_node.metadata.get("source_path") if isinstance(source_node.metadata.get("source_path"), str) else source_node.provenance[0].source,
                        1,
                        detail=detail,
                        declaration_sha256=current_source_sha,
                    ),
                ),
            )
        )
    return tuple(results)


class ServiceCommunicationObserver:
    """Observe local service endpoints/calls/events and current contract bindings without runtime probing."""

    observer_id = COMMUNICATION_OBSERVER_ID

    def observe(
        self,
        project_root: Path,
        approved: ApprovedArchitecture,
    ) -> ArchitectureObservation:
        if not isinstance(approved, ApprovedArchitecture):
            raise _fail("SDAI-ARCH-COMM-001", "communication observation requires approved architecture truth")
        repository = ArchitectureRepositoryIndex(project_root, approved.topology.components)
        host_aliases, channel_aliases = _communication_aliases(approved)
        facts: list[ObservedArchitectureFact] = []
        for path in repository.source_files(_SOURCE_SUFFIXES, maximum=COMMUNICATION_MAX_SOURCE_FILES):
            relative = PurePosixPath(path.relative_to(repository.root).as_posix())
            component = repository.owner_for_relative_path(relative)
            if component is None:
                continue
            source = relative.as_posix()
            text = repository.read_utf8(
                path,
                maximum_bytes=COMMUNICATION_MAX_FILE_BYTES,
                label="communication source",
            )
            for hit in _hits_for(path, text, source=source):
                facts.append(
                    _hit_fact(
                        component,
                        hit,
                        source=source,
                        host_aliases=host_aliases,
                        channel_aliases=channel_aliases,
                    )
                )
        facts.extend(_contract_facts(repository.root, approved))
        return ArchitectureObservation(self.observer_id, _aggregate_facts(facts))


__all__ = [
    "COMMUNICATION_OBSERVER_ID",
    "ServiceCommunicationObserver",
]
