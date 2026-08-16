from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
import json
from pathlib import Path, PurePosixPath
import re
from typing import Iterable, Mapping

from sdai.feature_repositories import (
    FEATURE_REPOSITORIES_PATH,
    FeatureEntityType,
    FeatureRepositoryError,
    ResolvedFeatureRepositories,
    RoutableEntity,
    load_feature_repository_manifest,
    resolve_feature_repositories,
    route_feature_entities,
)
from sdai.models import validate_feature_id
from sdai.spec_changes import DeltaOperationKind, SpecChangeError
from sdai.specification_store_references import (
    SPECIFICATION_STORE_REFERENCES_PATH,
    ResolvedSpecificationStoreReferences,
    SpecificationStoreReferenceError,
    resolve_specification_store_references,
)
from sdai.trace_builder import TraceBuildError, TraceBuildResult, build_feature_trace_graph
from sdai.trace_graph import TraceNode, TraceNodeType


MULTI_REPO_FEATURE_GRAPH_API_VERSION = "sdai.multi-repo-feature-graph/v1"


class MultiRepoFeatureGraphError(RuntimeError):
    """Raised when a multi-repository feature graph cannot be built safely."""


class FeatureGraphNodeType(StrEnum):
    STORE = "store"
    REPOSITORY = "repository"
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
    PR_REFERENCE = "pr-reference"


class FeatureGraphFindingLevel(StrEnum):
    WARNING = "warning"
    ERROR = "error"


_TRACE_NODE_TYPES: Mapping[TraceNodeType, FeatureGraphNodeType] = {
    TraceNodeType.REQUIREMENT: FeatureGraphNodeType.REQUIREMENT,
    TraceNodeType.SCENARIO: FeatureGraphNodeType.SCENARIO,
    TraceNodeType.RFC: FeatureGraphNodeType.RFC,
    TraceNodeType.ADR: FeatureGraphNodeType.ADR,
    TraceNodeType.COMPONENT: FeatureGraphNodeType.COMPONENT,
    TraceNodeType.CONTRACT: FeatureGraphNodeType.CONTRACT,
    TraceNodeType.THREAT: FeatureGraphNodeType.THREAT,
    TraceNodeType.TASK: FeatureGraphNodeType.TASK,
    TraceNodeType.CODE: FeatureGraphNodeType.CODE,
    TraceNodeType.TEST: FeatureGraphNodeType.TEST,
    TraceNodeType.APPROVAL: FeatureGraphNodeType.APPROVAL,
    TraceNodeType.EVIDENCE: FeatureGraphNodeType.EVIDENCE,
}
_ROUTE_TYPES: Mapping[TraceNodeType, FeatureEntityType] = {
    TraceNodeType.REQUIREMENT: FeatureEntityType.REQUIREMENT,
    TraceNodeType.CONTRACT: FeatureEntityType.CONTRACT,
    TraceNodeType.COMPONENT: FeatureEntityType.COMPONENT,
    TraceNodeType.TASK: FeatureEntityType.TASK,
}
_KIND = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")


def _fail(code: str, message: str) -> MultiRepoFeatureGraphError:
    return MultiRepoFeatureGraphError(f"{code}: {message}")


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
            "SDAI-FEATURE-GRAPH-001",
            "feature graph data must be canonical finite JSON",
        ) from exc


def _sha256_json(value: object) -> str:
    return "sha256:" + sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _text(value: object, *, label: str, maximum: int = 1024) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise _fail("SDAI-FEATURE-GRAPH-001", f"{label} must be non-empty normalized text")
    if len(value) > maximum or any(ord(character) < 32 for character in value):
        raise _fail("SDAI-FEATURE-GRAPH-001", f"{label} is not portable bounded text")
    return value


def _kind(value: object, *, label: str) -> str:
    candidate = _text(value, label=label, maximum=64)
    if not _KIND.fullmatch(candidate):
        raise _fail("SDAI-FEATURE-GRAPH-001", f"{label} must be a portable lowercase kind")
    return candidate


