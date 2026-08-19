from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Mapping

from sdai.audit_trace import AuditTraceError, AuditTraceGap, build_audit_trace_index
from sdai.contract_trace import (
    ContractTraceError,
    ContractTraceGap,
    build_contract_trace_index,
    build_contract_trace_links,
)
from sdai.cross_artifact import FeatureArtifactIndex, IndexedEntity, build_feature_artifact_index
from sdai.models import validate_feature_id
from sdai.path_safety import PathSafetyError, ensure_within_project
from sdai.text import TextEncodingError, read_utf8_text
from sdai.trace_evidence import TRACE_EVIDENCE_API_VERSION, TraceEvidence, TraceEvidenceError
from sdai.trace_graph import (
    TraceEdge,
    TraceGraph,
    TraceGraphError,
    TraceNode,
    TraceNodeType,
    TraceProvenance,
    TraceRelation,
)


TRACE_BUILD_API_VERSION = "sdai.trace-build/v1"


class TraceBuildError(RuntimeError):
    """Raised when a canonical feature trace graph cannot be built safely."""


_KIND_MAP: Mapping[str, TraceNodeType] = {
    "requirement": TraceNodeType.REQUIREMENT,
    "scenario": TraceNodeType.SCENARIO,
    "adr": TraceNodeType.ADR,
    "contract": TraceNodeType.CONTRACT,
    "threat": TraceNodeType.THREAT,
    "task": TraceNodeType.TASK,
    "test": TraceNodeType.TEST,
    "approval": TraceNodeType.APPROVAL,
}
_SOURCE_SUFFIXES = frozenset(
    {
        ".py", ".java", ".kt", ".kts", ".cs", ".fs", ".go", ".rs",
        ".js", ".jsx", ".ts", ".tsx", ".c", ".cc", ".cpp", ".h", ".hpp",
        ".sh", ".bash", ".ps1", ".rb", ".php", ".scala", ".swift",
    }
)
_EXCLUDED_PARTS = frozenset(
    {
        ".git", ".sdai", ".venv", "venv", "node_modules", "__pycache__",
        "dist", "build", "target", ".idea", ".vscode",
    }
)
_SPECIAL_DECLARATION = re.compile(
    r"^\s*(?:(?:#{1,6}|[-*+])\s+)?"
    r"(?P<id>(?:RFC|COMPONENT)-[A-Za-z0-9][A-Za-z0-9._-]{0,126})"
    r"\s*(?::|[-–—]|$)\s*(?P<title>.*)$",
    re.IGNORECASE,
)
_ID_REFERENCE = re.compile(
    r"(?<![A-Za-z0-9])"
    r"(?P<id>(?:FR|NFR|REQ|AC|SCN|TASK|TEST|ADR|CONTRACT|API|EVENT|SCHEMA|THREAT|APPROVAL|RFC|COMPONENT)-"
    r"[A-Za-z0-9][A-Za-z0-9._-]{0,126})"
    r"(?![A-Za-z0-9])",
    re.IGNORECASE,
)


def _fail(code: str, message: str) -> TraceBuildError:
    return TraceBuildError(f"{code}: {message}")


def _hash_bytes(value: bytes) -> str:
    return "sha256:" + sha256(value).hexdigest()


