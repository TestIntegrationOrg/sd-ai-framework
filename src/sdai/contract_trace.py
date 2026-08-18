from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path, PurePosixPath
import re
from typing import Iterable, Mapping

import yaml

from sdai.asyncapi_contracts import _parse as _parse_asyncapi
from sdai.contracts import (
    CONTRACT_MANIFEST_PATH,
    ContractFinding,
    ContractSeverity,
    ContractSnapshot,
    discover_contracts,
)
from sdai.json_schema_contracts import _parse as _parse_json_schema
from sdai.openapi_contracts import _parse as _parse_openapi
from sdai.protobuf_contracts import (
    ProtoDocument,
    ProtoField,
    ProtoMessage,
    ProtoRpc,
    ProtoService,
    _parse as _parse_protobuf,
)
from sdai.structured_contracts import UniqueKeySafeLoader, escape_json_pointer
from sdai.trace_graph import TraceEdge, TraceNode, TraceNodeType, TraceProvenance, TraceRelation


CONTRACT_TRACE_API_VERSION = "sdai.contract-trace/v1"
CONTRACT_TRACE_FILE = "contract-trace.yaml"
CONTRACT_POLICY_DECISION_API_VERSION = "sdai.contract-policy-decision/v1"
CONTRACT_TRACE_MAX_BYTES = 1024 * 1024
CONTRACT_TRACE_MAX_LINKS = 10_000
CONTRACT_TRACE_MAX_SYMBOLS_PER_SOURCE = 50_000
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")


class ContractTraceError(RuntimeError):
    """Raised when deterministic contract trace material cannot be built safely."""


@dataclass(frozen=True, slots=True)
class ContractTraceGap:
    kind: str
    source: str
    line: int
    target: str
    relation: str
    source_node_id: str | None = None
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class ContractTraceSymbol:
    source_id: str
    source_path: str
    contract_kind: str
    address: str
    symbol_kind: str
    symbol_sha256: str
    node_id: str


@dataclass(frozen=True, slots=True)
class ContractTraceIndex:
    nodes: tuple[TraceNode, ...]
    edges: tuple[TraceEdge, ...]
    gaps: tuple[ContractTraceGap, ...]
    sources: Mapping[str, TraceNode]
    symbols: Mapping[tuple[str, str], ContractTraceSymbol]


@dataclass(frozen=True, slots=True)
class ContractTraceLinks:
    edges: tuple[TraceEdge, ...]
    gaps: tuple[ContractTraceGap, ...]


def _fail(code: str, message: str) -> ContractTraceError:
    return ContractTraceError(f"{code}: {message}")


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
        raise _fail("SDAI-CONTRACT-TRACE-001", f"contract trace data is not canonical JSON: {exc}") from exc


def _hash_json(value: object) -> str:
    return "sha256:" + sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _hash_bytes(value: bytes) -> str:
    return "sha256:" + sha256(value).hexdigest()


def _source_entity_id(source_id: str) -> str:
    return f"source:{source_id}"


def _symbol_entity_id(source_id: str, address: str) -> str:
    digest = sha256(address.encode("utf-8")).hexdigest()
    return f"symbol:{source_id}:sha256:{digest}"


def _source_node_id(source_id: str) -> str:
    return f"contract:{_source_entity_id(source_id)}"


def _symbol_node_id(source_id: str, address: str) -> str:
    return f"contract:{_symbol_entity_id(source_id, address)}"


def _provenance(
    source_path: str,
    source_sha256: str,
    *,
    detail: str,
) -> tuple[TraceProvenance, ...]:
    return (
        TraceProvenance(
            source=source_path,
            line=1,
            detail=detail,
            declaration_sha256=source_sha256,
        ),
    )


def _source_node(snapshot: ContractSnapshot) -> TraceNode:
    source = snapshot.source
    return TraceNode(
        type=TraceNodeType.CONTRACT,
        entity_id=_source_entity_id(source.source_id),
        label=source.source_id,
        metadata={
            "contract_trace_role": "source",
            "source_id": source.source_id,
            "contract_kind": source.kind,
            "source_path": source.path,
            "source_sha256": snapshot.sha256,
        },
        provenance=_provenance(
            source.path,
            snapshot.sha256,
            detail=f"declared {source.kind} contract source {source.source_id}",
        ),
    )