def _payload_json(value: Mapping[str, object]) -> str:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise _fail("SDAI-FEATURE-GRAPH-001", "feature graph fact payload must be a string-keyed mapping")
    rendered = _canonical_json(dict(value))
    decoded = json.loads(rendered)
    if not isinstance(decoded, dict):
        raise _fail("SDAI-FEATURE-GRAPH-001", "feature graph fact payload must normalize to a mapping")
    return rendered


@dataclass(frozen=True)
class FeatureGraphFact:
    kind: str
    participant: str
    payload_json: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", _kind(self.kind, label="fact kind"))
        object.__setattr__(
            self,
            "participant",
            _text(self.participant, label="fact participant", maximum=512),
        )
        try:
            payload = json.loads(self.payload_json)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise _fail("SDAI-FEATURE-GRAPH-001", "fact payload_json must be canonical JSON") from exc
        canonical = _payload_json(payload)
        if canonical != self.payload_json:
            raise _fail("SDAI-FEATURE-GRAPH-001", "fact payload_json must use canonical JSON")

    @classmethod
    def create(
        cls,
        kind: str,
        participant: str,
        payload: Mapping[str, object],
    ) -> "FeatureGraphFact":
        return cls(kind, participant, _payload_json(payload))

    @property
    def payload(self) -> dict[str, object]:
        value = json.loads(self.payload_json)
        if not isinstance(value, dict):
            raise _fail("SDAI-FEATURE-GRAPH-001", "fact payload must be a mapping")
        return value

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "participant": self.participant,
            "payload": self.payload,
            "sha256": self.sha256,
        }

    @property
    def sha256(self) -> str:
        return _sha256_json(
            {
                "kind": self.kind,
                "participant": self.participant,
                "payload": self.payload,
            }
        )


@dataclass(frozen=True)
class MultiRepoFeatureNode:
    node_id: str
    type: FeatureGraphNodeType
    facts: tuple[FeatureGraphFact, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "node_id", _text(self.node_id, label="node id", maximum=512))
        try:
            node_type = self.type if isinstance(self.type, FeatureGraphNodeType) else FeatureGraphNodeType(self.type)
        except ValueError as exc:
            raise _fail("SDAI-FEATURE-GRAPH-001", f"unsupported node type: {self.type!r}") from exc
        object.__setattr__(self, "type", node_type)
        if not isinstance(self.facts, (tuple, list)) or not self.facts:
            raise _fail("SDAI-FEATURE-GRAPH-001", f"node {self.node_id!r} requires at least one fact")
        if not all(isinstance(item, FeatureGraphFact) for item in self.facts):
            raise _fail("SDAI-FEATURE-GRAPH-001", "node facts contain an invalid fact")
        unique = {item.sha256: item for item in self.facts}
        ordered = tuple(
            sorted(
                unique.values(),
                key=lambda item: (item.kind, item.participant, item.sha256),
            )
        )
        object.__setattr__(self, "facts", ordered)

    def as_dict(self) -> dict[str, object]:
        return {
            "facts": [item.as_dict() for item in self.facts],
            "nodeId": self.node_id,
            "type": self.type.value,
        }


@dataclass(frozen=True)
class MultiRepoFeatureEdge:
    relation: str
    source: str
    target: str
    facts: tuple[FeatureGraphFact, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "relation", _kind(self.relation, label="edge relation"))
        object.__setattr__(self, "source", _text(self.source, label="edge source", maximum=512))
        object.__setattr__(self, "target", _text(self.target, label="edge target", maximum=512))
        if self.source == self.target:
            raise _fail("SDAI-FEATURE-GRAPH-001", "self-referential feature graph edges are not allowed")
        if not isinstance(self.facts, (tuple, list)) or not self.facts:
            raise _fail("SDAI-FEATURE-GRAPH-001", "feature graph edge requires at least one fact")
        if not all(isinstance(item, FeatureGraphFact) for item in self.facts):
            raise _fail("SDAI-FEATURE-GRAPH-001", "edge facts contain an invalid fact")
        unique = {item.sha256: item for item in self.facts}
        object.__setattr__(
            self,
            "facts",
            tuple(
                sorted(
                    unique.values(),
                    key=lambda item: (item.kind, item.participant, item.sha256),
                )
            ),
        )

    @property
    def edge_id(self) -> str:
        return f"{self.relation}:{self.source}->{self.target}"

    def as_dict(self) -> dict[str, object]:
        return {
            "edgeId": self.edge_id,
            "facts": [item.as_dict() for item in self.facts],
            "relation": self.relation,
            "source": self.source,
            "target": self.target,
        }


