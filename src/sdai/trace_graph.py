from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from hashlib import sha256
import json
import math
from pathlib import Path, PurePosixPath
import re
from types import MappingProxyType
from typing import Iterable, Mapping

from sdai.models import validate_feature_id
from sdai.path_safety import PathSafetyError, ensure_within_project


TRACE_GRAPH_API_VERSION = "sdai.trace-graph/v1"
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_ENTITY_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/#@+\-]{0,255}$")
_MAX_DETAIL_LENGTH = 4096


class TraceGraphError(RuntimeError):
    """Raised when canonical traceability graph facts are invalid or ambiguous."""


class TraceNodeType(str, Enum):
    REQUIREMENT = "requirement"
    SCENARIO = "scenario"
    RFC = "rfc"
    ADR = "adr"
    COMPONENT = "component"
    CONTRACT = "contract"
    THREAT = "threat"
    TASK = "task"
    CODE = "code"
    TEST = "test"
    APPROVAL = "approval"
    EVIDENCE = "evidence"


class TraceRelation(str, Enum):
    HAS_SCENARIO = "has-scenario"
    DESIGNED_BY = "designed-by"
    IMPLEMENTED_BY = "implemented-by"
    VERIFIED_BY = "verified-by"
    THREATENED_BY = "threatened-by"
    MITIGATED_BY = "mitigated-by"
    APPROVED_BY = "approved-by"
    EVIDENCED_BY = "evidenced-by"
    CONTAINS = "contains"
    DEPENDS_ON = "depends-on"
    REFERENCES = "references"


_ALL_NODE_TYPES = frozenset(TraceNodeType)
_RELATION_ENDPOINTS: Mapping[
    TraceRelation,
    tuple[frozenset[TraceNodeType], frozenset[TraceNodeType]],
] = MappingProxyType(
    {
        TraceRelation.HAS_SCENARIO: (
            frozenset({TraceNodeType.REQUIREMENT}),
            frozenset({TraceNodeType.SCENARIO}),
        ),
        TraceRelation.DESIGNED_BY: (
            frozenset({TraceNodeType.REQUIREMENT, TraceNodeType.SCENARIO}),
            frozenset(
                {
                    TraceNodeType.RFC,
                    TraceNodeType.ADR,
                    TraceNodeType.COMPONENT,
                    TraceNodeType.CONTRACT,
                }
            ),
        ),
        TraceRelation.IMPLEMENTED_BY: (
            frozenset(
                {
                    TraceNodeType.REQUIREMENT,
                    TraceNodeType.SCENARIO,
                    TraceNodeType.TASK,
                }
            ),
            frozenset({TraceNodeType.TASK, TraceNodeType.CODE}),
        ),
        TraceRelation.VERIFIED_BY: (
            frozenset(
                {
                    TraceNodeType.REQUIREMENT,
                    TraceNodeType.SCENARIO,
                    TraceNodeType.TASK,
                    TraceNodeType.CODE,
                    TraceNodeType.CONTRACT,
                }
            ),
            frozenset({TraceNodeType.TEST, TraceNodeType.EVIDENCE}),
        ),
        TraceRelation.THREATENED_BY: (
            frozenset(
                {
                    TraceNodeType.REQUIREMENT,
                    TraceNodeType.COMPONENT,
                    TraceNodeType.CONTRACT,
                    TraceNodeType.CODE,
                }
            ),
            frozenset({TraceNodeType.THREAT}),
        ),
        TraceRelation.MITIGATED_BY: (
            frozenset({TraceNodeType.THREAT}),
            frozenset(
                {
                    TraceNodeType.TASK,
                    TraceNodeType.CODE,
                    TraceNodeType.EVIDENCE,
                }
            ),
        ),
        TraceRelation.APPROVED_BY: (
            frozenset(
                {
                    TraceNodeType.REQUIREMENT,
                    TraceNodeType.RFC,
                    TraceNodeType.ADR,
                    TraceNodeType.CONTRACT,
                    TraceNodeType.THREAT,
                }
            ),
            frozenset({TraceNodeType.APPROVAL}),
        ),
        TraceRelation.EVIDENCED_BY: (
            frozenset(_ALL_NODE_TYPES - {TraceNodeType.EVIDENCE}),
            frozenset({TraceNodeType.EVIDENCE}),
        ),
        TraceRelation.CONTAINS: (
            frozenset({TraceNodeType.COMPONENT}),
            frozenset({TraceNodeType.CODE, TraceNodeType.TEST}),
        ),
        TraceRelation.DEPENDS_ON: (_ALL_NODE_TYPES, _ALL_NODE_TYPES),
        TraceRelation.REFERENCES: (_ALL_NODE_TYPES, _ALL_NODE_TYPES),
    }
)