def _symbol_node(
    snapshot: ContractSnapshot,
    *,
    address: str,
    symbol_kind: str,
    label: str,
    value: object,
) -> tuple[TraceNode, ContractTraceSymbol]:
    symbol_sha256 = _hash_json(
        {
            "source_id": snapshot.source.source_id,
            "contract_kind": snapshot.source.kind,
            "address": address,
            "symbol_kind": symbol_kind,
            "value": value,
        }
    )
    node = TraceNode(
        type=TraceNodeType.CONTRACT,
        entity_id=_symbol_entity_id(snapshot.source.source_id, address),
        label=label,
        metadata={
            "contract_trace_role": "symbol",
            "source_id": snapshot.source.source_id,
            "contract_kind": snapshot.source.kind,
            "source_path": snapshot.source.path,
            "source_sha256": snapshot.sha256,
            "address": address,
            "symbol_kind": symbol_kind,
            "symbol_sha256": symbol_sha256,
        },
        provenance=_provenance(
            snapshot.source.path,
            snapshot.sha256,
            detail=f"{symbol_kind} contract symbol {address}",
        ),
    )
    return node, ContractTraceSymbol(
        source_id=snapshot.source.source_id,
        source_path=snapshot.source.path,
        contract_kind=snapshot.source.kind,
        address=address,
        symbol_kind=symbol_kind,
        symbol_sha256=symbol_sha256,
        node_id=node.node_id,
    )


def _validation_gaps(
    snapshot: ContractSnapshot,
    findings: Iterable[ContractFinding],
) -> list[ContractTraceGap]:
    gaps: list[ContractTraceGap] = []
    for finding in findings:
        if finding.severity is not ContractSeverity.ERROR:
            continue
        pointer = finding.provenance.pointer if finding.provenance is not None else None
        gaps.append(
            ContractTraceGap(
                kind="invalid-contract-source",
                source=snapshot.source.path,
                line=1,
                source_node_id=_source_node_id(snapshot.source.source_id),
                target=pointer or snapshot.source.source_id,
                relation=TraceRelation.REFERENCES.value,
                detail=f"{finding.code}: {finding.message}",
            )
        )
    return gaps


def _openapi_symbols(snapshot: ContractSnapshot) -> tuple[list[tuple[str, str, str, object]], tuple[ContractFinding, ...]]:
    parsed = _parse_openapi(snapshot)
    document = parsed.document
    if document is None:
        return [], parsed.findings
    symbols: list[tuple[str, str, str, object]] = []
    paths = document.get("paths")
    if isinstance(paths, Mapping):
        for path in sorted(paths):
            path_item = paths[path]
            if not isinstance(path, str) or not isinstance(path_item, Mapping):
                continue
            escaped_path = escape_json_pointer(path)
            for method in ("delete", "get", "head", "options", "patch", "post", "put", "trace"):
                operation = path_item.get(method)
                if not isinstance(operation, Mapping):
                    continue
                address = f"/paths/{escaped_path}/{method}"
                operation_id = operation.get("operationId")
                label = (
                    operation_id.strip()
                    if isinstance(operation_id, str) and operation_id.strip()
                    else f"{method.upper()} {path}"
                )
                symbols.append((address, "operation", label, operation))
    return symbols, parsed.findings