@dataclass(frozen=True)
class FeatureGraphFinding:
    level: FeatureGraphFindingLevel
    code: str
    message: str
    subject: str
    participant: str | None = None

    def __post_init__(self) -> None:
        try:
            level = self.level if isinstance(self.level, FeatureGraphFindingLevel) else FeatureGraphFindingLevel(self.level)
        except ValueError as exc:
            raise _fail("SDAI-FEATURE-GRAPH-001", f"unsupported finding level: {self.level!r}") from exc
        object.__setattr__(self, "level", level)
        object.__setattr__(self, "code", _text(self.code, label="finding code", maximum=128))
        object.__setattr__(self, "message", _text(self.message, label="finding message", maximum=4096))
        object.__setattr__(self, "subject", _text(self.subject, label="finding subject", maximum=512))
        if self.participant is not None:
            object.__setattr__(
                self,
                "participant",
                _text(self.participant, label="finding participant", maximum=512),
            )

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "code": self.code,
            "level": self.level.value,
            "message": self.message,
            "subject": self.subject,
        }
        if self.participant is not None:
            payload["participant"] = self.participant
        return payload


@dataclass(frozen=True)
class MultiRepoFeatureGraph:
    feature_id: str
    nodes: tuple[MultiRepoFeatureNode, ...]
    edges: tuple[MultiRepoFeatureEdge, ...]
    findings: tuple[FeatureGraphFinding, ...]
    store_resolution_sha256: str | None = None
    repository_resolution_sha256: str | None = None

    def __post_init__(self) -> None:
        feature = validate_feature_id(self.feature_id)
        object.__setattr__(self, "feature_id", feature)
        node_map: dict[str, MultiRepoFeatureNode] = {}
        for node in self.nodes:
            if not isinstance(node, MultiRepoFeatureNode):
                raise _fail("SDAI-FEATURE-GRAPH-001", "graph contains an invalid node")
            existing = node_map.get(node.node_id)
            if existing is not None and existing.type is not node.type:
                raise _fail(
                    "SDAI-FEATURE-GRAPH-001",
                    f"node identity {node.node_id!r} has conflicting types",
                )
            node_map[node.node_id] = (
                node
                if existing is None
                else MultiRepoFeatureNode(node.node_id, node.type, (*existing.facts, *node.facts))
            )
        canonical_nodes = tuple(
            sorted(node_map.values(), key=lambda item: (item.type.value, item.node_id.casefold(), item.node_id))
        )
        known = set(node_map)
        edge_map: dict[str, MultiRepoFeatureEdge] = {}
        for edge in self.edges:
            if not isinstance(edge, MultiRepoFeatureEdge):
                raise _fail("SDAI-FEATURE-GRAPH-001", "graph contains an invalid edge")
            if edge.source not in known or edge.target not in known:
                raise _fail(
                    "SDAI-FEATURE-GRAPH-001",
                    f"edge {edge.edge_id!r} references a missing node",
                )
            existing = edge_map.get(edge.edge_id)
            edge_map[edge.edge_id] = (
                edge
                if existing is None
                else MultiRepoFeatureEdge(
                    edge.relation,
                    edge.source,
                    edge.target,
                    (*existing.facts, *edge.facts),
                )
            )
        canonical_edges = tuple(
            sorted(
                edge_map.values(),
                key=lambda item: (item.relation, item.source.casefold(), item.source, item.target.casefold(), item.target),
            )
        )
        canonical_findings = tuple(
            sorted(
                self.findings,
                key=lambda item: (
                    item.level.value,
                    item.code,
                    item.subject.casefold(),
                    item.subject,
                    item.participant or "",
                    item.message,
                ),
            )
        )
        object.__setattr__(self, "nodes", canonical_nodes)
        object.__setattr__(self, "edges", canonical_edges)
        object.__setattr__(self, "findings", canonical_findings)
        for label, value in (
            ("store resolution sha256", self.store_resolution_sha256),
            ("repository resolution sha256", self.repository_resolution_sha256),
        ):
            if value is not None and (not isinstance(value, str) or not _HASH.fullmatch(value)):
                raise _fail("SDAI-FEATURE-GRAPH-001", f"{label} must be a lowercase SHA-256 digest")

    @property
    def has_errors(self) -> bool:
        return any(item.level is FeatureGraphFindingLevel.ERROR for item in self.findings)

    def as_dict(self) -> dict[str, object]:
        return {
            "apiVersion": MULTI_REPO_FEATURE_GRAPH_API_VERSION,
            "edges": [item.as_dict() for item in self.edges],
            "featureId": self.feature_id,
            "findings": [item.as_dict() for item in self.findings],
            "inputs": {
                "repositoryResolutionSha256": self.repository_resolution_sha256,
                "storeResolutionSha256": self.store_resolution_sha256,
            },
            "nodes": [item.as_dict() for item in self.nodes],
        }

    @property
    def sha256(self) -> str:
        return _sha256_json(self.as_dict())

    def to_json(self) -> str:
        payload = self.as_dict()
        payload["graphSha256"] = self.sha256
        return _canonical_json(payload)