def _canonical_hash(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return _hash_bytes(encoded)


def _portable(root: Path, path: Path, *, label: str) -> str:
    try:
        safe = ensure_within_project(root, path, label=label)
        return safe.relative_to(root.resolve()).as_posix()
    except (PathSafetyError, ValueError) as exc:
        raise _fail("SDAI-TRACE-BUILD-001", f"{label} must remain inside the project root") from exc


def _provenance(source: str, line: int, *, detail: str | None = None) -> tuple[TraceProvenance, ...]:
    return (TraceProvenance(source=source, line=line, detail=detail),)


def _file_entity_id(source: str) -> str:
    """Return an ASCII-portable identity while preserving UTF-8 source in metadata/provenance."""
    return "path-sha256:" + sha256(source.encode("utf-8")).hexdigest()


def _indexed_node(entity: IndexedEntity) -> TraceNode | None:
    node_type = _KIND_MAP.get(entity.kind)
    if node_type is None:
        return None
    return TraceNode(
        type=node_type,
        entity_id=entity.id,
        label=entity.title or None,
        metadata={"status": entity.status} if entity.status is not None else {},
        provenance=_provenance(entity.source, entity.line, detail="cross-artifact declaration"),
    )


def _special_nodes(index: FeatureArtifactIndex, root: Path) -> tuple[TraceNode, ...]:
    nodes: list[TraceNode] = []
    for item in index.files:
        path = root / Path(item.source)
        try:
            text = read_utf8_text(path)
        except (OSError, TextEncodingError) as exc:
            raise _fail(
                "SDAI-TRACE-BUILD-002",
                f"unable to read UTF-8 trace source {item.source!r}: {exc}",
            ) from exc
        for line_number, line in enumerate(text.splitlines(), start=1):
            match = _SPECIAL_DECLARATION.match(line)
            if match is None:
                continue
            entity_id = match.group("id").upper()
            node_type = (
                TraceNodeType.RFC
                if entity_id.startswith("RFC-")
                else TraceNodeType.COMPONENT
            )
            nodes.append(
                TraceNode(
                    type=node_type,
                    entity_id=entity_id,
                    label=match.group("title").strip() or None,
                    provenance=_provenance(
                        item.source,
                        line_number,
                        detail="explicit trace declaration",
                    ),
                )
            )
    return tuple(nodes)


def _is_test_path(relative: Path) -> bool:
    lowered = tuple(part.casefold() for part in relative.parts)
    name = relative.name.casefold()
    stem = relative.stem.casefold()
    return (
        "tests" in lowered
        or "test" in lowered[:-1]
        or name.startswith("test_")
        or stem.endswith("_test")
        or stem.endswith("tests")
        or name.endswith("test.java")
        or name.endswith("tests.cs")
    )


def _repository_source_files(root: Path, feature_dir: Path) -> tuple[Path, ...]:
    result: list[Path] = []
    for path in root.rglob("*"):
        try:
            relative = path.relative_to(root)
        except ValueError:
            continue
        if any(part in _EXCLUDED_PARTS for part in relative.parts):
            continue
        if path == feature_dir or feature_dir in path.parents:
            continue
        if path.is_symlink():
            if path.suffix.casefold() in _SOURCE_SUFFIXES:
                raise _fail(
                    "SDAI-TRACE-BUILD-001",
                    f"repository trace source must not be a symlink: {relative.as_posix()}",
                )
            continue
        if path.is_file() and path.suffix.casefold() in _SOURCE_SUFFIXES:
            result.append(path)
    return tuple(
        sorted(
            result,
            key=lambda value: (
                value.relative_to(root).as_posix().casefold(),
                value.relative_to(root).as_posix(),
            ),
        )
    )


def _id_to_node_ids(nodes: tuple[TraceNode, ...]) -> dict[str, tuple[str, ...]]:
    values: dict[str, set[str]] = {}
    for node in nodes:
        values.setdefault(node.entity_id.upper(), set()).add(node.node_id)
    return {key: tuple(sorted(node_ids)) for key, node_ids in values.items()}


@dataclass(frozen=True)
class TraceGap:
    kind: str
    source: str
    line: int
    target: str
    relation: str
    source_node_id: str | None = None
    detail: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "source": self.source,
            "line": self.line,
            "source_node_id": self.source_node_id,
            "target": self.target,
            "relation": self.relation,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class TraceBuildResult:
    graph: TraceGraph
    gaps: tuple[TraceGap, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "gaps",
            tuple(
                sorted(
                    self.gaps,
                    key=lambda item: (
                        item.kind,
                        item.source.casefold(),
                        item.source,
                        item.line,
                        item.source_node_id or "",
                        item.target,
                        item.relation,
                        item.detail or "",
                    ),
                )
            ),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "apiVersion": TRACE_BUILD_API_VERSION,
            "feature_id": self.graph.feature_id,
            "graph_sha256": self.graph.sha256,
            "gaps": [item.as_dict() for item in self.gaps],
        }

    @property
    def sha256(self) -> str:
        return _canonical_hash(self.as_dict())


def _contract_gap(item: ContractTraceGap) -> TraceGap:
    return TraceGap(
        kind=item.kind,
        source=item.source,
        line=item.line,
        target=item.target,
        relation=item.relation,
        source_node_id=item.source_node_id,
        detail=item.detail,
    )


def _audit_gap(item: AuditTraceGap) -> TraceGap:
    return TraceGap(
        kind=item.kind,
        source=item.source,
        line=item.line,
        target=item.target,
        relation=item.relation,
        source_node_id=item.source_node_id,
        detail=item.detail,
    )


def _cross_artifact_edges(
    index: FeatureArtifactIndex,
    node_ids: Mapping[str, tuple[str, ...]],
) -> tuple[list[TraceEdge], list[TraceGap]]:
    edges: list[TraceEdge] = []
    gaps: list[TraceGap] = []
    for relation in index.relationships:
        sources = node_ids.get(relation.from_id.upper(), ())
        targets = node_ids.get(relation.to_id.upper(), ())
        if len(sources) != 1 or len(targets) != 1:
            missing = relation.from_id if len(sources) != 1 else relation.to_id
            candidates = node_ids.get(missing.upper(), ())
            gaps.append(
                TraceGap(
                    kind="missing-endpoint" if not candidates else "ambiguous-endpoint",
                    source=relation.source,
                    line=relation.line,
                    source_node_id=sources[0] if len(sources) == 1 else None,
                    target=missing,
                    relation=TraceRelation.REFERENCES.value,
                    detail="explicit cross-artifact reference was not uniquely resolvable",
                )
            )
            continue
        if sources[0] == targets[0]:
            continue
        edges.append(
            TraceEdge(
                relation=TraceRelation.REFERENCES,
                source=sources[0],
                target=targets[0],
                provenance=_provenance(
                    relation.source,
                    relation.line,
                    detail="explicit cross-artifact reference",
                ),
                metadata={"declared_relation": relation.relation},
            )
        )
    return edges, gaps


def _repository_nodes_and_edges(
    root: Path,
    feature_dir: Path,
    known_node_ids: Mapping[str, tuple[str, ...]],
) -> tuple[list[TraceNode], list[TraceEdge], list[TraceGap]]:
    nodes: list[TraceNode] = []
    edges: list[TraceEdge] = []
    gaps: list[TraceGap] = []
    for path in _repository_source_files(root, feature_dir):
        source = _portable(root, path, label="repository trace source")
        try:
            text = read_utf8_text(path)
        except (OSError, TextEncodingError) as exc:
            raise _fail(
                "SDAI-TRACE-BUILD-002",
                f"unable to read UTF-8 source {source!r}: {exc}",
            ) from exc
        references: list[tuple[int, str]] = []
        for line_number, line in enumerate(text.splitlines(), start=1):
            for match in _ID_REFERENCE.finditer(line):
                references.append((line_number, match.group("id").upper()))
        if not references:
            continue

        relative = path.relative_to(root)
        node_type = TraceNodeType.TEST if _is_test_path(relative) else TraceNodeType.CODE
        node = TraceNode(
            type=node_type,
            entity_id=_file_entity_id(source),
            label=relative.name,
            metadata={"repository_file": True, "source": source},
            provenance=_provenance(
                source,
                references[0][0],
                detail="first explicit trace reference",
            ),
        )
        nodes.append(node)

        seen: set[tuple[int, str]] = set()
        for line_number, referenced_id in references:
            key = (line_number, referenced_id)
            if key in seen:
                continue
            seen.add(key)
            targets = known_node_ids.get(referenced_id, ())
            if len(targets) != 1:
                gaps.append(
                    TraceGap(
                        kind="missing-endpoint" if not targets else "ambiguous-endpoint",
                        source=source,
                        line=line_number,
                        source_node_id=node.node_id,
                        target=referenced_id,
                        relation=TraceRelation.REFERENCES.value,
                        detail="repository source reference was not uniquely resolvable",
                    )
                )
                continue
            edges.append(
                TraceEdge(
                    relation=TraceRelation.REFERENCES,
                    source=node.node_id,
                    target=targets[0],
                    provenance=_provenance(
                        source,
                        line_number,
                        detail="explicit repository source reference",
                    ),
                )
            )
    return nodes, edges, gaps


def _evidence_candidates(feature_dir: Path) -> tuple[Path, ...]:
    candidates: list[Path] = []
    for path in feature_dir.rglob("*.json"):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        if TRACE_EVIDENCE_API_VERSION.encode("utf-8") in raw:
            candidates.append(path)
    return tuple(sorted(candidates, key=lambda item: (item.as_posix().casefold(), item.as_posix())))


def _evidence_nodes_and_edges(
    root: Path,
    feature_dir: Path,
    graph_nodes: tuple[TraceNode, ...],
) -> tuple[list[TraceNode], list[TraceEdge], list[TraceGap]]:
    nodes: list[TraceNode] = []
    edges: list[TraceEdge] = []
    gaps: list[TraceGap] = []
    node_map = {node.node_id: node for node in graph_nodes}
    evidence_truth: dict[str, str] = {}

    for path in _evidence_candidates(feature_dir):
        source = _portable(root, path, label="trace evidence")
        try:
            record = TraceEvidence.from_json(path.read_bytes())
        except (OSError, TraceEvidenceError) as exc:
            raise _fail(
                "SDAI-TRACE-BUILD-003",
                f"invalid typed trace evidence at {source!r}: {exc}",
            ) from exc

        previous_truth = evidence_truth.get(record.evidence_id)
        if previous_truth is not None and previous_truth != record.truth_sha256:
            raise _fail(
                "SDAI-TRACE-BUILD-004",
                f"conflicting typed evidence truth for {record.evidence_id!r}",
            )
        evidence_truth[record.evidence_id] = record.truth_sha256

        evidence_node = TraceNode(
            type=TraceNodeType.EVIDENCE,
            entity_id=record.evidence_id,
            label=f"{record.kind.value}:{record.status.value}",
            metadata={
                "truth_sha256": record.truth_sha256,
                "kind": record.kind.value,
                "status": record.status.value,
                "git_commit": record.git_commit,
            },
            provenance=_provenance(
                source,
                1,
                detail="validated sdai.trace-evidence/v1 record",
            ),
        )
        nodes.append(evidence_node)

        if record.subject not in node_map:
            gaps.append(
                TraceGap(
                    kind="missing-evidence-subject",
                    source=source,
                    line=1,
                    target=record.subject,
                    relation=TraceRelation.EVIDENCED_BY.value,
                    detail="validated evidence subject is not present in the canonical graph",
                )
            )
            continue
        edges.append(
            TraceEdge(
                relation=TraceRelation.EVIDENCED_BY,
                source=record.subject,
                target=evidence_node.node_id,
                provenance=_provenance(
                    source,
                    1,
                    detail="validated typed evidence subject binding",
                ),
                metadata={"truth_sha256": record.truth_sha256},
            )
        )
    return nodes, edges, gaps


def build_feature_trace_graph(
    project_root: Path,
    feature_id: str,
    *,
    environ: Mapping[str, str] | None = None,
) -> TraceBuildResult:
    """Build a deterministic, provider-free canonical trace graph."""
    root = project_root.resolve()
    feature = validate_feature_id(feature_id)
    try:
        feature_dir = ensure_within_project(
            root,
            root / "specs" / "changes" / feature,
            label="trace feature directory",
        )
    except PathSafetyError as exc:
        raise _fail("SDAI-TRACE-BUILD-001", "feature directory must remain inside project root") from exc
    if not feature_dir.is_dir() or feature_dir.is_symlink():
        raise _fail(
            "SDAI-TRACE-BUILD-001",
            f"feature directory is missing or unsafe: specs/changes/{feature}",
        )

    try:
        index = build_feature_artifact_index(root, feature, environ=environ)
    except RuntimeError as exc:
        raise _fail("SDAI-TRACE-BUILD-002", f"unable to build cross-artifact facts: {exc}") from exc

    nodes: list[TraceNode] = []
    for entity in index.entities:
        node = _indexed_node(entity)
        if node is not None:
            nodes.append(node)
    nodes.extend(_special_nodes(index, root))

    try:
        contract_index = build_contract_trace_index(root)
    except ContractTraceError as exc:
        raise _fail("SDAI-TRACE-BUILD-005", f"unable to build contract trace symbols: {exc}") from exc
    nodes.extend(contract_index.nodes)

    declaration_nodes = tuple(nodes)
    known = _id_to_node_ids(declaration_nodes)
    edges, gaps = _cross_artifact_edges(index, known)
    edges.extend(contract_index.edges)
    gaps.extend(_contract_gap(item) for item in contract_index.gaps)

    repository_nodes, repository_edges, repository_gaps = _repository_nodes_and_edges(
        root,
        feature_dir,
        known,
    )
    nodes.extend(repository_nodes)
    edges.extend(repository_edges)
    gaps.extend(repository_gaps)

    evidence_nodes, evidence_edges, evidence_gaps = _evidence_nodes_and_edges(
        root,
        feature_dir,
        tuple(nodes),
    )
    nodes.extend(evidence_nodes)
    edges.extend(evidence_edges)
    gaps.extend(evidence_gaps)

    try:
        audit_trace = build_audit_trace_index(root, feature, tuple(nodes))
    except AuditTraceError as exc:
        raise _fail("SDAI-TRACE-BUILD-006", f"unable to project audit provenance: {exc}") from exc
    nodes.extend(audit_trace.nodes)
    edges.extend(audit_trace.edges)
    gaps.extend(_audit_gap(item) for item in audit_trace.gaps)

    try:
        contract_links = build_contract_trace_links(root, feature, contract_index, tuple(nodes))
    except ContractTraceError as exc:
        raise _fail("SDAI-TRACE-BUILD-005", f"unable to resolve contract trace links: {exc}") from exc
    edges.extend(contract_links.edges)
    gaps.extend(_contract_gap(item) for item in contract_links.gaps)

    try:
        graph = TraceGraph(feature_id=feature, nodes=tuple(nodes), edges=tuple(edges))
    except TraceGraphError as exc:
        raise _fail("SDAI-TRACE-BUILD-004", f"canonical trace graph conflict: {exc}") from exc
    return TraceBuildResult(graph=graph, gaps=tuple(gaps))