def _asyncapi_symbols(snapshot: ContractSnapshot) -> tuple[list[tuple[str, str, str, object]], tuple[ContractFinding, ...]]:
    parsed = _parse_asyncapi(snapshot)
    document = parsed.document
    if document is None:
        return [], parsed.findings
    symbols: list[tuple[str, str, str, object]] = []
    channels = document.get("channels")
    if isinstance(channels, Mapping):
        for channel_name in sorted(channels):
            channel = channels[channel_name]
            escaped = escape_json_pointer(str(channel_name))
            address = f"/channels/{escaped}"
            symbols.append((address, "channel", str(channel_name), channel))
            if not isinstance(channel, Mapping):
                continue
            for action in ("publish", "subscribe"):
                operation = channel.get(action)
                if isinstance(operation, Mapping):
                    op_address = f"{address}/{action}"
                    symbols.append((op_address, "operation", f"{action} {channel_name}", operation))
                    message = operation.get("message")
                    if message is not None:
                        symbols.append((f"{op_address}/message", "message", f"{channel_name} {action} message", message))
                    messages = operation.get("messages")
                    if isinstance(messages, list):
                        for index, item in enumerate(messages):
                            symbols.append((f"{op_address}/messages/{index}", "message", f"{channel_name} {action} message {index}", item))
                    elif isinstance(messages, Mapping):
                        for name in sorted(messages):
                            symbols.append((f"{op_address}/messages/{escape_json_pointer(str(name))}", "message", str(name), messages[name]))
            channel_messages = channel.get("messages")
            if isinstance(channel_messages, Mapping):
                for name in sorted(channel_messages):
                    symbols.append((f"{address}/messages/{escape_json_pointer(str(name))}", "message", str(name), channel_messages[name]))
            elif isinstance(channel_messages, list):
                for index, item in enumerate(channel_messages):
                    symbols.append((f"{address}/messages/{index}", "message", f"{channel_name} message {index}", item))

    operations = document.get("operations")
    if isinstance(operations, Mapping):
        for operation_id in sorted(operations):
            operation = operations[operation_id]
            if not isinstance(operation, Mapping):
                continue
            address = f"/operations/{escape_json_pointer(str(operation_id))}"
            symbols.append((address, "operation", str(operation_id), operation))
            messages = operation.get("messages")
            if isinstance(messages, list):
                for index, item in enumerate(messages):
                    symbols.append((f"{address}/messages/{index}", "message", f"{operation_id} message {index}", item))
            elif isinstance(messages, Mapping):
                for name in sorted(messages):
                    symbols.append((f"{address}/messages/{escape_json_pointer(str(name))}", "message", str(name), messages[name]))

    components = document.get("components")
    if isinstance(components, Mapping):
        messages = components.get("messages")
        if isinstance(messages, Mapping):
            for name in sorted(messages):
                symbols.append((f"/components/messages/{escape_json_pointer(str(name))}", "message", str(name), messages[name]))
    return symbols, parsed.findings


def _json_schema_children(value: object, pointer: str) -> Iterable[tuple[str, object]]:
    if not isinstance(value, Mapping):
        return ()
    result: list[tuple[str, object]] = []
    mapping_keywords = ("$defs", "definitions", "properties", "patternProperties", "dependentSchemas")
    for keyword in mapping_keywords:
        collection = value.get(keyword)
        if not isinstance(collection, Mapping):
            continue
        for name in sorted(collection):
            child = collection[name]
            if isinstance(child, (Mapping, bool)):
                result.append((f"{pointer}/{escape_json_pointer(keyword)}/{escape_json_pointer(str(name))}", child))
    single_keywords = (
        "additionalProperties",
        "contains",
        "else",
        "if",
        "items",
        "not",
        "propertyNames",
        "then",
        "unevaluatedItems",
        "unevaluatedProperties",
    )
    for keyword in single_keywords:
        child = value.get(keyword)
        if isinstance(child, (Mapping, bool)):
            result.append((f"{pointer}/{escape_json_pointer(keyword)}", child))
        elif keyword == "items" and isinstance(child, list):
            for index, item in enumerate(child):
                if isinstance(item, (Mapping, bool)):
                    result.append((f"{pointer}/items/{index}", item))
    for keyword in ("allOf", "anyOf", "oneOf", "prefixItems"):
        collection = value.get(keyword)
        if isinstance(collection, list):
            for index, item in enumerate(collection):
                if isinstance(item, (Mapping, bool)):
                    result.append((f"{pointer}/{escape_json_pointer(keyword)}/{index}", item))
    return result


def _json_schema_symbols(snapshot: ContractSnapshot) -> tuple[list[tuple[str, str, str, object]], tuple[ContractFinding, ...]]:
    parsed = _parse_json_schema(snapshot)
    if parsed.schema is None:
        return [], parsed.findings
    symbols: list[tuple[str, str, str, object]] = []
    stack: list[tuple[str, object]] = [("#", parsed.schema)]
    while stack:
        address, schema = stack.pop()
        symbols.append((address, "schema", address, schema))
        children = list(_json_schema_children(schema, address))
        for child in reversed(children):
            stack.append(child)
        if len(symbols) > CONTRACT_TRACE_MAX_SYMBOLS_PER_SOURCE:
            raise _fail(
                "SDAI-CONTRACT-TRACE-004",
                f"JSON Schema source {snapshot.source.source_id!r} exceeds the {CONTRACT_TRACE_MAX_SYMBOLS_PER_SOURCE}-symbol trace limit",
            )
    return symbols, parsed.findings