class _GraphBuilder:
    def __init__(self, feature_id: str) -> None:
        self.feature_id = feature_id
        self.nodes: dict[str, tuple[FeatureGraphNodeType, list[FeatureGraphFact]]] = {}
        self.edges: dict[tuple[str, str, str], list[FeatureGraphFact]] = {}
        self.findings: list[FeatureGraphFinding] = []
        self.routable: dict[str, RoutableEntity] = {}
        self.trace_semantics: dict[str, set[str]] = {}
        self.trace_nodes_by_repository: dict[str, set[str]] = {}
        self.trace_graph_by_repository: dict[str, str] = {}
        self.routed_by_repository: dict[str, set[str]] = {}
        self.store_resolution_sha256: str | None = None
        self.repository_resolution_sha256: str | None = None

    def add_node(self, node_id: str, node_type: FeatureGraphNodeType, fact: FeatureGraphFact) -> None:
        existing = self.nodes.get(node_id)
        if existing is not None and existing[0] is not node_type:
            raise _fail(
                "SDAI-FEATURE-GRAPH-001",
                f"node identity {node_id!r} has conflicting types",
            )
        if existing is None:
            self.nodes[node_id] = (node_type, [fact])
        else:
            existing[1].append(fact)

    def add_edge(self, relation: str, source: str, target: str, fact: FeatureGraphFact) -> None:
        self.edges.setdefault((relation, source, target), []).append(fact)

    def finding(
        self,
        level: FeatureGraphFindingLevel,
        code: str,
        message: str,
        subject: str,
        participant: str | None = None,
    ) -> None:
        candidate = FeatureGraphFinding(level, code, message, subject, participant)
        if candidate not in self.findings:
            self.findings.append(candidate)

    def add_routable(self, entity: RoutableEntity) -> None:
        existing = self.routable.get(entity.identity)
        if existing is None or (entity.required and not existing.required):
            self.routable[entity.identity] = entity

    def finish(self) -> MultiRepoFeatureGraph:
        nodes = tuple(
            MultiRepoFeatureNode(node_id, node_type, tuple(facts))
            for node_id, (node_type, facts) in self.nodes.items()
        )
        edges = tuple(
            MultiRepoFeatureEdge(relation, source, target, tuple(facts))
            for (relation, source, target), facts in self.edges.items()
        )
        return MultiRepoFeatureGraph(
            self.feature_id,
            nodes,
            edges,
            tuple(self.findings),
            self.store_resolution_sha256,
            self.repository_resolution_sha256,
        )


def _project_root(project_root: Path) -> Path:
    try:
        root = Path(project_root).resolve(strict=True)
    except (OSError, RuntimeError, TypeError, UnicodeError, ValueError) as exc:
        raise _fail("SDAI-FEATURE-GRAPH-001", "project root must be an existing local directory") from exc
    if not root.is_dir() or not (root / ".sdai" / "config.yaml").is_file():
        raise _fail("SDAI-FEATURE-GRAPH-001", "project must be initialized before building a feature graph")
    return root


def _requirement_node_id(requirement_id: str) -> str:
    return f"requirement:{requirement_id}"