def _fail(code: str, message: str) -> TraceGraphError:
    return TraceGraphError(f"{code}: {message}")


def _validate_json_value(value: object, *, label: str) -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise _fail("SDAI-TRACE-001", f"{label} contains a non-finite number")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_json_value(item, label=f"{label}[{index}]")
        return
    if isinstance(value, tuple):
        for index, item in enumerate(value):
            _validate_json_value(item, label=f"{label}[{index}]")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str) or not key:
                raise _fail("SDAI-TRACE-001", f"{label} mapping keys must be non-empty strings")
            _validate_json_value(item, label=f"{label}.{key}")
        return
    raise _fail(
        "SDAI-TRACE-001",
        f"{label} contains unsupported JSON value type {type(value).__name__}",
    )


def _canonical_bytes(payload: Mapping[str, object]) -> bytes:
    _validate_json_value(payload, label="trace graph")
    try:
        return json.dumps(
            payload,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise _fail("SDAI-TRACE-001", f"trace graph is not canonical JSON: {exc}") from exc


def _sha256_bytes(content: bytes) -> str:
    return "sha256:" + sha256(content).hexdigest()


def _freeze_json(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    return value


def _thaw_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _normalize_metadata(
    value: Mapping[str, object] | None,
    *,
    label: str,
) -> Mapping[str, object]:
    if value is None:
        return MappingProxyType({})
    if not isinstance(value, Mapping):
        raise _fail("SDAI-TRACE-001", f"{label} must be a mapping")
    normalized = dict(value)
    _validate_json_value(normalized, label=label)
    cloned = json.loads(
        json.dumps(
            normalized,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
    )
    if not isinstance(cloned, dict):
        raise _fail("SDAI-TRACE-001", f"{label} must normalize to a mapping")
    frozen = _freeze_json(cloned)
    if not isinstance(frozen, Mapping):
        raise _fail("SDAI-TRACE-001", f"{label} must normalize to a mapping")
    return frozen


def _metadata_dict(value: Mapping[str, object] | None) -> dict[str, object]:
    thawed = _thaw_json(value or {})
    if not isinstance(thawed, dict):
        raise _fail("SDAI-TRACE-001", "trace metadata must normalize to a mapping")
    return thawed


def _normalize_entity_id(value: str) -> str:
    if not isinstance(value, str) or not _ENTITY_ID.fullmatch(value):
        raise _fail(
            "SDAI-TRACE-001",
            "entity_id must use 1-256 portable letters, numbers, '.', '_', ':', '/', '#', '@', '+', or '-'",
        )
    if "\\" in value or any(ord(char) < 32 for char in value):
        raise _fail("SDAI-TRACE-001", f"entity_id is not portable: {value!r}")
    return value


def trace_node_id(node_type: TraceNodeType | str, entity_id: str) -> str:
    try:
        kind = node_type if isinstance(node_type, TraceNodeType) else TraceNodeType(node_type)
    except ValueError as exc:
        raise _fail("SDAI-TRACE-001", f"unsupported trace node type: {node_type!r}") from exc
    return f"{kind.value}:{_normalize_entity_id(entity_id)}"


def _normalize_source(source: str) -> str:
    if not isinstance(source, str) or not source.strip():
        raise _fail("SDAI-TRACE-002", "provenance source must be a non-empty string")
    value = source.strip()
    if "\\" in value or value.startswith("/") or re.match(r"^[A-Za-z]:", value):
        raise _fail(
            "SDAI-TRACE-002",
            f"provenance source must be a repository-relative POSIX path: {source!r}",
        )
    path = PurePosixPath(value)
    if any(part in {"", ".", ".."} for part in path.parts):
        raise _fail("SDAI-TRACE-002", f"unsafe provenance source path: {source!r}")
    return path.as_posix()


@dataclass(frozen=True)
class TraceProvenance:
    source: str
    line: int
    detail: str | None = None
    declaration_sha256: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "source", _normalize_source(self.source))
        if not isinstance(self.line, int) or isinstance(self.line, bool) or self.line < 1:
            raise _fail("SDAI-TRACE-002", "provenance line must be a positive integer")
        if self.detail is not None:
            if not isinstance(self.detail, str) or not self.detail.strip():
                raise _fail("SDAI-TRACE-002", "provenance detail must be null or non-empty text")
            detail = self.detail.strip()
            if len(detail) > _MAX_DETAIL_LENGTH:
                raise _fail("SDAI-TRACE-002", "provenance detail is too long")
            object.__setattr__(self, "detail", detail)
        if self.declaration_sha256 is not None and not _SHA256.fullmatch(self.declaration_sha256):
            raise _fail(
                "SDAI-TRACE-002",
                f"invalid provenance declaration SHA-256: {self.declaration_sha256!r}",
            )

    @property
    def location(self) -> tuple[str, int]:
        return (self.source, self.line)

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {"source": self.source, "line": self.line}
        if self.detail is not None:
            result["detail"] = self.detail
        if self.declaration_sha256 is not None:
            result["declaration_sha256"] = self.declaration_sha256
        return result

    @classmethod
    def from_mapping(cls, value: object) -> "TraceProvenance":
        if not isinstance(value, Mapping):
            raise _fail("SDAI-TRACE-002", "provenance must be a mapping")
        allowed = {"source", "line", "detail", "declaration_sha256"}
        unknown = sorted(str(key) for key in value if key not in allowed)
        if unknown:
            raise _fail(
                "SDAI-TRACE-002",
                "provenance contains unknown field(s): " + ", ".join(unknown),
            )
        if "source" not in value or "line" not in value:
            raise _fail("SDAI-TRACE-002", "provenance requires source and line")
        source = value["source"]
        line = value["line"]
        detail = value.get("detail")
        declaration_sha256 = value.get("declaration_sha256")
        if not isinstance(source, str):
            raise _fail("SDAI-TRACE-002", "provenance source must be a string")
        if not isinstance(line, int) or isinstance(line, bool):
            raise _fail("SDAI-TRACE-002", "provenance line must be an integer")
        if detail is not None and not isinstance(detail, str):
            raise _fail("SDAI-TRACE-002", "provenance detail must be null or a string")
        if declaration_sha256 is not None and not isinstance(declaration_sha256, str):
            raise _fail(
                "SDAI-TRACE-002",
                "provenance declaration_sha256 must be null or a string",
            )
        return cls(
            source=source,
            line=line,
            detail=detail,
            declaration_sha256=declaration_sha256,
        )


def trace_provenance_for_path(
    project_root: Path,
    path: Path,
    *,
    line: int,
    detail: str | None = None,
) -> TraceProvenance:
    root = project_root.resolve()
    candidate = path if path.is_absolute() else root / path
    try:
        ensure_within_project(root, candidate, label="trace provenance source")
    except PathSafetyError as exc:
        raise _fail(
            "SDAI-TRACE-002",
            "trace provenance source must stay inside the project root",
        ) from exc
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise _fail(
            "SDAI-TRACE-002",
            "trace provenance source must stay inside the project root",
        ) from exc
    current = root
    for part in relative.parts:
        current = current / part
        if current.exists() and current.is_symlink():
            raise _fail(
                "SDAI-TRACE-002",
                f"trace provenance source contains a symlink component: {relative.as_posix()}",
            )
    if candidate.is_symlink() or not candidate.is_file():
        raise _fail(
            "SDAI-TRACE-002",
            f"trace provenance source must be a regular non-symlink file: {relative.as_posix()}",
        )
    try:
        content = candidate.read_bytes()
    except OSError as exc:
        raise _fail(
            "SDAI-TRACE-002",
            f"unable to read trace provenance source {relative.as_posix()}: {exc}",
        ) from exc
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _fail(
            "SDAI-TRACE-002",
            f"trace provenance source must be valid UTF-8 text: {relative.as_posix()}",
        ) from exc
    line_count = len(text.splitlines())
    if line > line_count:
        raise _fail(
            "SDAI-TRACE-002",
            f"trace provenance line {line} is outside {relative.as_posix()} (lines={line_count})",
        )
    digest = _sha256_bytes(content)
    return TraceProvenance(
        source=relative.as_posix(),
        line=line,
        detail=detail,
        declaration_sha256=digest,
    )


def _canonical_provenance(
    values: Iterable[TraceProvenance],
    *,
    label: str,
) -> tuple[TraceProvenance, ...]:
    by_location: dict[tuple[str, int], TraceProvenance] = {}
    for item in values:
        if not isinstance(item, TraceProvenance):
            raise _fail("SDAI-TRACE-002", f"{label} provenance contains invalid item")
        existing = by_location.get(item.location)
        if existing is not None and existing != item:
            raise _fail(
                "SDAI-TRACE-004",
                f"conflicting provenance declaration at {item.source}:{item.line} for {label}",
            )
        by_location[item.location] = item
    if not by_location:
        raise _fail("SDAI-TRACE-002", f"{label} requires at least one source/line provenance record")
    return tuple(
        sorted(
            by_location.values(),
            key=lambda item: (
                item.source.casefold(),
                item.source,
                item.line,
                item.declaration_sha256 or "",
                item.detail or "",
            ),
        )
    )


@dataclass(frozen=True)
class TraceNode:
    type: TraceNodeType
    entity_id: str
    provenance: tuple[TraceProvenance, ...]
    label: str | None = None
    metadata: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        try:
            node_type = self.type if isinstance(self.type, TraceNodeType) else TraceNodeType(self.type)
        except ValueError as exc:
            raise _fail("SDAI-TRACE-001", f"unsupported trace node type: {self.type!r}") from exc
        object.__setattr__(self, "type", node_type)
        object.__setattr__(self, "entity_id", _normalize_entity_id(self.entity_id))
        object.__setattr__(
            self,
            "provenance",
            _canonical_provenance(self.provenance, label=f"node {self.node_id}"),
        )
        if self.label is not None:
            if not isinstance(self.label, str) or not self.label.strip():
                raise _fail("SDAI-TRACE-001", "node label must be null or non-empty text")
            object.__setattr__(self, "label", self.label.strip())
        object.__setattr__(
            self,
            "metadata",
            _normalize_metadata(self.metadata, label=f"node {self.node_id} metadata"),
        )

    @property
    def node_id(self) -> str:
        return trace_node_id(self.type, self.entity_id)

    def semantic_signature(self) -> bytes:
        return _canonical_bytes(
            {
                "type": self.type.value,
                "entity_id": self.entity_id,
                "label": self.label,
                "metadata": _metadata_dict(self.metadata),
            }
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "node_id": self.node_id,
            "type": self.type.value,
            "entity_id": self.entity_id,
            "label": self.label,
            "metadata": _metadata_dict(self.metadata),
            "provenance": [item.as_dict() for item in self.provenance],
        }

    @classmethod
    def from_mapping(cls, value: object) -> "TraceNode":
        if not isinstance(value, Mapping):
            raise _fail("SDAI-TRACE-001", "trace node must be a mapping")
        allowed = {"node_id", "type", "entity_id", "label", "metadata", "provenance"}
        if set(value) != allowed:
            raise _fail("SDAI-TRACE-001", "trace node fields do not match sdai.trace-graph/v1")
        try:
            node_type = TraceNodeType(value["type"])
        except (TypeError, ValueError) as exc:
            raise _fail("SDAI-TRACE-001", f"unsupported trace node type: {value.get('type')!r}") from exc
        entity = value["entity_id"]
        if not isinstance(entity, str):
            raise _fail("SDAI-TRACE-001", "trace node entity_id must be a string")
        expected = trace_node_id(node_type, entity)
        if value["node_id"] != expected:
            raise _fail(
                "SDAI-TRACE-001",
                f"trace node node_id mismatch; expected {expected!r}, got {value['node_id']!r}",
            )
        raw_provenance = value["provenance"]
        if not isinstance(raw_provenance, list):
            raise _fail("SDAI-TRACE-002", "trace node provenance must be a list")
        metadata = value["metadata"]
        if not isinstance(metadata, Mapping):
            raise _fail("SDAI-TRACE-001", "trace node metadata must be a mapping")
        label = value["label"]
        if label is not None and not isinstance(label, str):
            raise _fail("SDAI-TRACE-001", "trace node label must be null or a string")
        return cls(
            type=node_type,
            entity_id=entity,
            label=label,
            metadata=dict(metadata),
            provenance=tuple(TraceProvenance.from_mapping(item) for item in raw_provenance),
        )


@dataclass(frozen=True)
class TraceEdge:
    relation: TraceRelation
    source: str
    target: str
    provenance: tuple[TraceProvenance, ...]
    metadata: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        try:
            relation = self.relation if isinstance(self.relation, TraceRelation) else TraceRelation(self.relation)
        except ValueError as exc:
            raise _fail("SDAI-TRACE-001", f"unsupported trace relation: {self.relation!r}") from exc
        object.__setattr__(self, "relation", relation)
        if not isinstance(self.source, str) or not self.source:
            raise _fail("SDAI-TRACE-001", "edge source must be a non-empty node_id")
        if not isinstance(self.target, str) or not self.target:
            raise _fail("SDAI-TRACE-001", "edge target must be a non-empty node_id")
        if self.source == self.target:
            raise _fail("SDAI-TRACE-003", "self-referential trace edges are not allowed")
        object.__setattr__(
            self,
            "provenance",
            _canonical_provenance(self.provenance, label=f"edge {self.edge_id}"),
        )
        object.__setattr__(
            self,
            "metadata",
            _normalize_metadata(self.metadata, label=f"edge {self.edge_id} metadata"),
        )

    @property
    def edge_id(self) -> str:
        return f"{self.relation.value}:{self.source}->{self.target}"

    def semantic_signature(self) -> bytes:
        return _canonical_bytes(
            {
                "relation": self.relation.value,
                "source": self.source,
                "target": self.target,
                "metadata": _metadata_dict(self.metadata),
            }
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "edge_id": self.edge_id,
            "relation": self.relation.value,
            "source": self.source,
            "target": self.target,
            "metadata": _metadata_dict(self.metadata),
            "provenance": [item.as_dict() for item in self.provenance],
        }

    @classmethod
    def from_mapping(cls, value: object) -> "TraceEdge":
        if not isinstance(value, Mapping):
            raise _fail("SDAI-TRACE-001", "trace edge must be a mapping")
        allowed = {"edge_id", "relation", "source", "target", "metadata", "provenance"}
        if set(value) != allowed:
            raise _fail("SDAI-TRACE-001", "trace edge fields do not match sdai.trace-graph/v1")
        try:
            relation = TraceRelation(value["relation"])
        except (TypeError, ValueError) as exc:
            raise _fail("SDAI-TRACE-001", f"unsupported trace relation: {value.get('relation')!r}") from exc
        source = value["source"]
        target = value["target"]
        if not isinstance(source, str) or not isinstance(target, str):
            raise _fail("SDAI-TRACE-001", "trace edge source/target must be strings")
        raw_provenance = value["provenance"]
        if not isinstance(raw_provenance, list):
            raise _fail("SDAI-TRACE-002", "trace edge provenance must be a list")
        metadata = value["metadata"]
        if not isinstance(metadata, Mapping):
            raise _fail("SDAI-TRACE-001", "trace edge metadata must be a mapping")
        edge = cls(
            relation=relation,
            source=source,
            target=target,
            metadata=dict(metadata),
            provenance=tuple(TraceProvenance.from_mapping(item) for item in raw_provenance),
        )
        if value["edge_id"] != edge.edge_id:
            raise _fail(
                "SDAI-TRACE-001",
                f"trace edge edge_id mismatch; expected {edge.edge_id!r}, got {value['edge_id']!r}",
            )
        return edge


def _merge_provenance(
    left: tuple[TraceProvenance, ...],
    right: tuple[TraceProvenance, ...],
    *,
    label: str,
) -> tuple[TraceProvenance, ...]:
    return _canonical_provenance((*left, *right), label=label)


@dataclass(frozen=True)
class TraceGraph:
    feature_id: str
    nodes: tuple[TraceNode, ...]
    edges: tuple[TraceEdge, ...]

    def __post_init__(self) -> None:
        feature = validate_feature_id(self.feature_id)
        object.__setattr__(self, "feature_id", feature)
        merged_nodes: dict[str, TraceNode] = {}
        for node in self.nodes:
            if not isinstance(node, TraceNode):
                raise _fail("SDAI-TRACE-001", "graph nodes must be TraceNode objects")
            existing = merged_nodes.get(node.node_id)
            if existing is None:
                merged_nodes[node.node_id] = node
                continue
            if existing.semantic_signature() != node.semantic_signature():
                raise _fail(
                    "SDAI-TRACE-004",
                    f"conflicting duplicate trace node declaration: {node.node_id}",
                )
            merged_nodes[node.node_id] = TraceNode(
                type=existing.type,
                entity_id=existing.entity_id,
                label=existing.label,
                metadata=_metadata_dict(existing.metadata),
                provenance=_merge_provenance(
                    existing.provenance,
                    node.provenance,
                    label=f"node {node.node_id}",
                ),
            )
        canonical_nodes = tuple(
            sorted(
                merged_nodes.values(),
                key=lambda item: (item.type.value, item.entity_id.casefold(), item.entity_id),
            )
        )
        node_map = {node.node_id: node for node in canonical_nodes}

        merged_edges: dict[str, TraceEdge] = {}
        for edge in self.edges:
            if not isinstance(edge, TraceEdge):
                raise _fail("SDAI-TRACE-001", "graph edges must be TraceEdge objects")
            source = node_map.get(edge.source)
            target = node_map.get(edge.target)
            if source is None or target is None:
                missing = edge.source if source is None else edge.target
                raise _fail(
                    "SDAI-TRACE-003",
                    f"trace edge {edge.edge_id} references missing endpoint {missing!r}",
                )
            allowed_sources, allowed_targets = _RELATION_ENDPOINTS[edge.relation]
            if source.type not in allowed_sources or target.type not in allowed_targets:
                raise _fail(
                    "SDAI-TRACE-003",
                    f"relation {edge.relation.value!r} does not allow {source.type.value} -> {target.type.value}",
                )
            existing = merged_edges.get(edge.edge_id)
            if existing is None:
                merged_edges[edge.edge_id] = edge
                continue
            if existing.semantic_signature() != edge.semantic_signature():
                raise _fail(
                    "SDAI-TRACE-004",
                    f"conflicting duplicate trace edge declaration: {edge.edge_id}",
                )
            merged_edges[edge.edge_id] = TraceEdge(
                relation=existing.relation,
                source=existing.source,
                target=existing.target,
                metadata=_metadata_dict(existing.metadata),
                provenance=_merge_provenance(
                    existing.provenance,
                    edge.provenance,
                    label=f"edge {edge.edge_id}",
                ),
            )
        canonical_edges = tuple(
            sorted(
                merged_edges.values(),
                key=lambda item: (
                    item.relation.value,
                    item.source.casefold(),
                    item.source,
                    item.target.casefold(),
                    item.target,
                ),
            )
        )
        object.__setattr__(self, "nodes", canonical_nodes)
        object.__setattr__(self, "edges", canonical_edges)

    @property
    def node_map(self) -> Mapping[str, TraceNode]:
        return MappingProxyType({item.node_id: item for item in self.nodes})

    @property
    def edge_map(self) -> Mapping[str, TraceEdge]:
        return MappingProxyType({item.edge_id: item for item in self.edges})

    def as_dict(self) -> dict[str, object]:
        return {
            "apiVersion": TRACE_GRAPH_API_VERSION,
            "feature_id": self.feature_id,
            "nodes": [item.as_dict() for item in self.nodes],
            "edges": [item.as_dict() for item in self.edges],
        }

    @property
    def sha256(self) -> str:
        return _sha256_bytes(_canonical_bytes(self.as_dict()))

    def to_json(self) -> str:
        payload = self.as_dict()
        payload["sha256"] = self.sha256
        return json.dumps(
            payload,
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
        ) + "\n"

    @classmethod
    def from_mapping(cls, value: object) -> "TraceGraph":
        if not isinstance(value, Mapping):
            raise _fail("SDAI-TRACE-001", "trace graph must be a mapping")
        allowed = {"apiVersion", "feature_id", "nodes", "edges", "sha256"}
        if set(value) - allowed:
            raise _fail("SDAI-TRACE-001", "trace graph contains unknown field(s)")
        required = {"apiVersion", "feature_id", "nodes", "edges"}
        if not required <= set(value):
            raise _fail("SDAI-TRACE-001", "trace graph is missing required field(s)")
        if value["apiVersion"] != TRACE_GRAPH_API_VERSION:
            raise _fail(
                "SDAI-TRACE-001",
                f"apiVersion must be {TRACE_GRAPH_API_VERSION!r}",
            )
        feature = value["feature_id"]
        if not isinstance(feature, str):
            raise _fail("SDAI-TRACE-001", "trace graph feature_id must be a string")
        raw_nodes = value["nodes"]
        raw_edges = value["edges"]
        if not isinstance(raw_nodes, list) or not isinstance(raw_edges, list):
            raise _fail("SDAI-TRACE-001", "trace graph nodes/edges must be lists")
        graph = cls(
            feature_id=feature,
            nodes=tuple(TraceNode.from_mapping(item) for item in raw_nodes),
            edges=tuple(TraceEdge.from_mapping(item) for item in raw_edges),
        )
        supplied_sha = value.get("sha256")
        if supplied_sha is not None:
            if not isinstance(supplied_sha, str) or not _SHA256.fullmatch(supplied_sha):
                raise _fail("SDAI-TRACE-001", "trace graph sha256 is invalid")
            if supplied_sha != graph.sha256:
                raise _fail("SDAI-TRACE-005", "trace graph SHA-256 does not match canonical graph content")
        return graph

    @classmethod
    def from_json(cls, content: str) -> "TraceGraph":
        if not isinstance(content, str):
            raise _fail("SDAI-TRACE-001", "trace graph JSON must be text")
        try:
            value = json.loads(content)
        except json.JSONDecodeError as exc:
            raise _fail("SDAI-TRACE-001", f"invalid trace graph JSON: {exc}") from exc
        return cls.from_mapping(value)