def _proto_field_value(field: ProtoField) -> dict[str, object]:
    return {
        "name": field.name,
        "number": field.number,
        "type": field.type_name,
        "cardinality": field.cardinality,
    }


def _proto_message_value(message: ProtoMessage) -> dict[str, object]:
    return {
        "full_name": message.full_name,
        "fields": [_proto_field_value(item) for item in message.fields],
        "reserved_ranges": [list(item) for item in message.reserved_ranges],
        "reserved_names": list(message.reserved_names),
    }


def _proto_rpc_value(rpc: ProtoRpc) -> dict[str, object]:
    return {
        "name": rpc.name,
        "request_type": rpc.request_type,
        "response_type": rpc.response_type,
        "client_streaming": rpc.client_streaming,
        "server_streaming": rpc.server_streaming,
    }


def _proto_service_value(service: ProtoService) -> dict[str, object]:
    return {
        "full_name": service.full_name,
        "rpcs": [_proto_rpc_value(item) for item in service.rpcs],
    }


def _protobuf_symbols(
    snapshot: ContractSnapshot,
    snapshots_by_path: Mapping[str, ContractSnapshot],
) -> tuple[list[tuple[str, str, str, object]], tuple[ContractFinding, ...]]:
    parsed = _parse_protobuf(
        snapshot,
        snapshots_by_path,
        importer_path=snapshot.source.path,
    )
    document: ProtoDocument | None = parsed.document
    if document is None:
        return [], parsed.findings
    symbols: list[tuple[str, str, str, object]] = []
    for message in document.messages:
        escaped = escape_json_pointer(message.full_name)
        address = f"/messages/{escaped}"
        symbols.append((address, "message", message.full_name, _proto_message_value(message)))
        for field in message.fields:
            field_address = f"{address}/fields/{field.number}"
            symbols.append((field_address, "field", f"{message.full_name}.{field.name}", _proto_field_value(field)))
    for service in document.services:
        escaped = escape_json_pointer(service.full_name)
        address = f"/services/{escaped}"
        symbols.append((address, "service", service.full_name, _proto_service_value(service)))
        for rpc in service.rpcs:
            rpc_address = f"{address}/rpcs/{escape_json_pointer(rpc.name)}"
            symbols.append((rpc_address, "rpc", f"{service.full_name}.{rpc.name}", _proto_rpc_value(rpc)))
    return symbols, parsed.findings


def _symbols_for(
    snapshot: ContractSnapshot,
    snapshots_by_path: Mapping[str, ContractSnapshot],
) -> tuple[list[tuple[str, str, str, object]], tuple[ContractFinding, ...]]:
    kind = snapshot.source.kind
    if kind == "openapi":
        return _openapi_symbols(snapshot)
    if kind == "asyncapi":
        return _asyncapi_symbols(snapshot)
    if kind == "json-schema":
        return _json_schema_symbols(snapshot)
    if kind == "protobuf":
        return _protobuf_symbols(snapshot, snapshots_by_path)
    raise _fail("SDAI-CONTRACT-TRACE-003", f"unsupported contract kind for trace extraction: {kind!r}")


def _member_edge(snapshot: ContractSnapshot, source_node: TraceNode, symbol_node: TraceNode) -> TraceEdge:
    return TraceEdge(
        relation=TraceRelation.REFERENCES,
        source=source_node.node_id,
        target=symbol_node.node_id,
        provenance=_provenance(
            snapshot.source.path,
            snapshot.sha256,
            detail="contract source contains addressable symbol",
        ),
        metadata={
            "contract_trace_role": "member",
            "source_sha256": snapshot.sha256,
            "symbol_sha256": symbol_node.metadata.get("symbol_sha256"),
        },
    )


def empty_contract_trace_index() -> ContractTraceIndex:
    return ContractTraceIndex(nodes=(), edges=(), gaps=(), sources={}, symbols={})