def _store_node_id(identity: str) -> str:
    return f"store:{identity}"


def _repository_node_id(repository_id: str) -> str:
    return f"repository:{repository_id}"


def _store_change_exists(reference: object, feature: str) -> bool:
    manifest = reference.manifest
    if "changes" not in manifest.capabilities:
        return False
    changes_root = next(
        (item.path for item in manifest.specification_roots if item.id == "changes"),
        None,
    )
    if changes_root is None:
        return False
    expected = (PurePosixPath(changes_root) / feature / "change.yaml").as_posix()
    return reference.snapshot.entry(expected) is not None


def _content_sha256(provenance: object, source: str) -> str | None:
    return next((item.sha256 for item in provenance.content if item.path == source), None)


def _add_store_requirements(
    builder: _GraphBuilder,
    stores: ResolvedSpecificationStoreReferences,
    feature: str,
) -> None:
    builder.store_resolution_sha256 = stores.sha256
    for reference in stores.references:
        store_id = _store_node_id(reference.identity)
        builder.add_node(
            store_id,
            FeatureGraphNodeType.STORE,
            FeatureGraphFact.create(
                "store-snapshot",
                reference.identity,
                {
                    "capabilities": list(reference.manifest.capabilities),
                    "manifestSha256": reference.manifest.sha256,
                    "ordinal": reference.ordinal,
                    "snapshotSha256": reference.snapshot.sha256,
                },
            ),
        )
        if not _store_change_exists(reference, feature):
            continue
        try:
            referenced = reference.read_change(feature)
        except (SpecificationStoreReferenceError, SpecChangeError):
            builder.finding(
                FeatureGraphFindingLevel.ERROR,
                "SDAI-FEATURE-GRAPH-STALE-CONTENT",
                "referenced SpecificationStore feature content is stale, invalid, or unreadable",
                feature,
                reference.identity,
            )
            continue
        change = referenced.change
        builder.add_node(
            store_id,
            FeatureGraphNodeType.STORE,
            FeatureGraphFact.create(
                "store-feature",
                reference.identity,
                {
                    "domains": list(change.metadata.domains),
                    "featureId": change.metadata.feature_id,
                    "source": change.metadata.source,
                    "sourceSha256": _content_sha256(referenced.provenance, change.metadata.source),
                    "status": change.metadata.status.value,
                    "title": change.metadata.title,
                },
            ),
        )
        for delta in change.deltas:
            source_sha256 = _content_sha256(referenced.provenance, delta.source)
            for operation in delta.operations:
                identities: list[tuple[str, str]] = [(operation.requirement_id, "primary")]
                if operation.op is DeltaOperationKind.RENAMED and operation.new_requirement_id is not None:
                    identities.append((operation.new_requirement_id, "renamed-target"))
                for requirement_id, role in identities:
                    node_id = _requirement_node_id(requirement_id)
                    payload = {
                        "baselineSpecSha256": delta.baseline_spec_sha256,
                        "domain": delta.domain,
                        "operation": operation.as_dict(),
                        "role": role,
                        "source": delta.source,
                        "sourceSha256": source_sha256,
                        "storeIdentity": reference.identity,
                        "storeSnapshotSha256": reference.snapshot.sha256,
                    }
                    fact = FeatureGraphFact.create("store-requirement", reference.identity, payload)
                    builder.add_node(node_id, FeatureGraphNodeType.REQUIREMENT, fact)
                    builder.add_edge(
                        "declares",
                        store_id,
                        node_id,
                        FeatureGraphFact.create(
                            "store-declaration",
                            reference.identity,
                            {
                                "domain": delta.domain,
                                "operation": operation.op.value,
                                "source": delta.source,
                                "sourceSha256": source_sha256,
                            },
                        ),
                    )
                    builder.add_routable(
                        RoutableEntity(FeatureEntityType.REQUIREMENT, requirement_id)
                    )
                if operation.op is DeltaOperationKind.RENAMED and operation.new_requirement_id is not None:
                    builder.add_edge(
                        "renamed-to",
                        _requirement_node_id(operation.requirement_id),
                        _requirement_node_id(operation.new_requirement_id),
                        FeatureGraphFact.create(
                            "store-rename",
                            reference.identity,
                            {
                                "domain": delta.domain,
                                "source": delta.source,
                                "sourceSha256": source_sha256,
                            },
                        ),
                    )


def _load_stores(builder: _GraphBuilder, root: Path, feature: str) -> None:
    declaration = root / SPECIFICATION_STORE_REFERENCES_PATH
    if not declaration.exists():
        builder.finding(
            FeatureGraphFindingLevel.WARNING,
            "SDAI-FEATURE-GRAPH-NO-STORE",
            "no SpecificationStore references are registered for this project",
            feature,
        )
        return
    try:
        stores = resolve_specification_store_references(root)
    except SpecificationStoreReferenceError:
        builder.finding(
            FeatureGraphFindingLevel.ERROR,
            "SDAI-FEATURE-GRAPH-STALE-CONTENT",
            "SpecificationStore references are stale, unsafe, or invalid",
            SPECIFICATION_STORE_REFERENCES_PATH,
        )
        return
    _add_store_requirements(builder, stores, feature)


def _add_repository_declarations(builder: _GraphBuilder, root: Path) -> None:
    declaration = root / FEATURE_REPOSITORIES_PATH
    if not declaration.exists():
        builder.finding(
            FeatureGraphFindingLevel.ERROR,
            "SDAI-FEATURE-GRAPH-MISSING-REPOSITORIES",
            "feature repository ownership declaration is missing",
            FEATURE_REPOSITORIES_PATH,
        )
        return
    try:
        manifest = load_feature_repository_manifest(root)
    except FeatureRepositoryError:
        builder.finding(
            FeatureGraphFindingLevel.ERROR,
            "SDAI-FEATURE-GRAPH-AMBIGUOUS-REPOSITORIES",
            "feature repository ownership declaration is invalid or unsafe",
            FEATURE_REPOSITORIES_PATH,
        )
        return
    for ordinal, repository in enumerate(manifest.repositories, start=1):
        builder.add_node(
            _repository_node_id(repository.id),
            FeatureGraphNodeType.REPOSITORY,
            FeatureGraphFact.create(
                "repository-declaration",
                repository.id,
                {
                    "capabilities": list(repository.capabilities),
                    "manifestSha256": manifest.sha256,
                    "ordinal": ordinal,
                    "ownership": [selector.as_dict() for selector in repository.ownership],
                    "required": repository.required,
                    "source": manifest.source,
                    "sourceSha256": manifest.source_sha256,
                },
            ),
        )


def _resolve_repositories(
    builder: _GraphBuilder,
    root: Path,
) -> ResolvedFeatureRepositories | None:
    _add_repository_declarations(builder, root)
    if not (root / FEATURE_REPOSITORIES_PATH).exists():
        return None
    try:
        resolved = resolve_feature_repositories(root)
    except FeatureRepositoryError as exc:
        message = str(exc)
        code = (
            "SDAI-FEATURE-GRAPH-MISSING-REPOSITORY"
            if "SDAI-FEATURE-REPO-004" in message
            else "SDAI-FEATURE-GRAPH-AMBIGUOUS-REPOSITORIES"
        )
        detail = (
            "required feature repository participant is missing or unavailable"
            if code == "SDAI-FEATURE-GRAPH-MISSING-REPOSITORY"
            else "feature repository participants cannot be resolved safely"
        )
        builder.finding(
            FeatureGraphFindingLevel.ERROR,
            code,
            detail,
            FEATURE_REPOSITORIES_PATH,
        )
        return None
    builder.repository_resolution_sha256 = resolved.sha256
    for repository in resolved.repositories:
        builder.add_node(
            _repository_node_id(repository.repository.id),
            FeatureGraphNodeType.REPOSITORY,
            FeatureGraphFact.create(
                "repository-resolution",
                repository.repository.id,
                {
                    "available": repository.available,
                    "manifestSha256": resolved.manifest_sha256,
                    "ordinal": repository.ordinal,
                    "resolutionSha256": resolved.sha256,
                    "source": resolved.source,
                    "sourceSha256": resolved.source_sha256,
                },
            ),
        )
        if not repository.available:
            builder.finding(
                FeatureGraphFindingLevel.WARNING,
                "SDAI-FEATURE-GRAPH-MISSING-REPOSITORY",
                "optional repository participant is unavailable",
                _repository_node_id(repository.repository.id),
                repository.repository.id,
            )
    return resolved