def build_contract_trace_index(project_root: Path) -> ContractTraceIndex:
    """Build deterministic source/symbol trace nodes without requiring feature links."""
    root = project_root.resolve()
    manifest = root / CONTRACT_MANIFEST_PATH
    if not manifest.exists():
        return empty_contract_trace_index()
    try:
        inspection = discover_contracts(root)
    except RuntimeError as exc:
        raise _fail("SDAI-CONTRACT-TRACE-002", f"unable to discover contract sources: {exc}") from exc

    snapshots_by_path = {item.source.path: item for item in inspection.sources}
    nodes: list[TraceNode] = []
    edges: list[TraceEdge] = []
    gaps: list[ContractTraceGap] = []
    sources: dict[str, TraceNode] = {}
    symbols: dict[tuple[str, str], ContractTraceSymbol] = {}

    for snapshot in inspection.sources:
        source_node = _source_node(snapshot)
        nodes.append(source_node)
        sources[snapshot.source.source_id] = source_node
        extracted, findings = _symbols_for(snapshot, snapshots_by_path)
        gaps.extend(_validation_gaps(snapshot, findings))
        if len(extracted) > CONTRACT_TRACE_MAX_SYMBOLS_PER_SOURCE:
            raise _fail(
                "SDAI-CONTRACT-TRACE-004",
                f"contract source {snapshot.source.source_id!r} exceeds the {CONTRACT_TRACE_MAX_SYMBOLS_PER_SOURCE}-symbol trace limit",
            )
        seen_addresses: set[str] = set()
        for address, symbol_kind, label, value in sorted(extracted, key=lambda item: (item[0], item[1], item[2])):
            if address in seen_addresses:
                raise _fail(
                    "SDAI-CONTRACT-TRACE-004",
                    f"contract source {snapshot.source.source_id!r} produced duplicate trace address {address!r}",
                )
            seen_addresses.add(address)
            node, symbol = _symbol_node(
                snapshot,
                address=address,
                symbol_kind=symbol_kind,
                label=label,
                value=value,
            )
            nodes.append(node)
            edges.append(_member_edge(snapshot, source_node, node))
            symbols[(snapshot.source.source_id, address)] = symbol

    return ContractTraceIndex(
        nodes=tuple(sorted(nodes, key=lambda item: item.node_id)),
        edges=tuple(sorted(edges, key=lambda item: item.edge_id)),
        gaps=tuple(
            sorted(
                gaps,
                key=lambda item: (
                    item.kind,
                    item.source,
                    item.target,
                    item.detail or "",
                ),
            )
        ),
        sources=sources,
        symbols=symbols,
    )


def _portable_relative_path(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise _fail("SDAI-CONTRACT-TRACE-005", f"{label} must be a non-empty repository-relative POSIX path")
    if "\\" in value or "\x00" in value or re.match(r"^[A-Za-z]:", value):
        raise _fail("SDAI-CONTRACT-TRACE-005", f"{label} must be a repository-relative POSIX path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise _fail("SDAI-CONTRACT-TRACE-005", f"{label} is unsafe: {value!r}")
    return path.as_posix()


def _safe_repo_file(root: Path, relative: str, *, label: str) -> Path:
    candidate = root / relative
    try:
        resolved = candidate.resolve(strict=True)
    except (FileNotFoundError, OSError) as exc:
        raise _fail("SDAI-CONTRACT-TRACE-005", f"{label} does not exist: {relative}") from exc
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise _fail("SDAI-CONTRACT-TRACE-005", f"{label} escapes the project root: {relative}") from exc
    current = root
    for part in PurePosixPath(relative).parts:
        current = current / part
        if current.is_symlink():
            raise _fail("SDAI-CONTRACT-TRACE-005", f"{label} must not traverse symlinks: {relative}")
    if not resolved.is_file() or resolved.is_symlink():
        raise _fail("SDAI-CONTRACT-TRACE-005", f"{label} must be a regular file: {relative}")
    return resolved


def _read_bounded(path: Path, *, label: str) -> bytes:
    try:
        with path.open("rb") as handle:
            content = handle.read(CONTRACT_TRACE_MAX_BYTES + 1)
    except OSError as exc:
        raise _fail("SDAI-CONTRACT-TRACE-005", f"unable to read {label}: {path}") from exc
    if len(content) > CONTRACT_TRACE_MAX_BYTES:
        raise _fail(
            "SDAI-CONTRACT-TRACE-005",
            f"{label} exceeds the {CONTRACT_TRACE_MAX_BYTES}-byte limit",
        )
    return content


def _trace_manifest(root: Path, feature_id: str) -> tuple[str, Mapping[str, object], str] | None:
    relative = f"specs/changes/{feature_id}/{CONTRACT_TRACE_FILE}"
    path = root / relative
    if not path.exists():
        return None
    safe = _safe_repo_file(root, relative, label="contract trace manifest")
    content = _read_bounded(safe, label="contract trace manifest")
    try:
        text = content.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError as exc:
        raise _fail("SDAI-CONTRACT-TRACE-005", "contract trace manifest must be valid UTF-8") from exc
    try:
        raw = yaml.load(text, Loader=UniqueKeySafeLoader)
    except yaml.YAMLError as exc:
        raise _fail("SDAI-CONTRACT-TRACE-006", f"contract trace manifest is invalid YAML: {exc}") from exc
    if not isinstance(raw, Mapping) or not all(isinstance(key, str) for key in raw):
        raise _fail("SDAI-CONTRACT-TRACE-006", "contract trace manifest must be a string-keyed mapping")
    if set(raw) != {"apiVersion", "kind", "links"}:
        raise _fail("SDAI-CONTRACT-TRACE-006", "contract trace manifest fields must be exactly apiVersion, kind, links")
    if raw.get("apiVersion") != CONTRACT_TRACE_API_VERSION or raw.get("kind") != "ContractTrace":
        raise _fail(
            "SDAI-CONTRACT-TRACE-006",
            f"contract trace manifest must use {CONTRACT_TRACE_API_VERSION} / ContractTrace",
        )
    links = raw.get("links")
    if not isinstance(links, list):
        raise _fail("SDAI-CONTRACT-TRACE-006", "contract trace links must be a list")
    if len(links) > CONTRACT_TRACE_MAX_LINKS:
        raise _fail("SDAI-CONTRACT-TRACE-006", f"contract trace manifest exceeds {CONTRACT_TRACE_MAX_LINKS} links")
    return relative, raw, _hash_bytes(content)


def _sha(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value.casefold()) is None:
        raise _fail("SDAI-CONTRACT-TRACE-006", f"{label} must be a lowercase sha256:<64 hex> digest")
    normalized = value.casefold()
    if value != normalized:
        raise _fail("SDAI-CONTRACT-TRACE-006", f"{label} must be lowercase")
    return normalized


def _load_decision(
    root: Path,
    raw: object,
    *,
    current_source_sha256: str,
) -> tuple[str, str, str, TraceProvenance]:
    if not isinstance(raw, Mapping) or set(raw) != {"path", "sha256"}:
        raise _fail("SDAI-CONTRACT-TRACE-006", "decision must contain exactly path and sha256")
    relative = _portable_relative_path(raw.get("path"), label="decision.path")
    expected = _sha(raw.get("sha256"), label="decision.sha256")
    path = _safe_repo_file(root, relative, label="contract policy decision")
    content = _read_bounded(path, label="contract policy decision")
    try:
        parsed = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _fail("SDAI-CONTRACT-TRACE-007", f"contract policy decision is not valid UTF-8 JSON: {relative}") from exc
    if not isinstance(parsed, Mapping):
        raise _fail("SDAI-CONTRACT-TRACE-007", "contract policy decision must be a JSON object")
    if parsed.get("apiVersion") != CONTRACT_POLICY_DECISION_API_VERSION or parsed.get("kind") != "ContractPolicyDecision":
        raise _fail(
            "SDAI-CONTRACT-TRACE-007",
            f"decision must use {CONTRACT_POLICY_DECISION_API_VERSION} / ContractPolicyDecision",
        )
    recorded = parsed.get("sha256")
    if recorded != expected:
        raise _fail("SDAI-CONTRACT-TRACE-007", "decision sha256 does not match the trace declaration")
    unsigned = {key: value for key, value in parsed.items() if key != "sha256"}
    if _hash_json(unsigned) != expected:
        raise _fail("SDAI-CONTRACT-TRACE-007", "contract policy decision semantic hash is invalid")
    candidate_sha256 = parsed.get("candidateSha256")
    if candidate_sha256 != current_source_sha256:
        raise _fail(
            "SDAI-CONTRACT-TRACE-008",
            "contract policy decision candidate hash does not match the current contract source",
        )
    diff_sha256 = parsed.get("diffSha256")
    policy_sha256 = parsed.get("policySha256")
    if not isinstance(diff_sha256, str) or not isinstance(policy_sha256, str):
        raise _fail("SDAI-CONTRACT-TRACE-007", "contract policy decision is missing diff/policy hashes")
    return (
        expected,
        diff_sha256,
        policy_sha256,
        TraceProvenance(
            source=relative,
            line=1,
            detail="validated contract policy decision binding",
            declaration_sha256=expected,
        ),
    )


def _relation_for(target: TraceNode) -> TraceRelation:
    if target.type is TraceNodeType.TEST:
        return TraceRelation.VERIFIED_BY
    if target.type is TraceNodeType.APPROVAL:
        # Approval nodes must already exist independently in the canonical feature graph.
        # This overlay never creates an approval node from provider/AI output.
        return TraceRelation.APPROVED_BY
    if target.type is TraceNodeType.EVIDENCE:
        return TraceRelation.EVIDENCED_BY
    return TraceRelation.REFERENCES


def _link_gap(
    *,
    kind: str,
    manifest_source: str,
    target: str,
    source_node_id: str | None,
    detail: str,
) -> ContractTraceGap:
    return ContractTraceGap(
        kind=kind,
        source=manifest_source,
        line=1,
        target=target,
        relation=TraceRelation.REFERENCES.value,
        source_node_id=source_node_id,
        detail=detail,
    )


def build_contract_trace_links(
    project_root: Path,
    feature_id: str,
    index: ContractTraceIndex,
    graph_nodes: Iterable[TraceNode],
) -> ContractTraceLinks:
    """Resolve explicit hash-bound contract links against already-built canonical nodes."""
    root = project_root.resolve()
    manifest = _trace_manifest(root, feature_id)
    if manifest is None:
        return ContractTraceLinks(edges=(), gaps=())
    manifest_source, raw, manifest_sha256 = manifest
    raw_links = raw["links"]
    assert isinstance(raw_links, list)
    nodes = {node.node_id: node for node in graph_nodes}
    edges: list[TraceEdge] = []
    gaps: list[ContractTraceGap] = []
    seen_edges: set[tuple[str, str]] = set()

    for link_index, raw_link in enumerate(raw_links):
        label = f"links[{link_index}]"
        if not isinstance(raw_link, Mapping) or not all(isinstance(key, str) for key in raw_link):
            raise _fail("SDAI-CONTRACT-TRACE-006", f"{label} must be a string-keyed mapping")
        allowed = {"contract", "target", "sourceSha256", "symbolSha256", "decision"}
        unknown = sorted(set(raw_link) - allowed)
        if unknown:
            raise _fail("SDAI-CONTRACT-TRACE-006", f"{label} has unsupported key(s): {', '.join(unknown)}")
        if not {"contract", "target", "sourceSha256"} <= set(raw_link):
            raise _fail("SDAI-CONTRACT-TRACE-006", f"{label} requires contract, target, and sourceSha256")
        contract = raw_link.get("contract")
        if not isinstance(contract, Mapping) or not all(isinstance(key, str) for key in contract):
            raise _fail("SDAI-CONTRACT-TRACE-006", f"{label}.contract must be a mapping")
        if set(contract) not in ({"sourceId"}, {"sourceId", "address"}):
            raise _fail("SDAI-CONTRACT-TRACE-006", f"{label}.contract must contain sourceId and optional address")
        source_id = contract.get("sourceId")
        if not isinstance(source_id, str) or not source_id:
            raise _fail("SDAI-CONTRACT-TRACE-006", f"{label}.contract.sourceId must be non-empty text")
        address = contract.get("address")
        if address is not None and (not isinstance(address, str) or not address):
            raise _fail("SDAI-CONTRACT-TRACE-006", f"{label}.contract.address must be non-empty text")
        target_id = raw_link.get("target")
        if not isinstance(target_id, str) or not target_id:
            raise _fail("SDAI-CONTRACT-TRACE-006", f"{label}.target must be a canonical non-empty node_id")
        expected_source = _sha(raw_link.get("sourceSha256"), label=f"{label}.sourceSha256")

        source_node = index.sources.get(source_id)
        if source_node is None:
            gaps.append(
                _link_gap(
                    kind="dangling-contract-source",
                    manifest_source=manifest_source,
                    target=source_id,
                    source_node_id=None,
                    detail=f"{label} references an undeclared contract source",
                )
            )
            continue
        current_source = source_node.metadata.get("source_sha256")
        if current_source != expected_source:
            gaps.append(
                _link_gap(
                    kind="stale-contract-source",
                    manifest_source=manifest_source,
                    target=source_id,
                    source_node_id=source_node.node_id,
                    detail=f"{label} expected {expected_source} but current source is {current_source}",
                )
            )
            continue

        source_trace_node = source_node
        current_symbol_sha256: str | None = None
        if address is not None:
            if "symbolSha256" not in raw_link:
                raise _fail("SDAI-CONTRACT-TRACE-006", f"{label}.symbolSha256 is required for a symbol link")
            expected_symbol = _sha(raw_link.get("symbolSha256"), label=f"{label}.symbolSha256")
            symbol = index.symbols.get((source_id, address))
            if symbol is None:
                gaps.append(
                    _link_gap(
                        kind="dangling-contract-symbol",
                        manifest_source=manifest_source,
                        target=f"{source_id}:{address}",
                        source_node_id=source_node.node_id,
                        detail=f"{label} references an address that is not present in the current contract",
                    )
                )
                continue
            current_symbol_sha256 = symbol.symbol_sha256
            if symbol.symbol_sha256 != expected_symbol:
                gaps.append(
                    _link_gap(
                        kind="stale-contract-symbol",
                        manifest_source=manifest_source,
                        target=f"{source_id}:{address}",
                        source_node_id=symbol.node_id,
                        detail=f"{label} expected {expected_symbol} but current symbol is {symbol.symbol_sha256}",
                    )
                )
                continue
            source_trace_node = nodes.get(symbol.node_id, source_trace_node)
        elif "symbolSha256" in raw_link:
            raise _fail("SDAI-CONTRACT-TRACE-006", f"{label}.symbolSha256 is only valid when contract.address is present")

        target_node = nodes.get(target_id)
        if target_node is None:
            gaps.append(
                _link_gap(
                    kind="missing-contract-trace-target",
                    manifest_source=manifest_source,
                    target=target_id,
                    source_node_id=source_trace_node.node_id,
                    detail=f"{label} target is not present in the canonical trace graph",
                )
            )
            continue
        if target_node.node_id == source_trace_node.node_id:
            raise _fail("SDAI-CONTRACT-TRACE-006", f"{label} cannot create a self-referential contract trace edge")

        relation = _relation_for(target_node)
        identity = (source_trace_node.node_id, target_node.node_id)
        if identity in seen_edges:
            raise _fail("SDAI-CONTRACT-TRACE-006", f"{label} duplicates an existing contract trace link")
        seen_edges.add(identity)

        metadata: dict[str, object] = {
            "contract_trace_role": "link",
            "source_id": source_id,
            "source_sha256": expected_source,
            "manifest_sha256": manifest_sha256,
        }
        if address is not None:
            metadata["address"] = address
            metadata["symbol_sha256"] = current_symbol_sha256

        provenance: list[TraceProvenance] = [
            TraceProvenance(
                source=manifest_source,
                line=1,
                detail=f"explicit contract trace declaration {label}",
                declaration_sha256=manifest_sha256,
            )
        ]
        if "decision" in raw_link:
            try:
                decision_sha256, diff_sha256, policy_sha256, decision_provenance = _load_decision(
                    root,
                    raw_link.get("decision"),
                    current_source_sha256=expected_source,
                )
            except ContractTraceError as exc:
                gaps.append(
                    _link_gap(
                        kind="stale-contract-decision",
                        manifest_source=manifest_source,
                        target=source_trace_node.node_id,
                        source_node_id=source_trace_node.node_id,
                        detail=f"{label}: {exc}",
                    )
                )
                continue
            metadata["decision_sha256"] = decision_sha256
            metadata["diff_sha256"] = diff_sha256
            metadata["policy_sha256"] = policy_sha256
            provenance.append(decision_provenance)

        edges.append(
            TraceEdge(
                relation=relation,
                source=source_trace_node.node_id,
                target=target_node.node_id,
                provenance=tuple(provenance),
                metadata=metadata,
            )
        )

    return ContractTraceLinks(
        edges=tuple(sorted(edges, key=lambda item: item.edge_id)),
        gaps=tuple(
            sorted(
                gaps,
                key=lambda item: (
                    item.kind,
                    item.source,
                    item.target,
                    item.source_node_id or "",
                    item.detail or "",
                ),
            )
        ),
    )


__all__ = [
    "CONTRACT_TRACE_API_VERSION",
    "CONTRACT_TRACE_FILE",
    "ContractTraceError",
    "ContractTraceGap",
    "ContractTraceIndex",
    "ContractTraceLinks",
    "ContractTraceSymbol",
    "build_contract_trace_index",
    "build_contract_trace_links",
    "empty_contract_trace_index",
]