def _trace_semantic_sha256(node: TraceNode) -> str:
    return _sha256_json(
        {
            "entityId": node.entity_id,
            "label": node.label,
            "metadata": dict(node.metadata or {}),
            "type": node.type.value,
        }
    )


def _check_trace_provenance(
    builder: _GraphBuilder,
    repository_id: str,
    repository_root: Path,
    result: TraceBuildResult,
) -> None:
    seen: set[tuple[str, str]] = set()
    for node in result.graph.nodes:
        for provenance in node.provenance:
            if provenance.declaration_sha256 is None:
                continue
            key = (provenance.source, provenance.declaration_sha256)
            if key in seen:
                continue
            seen.add(key)
            candidate = repository_root / Path(provenance.source)
            try:
                resolved = candidate.resolve(strict=True)
                resolved.relative_to(repository_root.resolve(strict=True))
                if candidate.is_symlink() or not resolved.is_file():
                    raise OSError("unsafe trace provenance source")
                digest = "sha256:" + sha256(resolved.read_bytes()).hexdigest()
            except (OSError, RuntimeError, UnicodeError, ValueError):
                digest = None
            if digest != provenance.declaration_sha256:
                builder.finding(
                    FeatureGraphFindingLevel.ERROR,
                    "SDAI-FEATURE-GRAPH-STALE-CONTENT",
                    "repository trace provenance no longer matches its declaration SHA-256",
                    provenance.source,
                    repository_id,
                )


def _add_repository_trace(
    builder: _GraphBuilder,
    repository_id: str,
    repository_root: Path,
    result: TraceBuildResult,
) -> None:
    builder.trace_graph_by_repository[repository_id] = result.graph.sha256
    builder.trace_nodes_by_repository.setdefault(repository_id, set())
    builder.add_node(
        _repository_node_id(repository_id),
        FeatureGraphNodeType.REPOSITORY,
        FeatureGraphFact.create(
            "repository-trace",
            repository_id,
            {
                "gaps": len(result.gaps),
                "traceBuildSha256": result.sha256,
                "traceGraphSha256": result.graph.sha256,
            },
        ),
    )
    for node in result.graph.nodes:
        node_type = _TRACE_NODE_TYPES[node.type]
        fact = FeatureGraphFact.create(
            "trace-node",
            repository_id,
            {
                "traceGraphSha256": result.graph.sha256,
                "value": node.as_dict(),
            },
        )
        builder.add_node(node.node_id, node_type, fact)
        builder.trace_nodes_by_repository[repository_id].add(node.node_id)
        semantic = _trace_semantic_sha256(node)
        semantics = builder.trace_semantics.setdefault(node.node_id, set())
        semantics.add(semantic)
        route_type = _ROUTE_TYPES.get(node.type)
        if route_type is not None:
            builder.add_routable(RoutableEntity(route_type, node.entity_id))
    for edge in result.graph.edges:
        builder.add_edge(
            edge.relation.value,
            edge.source,
            edge.target,
            FeatureGraphFact.create(
                "trace-edge",
                repository_id,
                {
                    "traceGraphSha256": result.graph.sha256,
                    "value": edge.as_dict(),
                },
            ),
        )
    for gap in result.gaps:
        builder.finding(
            FeatureGraphFindingLevel.WARNING,
            "SDAI-FEATURE-GRAPH-DISCONNECTED",
            f"repository trace gap {gap.kind}: {gap.detail or gap.target}",
            gap.source_node_id or gap.target,
            repository_id,
        )
    _check_trace_provenance(builder, repository_id, repository_root, result)


def _load_repository_traces(
    builder: _GraphBuilder,
    resolved: ResolvedFeatureRepositories,
    feature: str,
) -> None:
    for repository in resolved.repositories:
        repository_id = repository.repository.id
        if repository.root is None:
            continue
        feature_dir = repository.root / "specs" / "changes" / feature
        if not feature_dir.is_dir() or feature_dir.is_symlink():
            continue
        try:
            result = build_feature_trace_graph(repository.root, feature, environ={})
        except TraceBuildError as exc:
            builder.finding(
                FeatureGraphFindingLevel.ERROR,
                "SDAI-FEATURE-GRAPH-REPOSITORY-HEALTH",
                f"repository feature trace could not be built safely: {exc}",
                feature,
                repository_id,
            )
            continue
        _add_repository_trace(builder, repository_id, repository.root, result)


def _route_entities(
    builder: _GraphBuilder,
    resolved: ResolvedFeatureRepositories,
) -> None:
    for entity in sorted(builder.routable.values(), key=lambda item: (item.type.value, item.entity_id)):
        try:
            routing = route_feature_entities(resolved, (entity,))
        except FeatureRepositoryError as exc:
            message = str(exc)
            if "SDAI-FEATURE-REPO-005" in message:
                code = "SDAI-FEATURE-GRAPH-AMBIGUOUS-ROUTING"
            elif "SDAI-FEATURE-REPO-004" in message:
                code = "SDAI-FEATURE-GRAPH-MISSING-REPOSITORY"
            else:
                code = "SDAI-FEATURE-GRAPH-MISSING-OWNERSHIP"
            builder.finding(
                FeatureGraphFindingLevel.ERROR,
                code,
                f"entity ownership cannot be resolved: {message}",
                entity.identity,
            )
            continue
        if not routing.decisions:
            continue
        decision = routing.decisions[0]
        repository_node = _repository_node_id(decision.repository_id)
        if repository_node not in builder.nodes:
            raise _fail(
                "SDAI-FEATURE-GRAPH-001",
                f"routing decision references unknown repository {decision.repository_id!r}",
            )
        node_id = entity.identity
        if node_id not in builder.nodes:
            # A routed trace/store entity should already exist. Treat a missing
            # node as a structural bug instead of inventing graph content.
            raise _fail(
                "SDAI-FEATURE-GRAPH-001",
                f"routing input {node_id!r} is missing its canonical graph node",
            )
        builder.add_edge(
            "owned-by",
            node_id,
            repository_node,
            FeatureGraphFact.create(
                "ownership-decision",
                decision.repository_id,
                decision.as_dict() | {"decisionSha256": decision.sha256},
            ),
        )
        builder.routed_by_repository.setdefault(decision.repository_id, set()).add(node_id)


def _add_consistency_findings(builder: _GraphBuilder) -> None:
    for node_id, semantics in sorted(builder.trace_semantics.items()):
        if len(semantics) > 1:
            builder.finding(
                FeatureGraphFindingLevel.ERROR,
                "SDAI-FEATURE-GRAPH-AMBIGUOUS-TRACE",
                "the same trace node identity has conflicting semantic facts across repositories",
                node_id,
            )
    for repository_id, routed in sorted(builder.routed_by_repository.items()):
        trace_nodes = builder.trace_nodes_by_repository.get(repository_id)
        if trace_nodes is None:
            builder.finding(
                FeatureGraphFindingLevel.ERROR,
                "SDAI-FEATURE-GRAPH-MISSING-REPOSITORY-TRACE",
                "repository owns feature entities but has no readable feature trace graph",
                _repository_node_id(repository_id),
                repository_id,
            )
            continue
        for node_id in sorted(routed):
            if node_id not in trace_nodes:
                builder.finding(
                    FeatureGraphFindingLevel.WARNING,
                    "SDAI-FEATURE-GRAPH-DISCONNECTED",
                    "routed entity is not represented by this repository's trace facts",
                    node_id,
                    repository_id,
                )


def build_multi_repo_feature_graph(
    project_root: Path,
    feature_id: str,
) -> MultiRepoFeatureGraph:
    """Compose immutable store, routing, and repository trace facts into one graph.

    The builder is deliberately read-only. It never creates, updates, clones,
    fetches, pulls, pushes, or otherwise mutates a store or repository.
    """
    root = _project_root(project_root)
    feature = validate_feature_id(feature_id)
    builder = _GraphBuilder(feature)
    _load_stores(builder, root, feature)
    resolved = _resolve_repositories(builder, root)
    if resolved is not None:
        _load_repository_traces(builder, resolved, feature)
        _route_entities(builder, resolved)
    _add_consistency_findings(builder)
    return builder.finish()