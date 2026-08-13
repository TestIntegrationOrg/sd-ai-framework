from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Mapping

import yaml

from sdai.artifact_schemas import load_artifact_schema_graph
from sdai.models import validate_feature_id
from sdai.path_safety import ensure_within_project
from sdai.text import TextEncodingError, read_utf8_text


class CrossArtifactError(RuntimeError):
    """Raised when cross-artifact evidence cannot be indexed safely."""


_SUPPORTED_SUFFIXES = frozenset({".md", ".markdown", ".yaml", ".yml", ".json", ".txt"})
_EXCLUDED_DIRS = frozenset({".sdai", "__pycache__", ".git"})
_ID = re.compile(
    r"(?<![A-Za-z0-9])"
    r"(?P<id>(?:FR|NFR|REQ|AC|SCN|TASK|TEST|ADR|CONTRACT|API|EVENT|SCHEMA|THREAT|MITIGATION|APPROVAL)-"
    r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,126}[A-Za-z0-9])?)"
    r"(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_DECLARATION = re.compile(
    r"^\s*(?:(?:#{1,6}|[-*+])\s+)?(?:\[[ xX]\]\s+)?"
    r"(?P<id>(?:FR|NFR|REQ|AC|SCN|TASK|TEST|ADR|CONTRACT|API|EVENT|SCHEMA|THREAT|MITIGATION|APPROVAL)-"
    r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,126}[A-Za-z0-9])?)"
    r"\s*(?::|[-–—]|$)\s*(?P<title>.*)$",
    re.IGNORECASE,
)
_YAML_ID = re.compile(
    r"^\s*(?:id|requirement_id|scenario_id|task_id|test_id|adr_id|contract_id|threat_id|mitigation_id|approval_id)"
    r"\s*:\s*[\"']?(?P<id>(?:FR|NFR|REQ|AC|SCN|TASK|TEST|ADR|CONTRACT|API|EVENT|SCHEMA|THREAT|MITIGATION|APPROVAL)-"
    r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,126}[A-Za-z0-9])?)[\"']?\s*$",
    re.IGNORECASE,
)
_STATUS = re.compile(r"^\s*status\s*:\s*[\"']?(?P<status>[A-Za-z0-9._-]+)[\"']?\s*$", re.IGNORECASE)

_KIND_BY_PREFIX = {
    "FR": "requirement",
    "NFR": "requirement",
    "REQ": "requirement",
    "AC": "scenario",
    "SCN": "scenario",
    "TASK": "task",
    "TEST": "test",
    "ADR": "adr",
    "CONTRACT": "contract",
    "API": "contract",
    "EVENT": "contract",
    "SCHEMA": "contract",
    "THREAT": "threat",
    "MITIGATION": "mitigation",
    "APPROVAL": "approval",
}


def _fail(code: str, message: str) -> CrossArtifactError:
    return CrossArtifactError(f"{code}: {message}")


def _portable(root: Path, path: Path) -> str:
    safe = ensure_within_project(root, path, label="cross-artifact source")
    return safe.relative_to(root.resolve()).as_posix()


def _hash_text(text: str) -> str:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return "sha256:" + sha256(normalized.encode("utf-8")).hexdigest()


def _canonical_hash(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + sha256(encoded).hexdigest()


def _normalize_id(value: str) -> str:
    return value.upper()


def _entity_kind(entity_id: str) -> str:
    prefix = entity_id.split("-", 1)[0].upper()
    return _KIND_BY_PREFIX[prefix]


@dataclass(frozen=True)
class SourceEvidence:
    source: str
    line: int
    entity_id: str | None = None
    detail: str | None = None

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {"source": self.source, "line": self.line}
        if self.entity_id is not None:
            result["entity_id"] = self.entity_id
        if self.detail is not None:
            result["detail"] = self.detail
        return result


@dataclass(frozen=True)
class IndexedFile:
    source: str
    sha256: str
    size_bytes: int

    def as_dict(self) -> dict[str, object]:
        return {
            "source": self.source,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
        }


@dataclass(frozen=True)
class IndexedEntity:
    id: str
    kind: str
    source: str
    line: int
    title: str
    status: str | None = None

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.id}@{self.source}:{self.line}"

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "key": self.key,
            "id": self.id,
            "kind": self.kind,
            "source": self.source,
            "line": self.line,
            "title": self.title,
        }
        if self.status is not None:
            result["status"] = self.status
        return result


@dataclass(frozen=True)
class RelationshipEdge:
    from_id: str
    to_id: str
    relation: str
    source: str
    line: int

    def as_dict(self) -> dict[str, object]:
        return {
            "from_id": self.from_id,
            "to_id": self.to_id,
            "relation": self.relation,
            "source": self.source,
            "line": self.line,
        }


@dataclass(frozen=True)
class SchemaArtifactFact:
    id: str
    path_template: str
    resolved_path: str | None
    type: str
    required: bool
    depends_on: tuple[str, ...]
    source_layer: str
    exists: bool | None

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "path_template": self.path_template,
            "resolved_path": self.resolved_path,
            "type": self.type,
            "required": self.required,
            "depends_on": list(self.depends_on),
            "source_layer": self.source_layer,
            "exists": self.exists,
        }


@dataclass(frozen=True)
class FeatureArtifactIndex:
    feature_id: str
    files: tuple[IndexedFile, ...]
    entities: tuple[IndexedEntity, ...]
    relationships: tuple[RelationshipEdge, ...]
    schema_artifacts: tuple[SchemaArtifactFact, ...]
    schema_sources: tuple[str, ...]
    schema_topological_order: tuple[str, ...]

    def by_id(self) -> dict[str, tuple[IndexedEntity, ...]]:
        result: dict[str, list[IndexedEntity]] = {}
        for entity in self.entities:
            result.setdefault(entity.id, []).append(entity)
        return {key: tuple(value) for key, value in result.items()}

    def as_dict(self) -> dict[str, object]:
        return {
            "apiVersion": "sdai.analysis-index/v1",
            "feature_id": self.feature_id,
            "files": [item.as_dict() for item in self.files],
            "entities": [item.as_dict() for item in self.entities],
            "relationships": [item.as_dict() for item in self.relationships],
            "artifact_graph": {
                "artifacts": [item.as_dict() for item in self.schema_artifacts],
                "sources": list(self.schema_sources),
                "topological_order": list(self.schema_topological_order),
            },
        }

    @property
    def sha256(self) -> str:
        return _canonical_hash(self.as_dict())

    def to_json(self) -> str:
        payload = self.as_dict()
        payload["sha256"] = self.sha256
        return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False)


@dataclass(frozen=True)
class AnalysisFinding:
    code: str
    severity: str
    message: str
    entity_id: str | None = None
    evidence: tuple[SourceEvidence, ...] = ()

    def __post_init__(self) -> None:
        if self.severity not in {"blocking", "warning", "suggestion", "info"}:
            raise ValueError(f"unsupported analysis severity: {self.severity}")
        if not self.code or not re.fullmatch(r"[A-Z][A-Z0-9_]{1,127}", self.code):
            raise ValueError(f"invalid analysis finding code: {self.code}")

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "evidence": [item.as_dict() for item in self.evidence],
        }
        if self.entity_id is not None:
            result["entity_id"] = self.entity_id
        return result


@dataclass(frozen=True)
class AnalysisReport:
    feature_id: str
    index_sha256: str
    findings: tuple[AnalysisFinding, ...]

    def as_dict(self) -> dict[str, object]:
        ordered = tuple(
            sorted(
                self.findings,
                key=lambda item: (
                    item.severity,
                    item.code,
                    item.entity_id or "",
                    item.message,
                    tuple((e.source, e.line, e.entity_id or "", e.detail or "") for e in item.evidence),
                ),
            )
        )
        return {
            "apiVersion": "sdai.findings/v1",
            "feature_id": self.feature_id,
            "index_sha256": self.index_sha256,
            "findings": [item.as_dict() for item in ordered],
        }

    def to_json(self) -> str:
        return json.dumps(
            self.as_dict(),
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
        )


def _feature_dir(root: Path, feature_id: str) -> Path:
    path = ensure_within_project(
        root,
        root / "specs" / "changes" / feature_id,
        label="feature analysis directory",
    )
    if not path.is_dir() or path.is_symlink():
        raise _fail(
            "SDAI-ANALYSIS-001",
            f"feature change directory does not exist as a regular directory: specs/changes/{feature_id}",
        )
    return path


def _files(root: Path, feature_dir: Path) -> tuple[Path, ...]:
    result: list[Path] = []
    for path in feature_dir.rglob("*"):
        relative_parts = path.relative_to(feature_dir).parts
        if any(part in _EXCLUDED_DIRS for part in relative_parts):
            continue
        if path.is_symlink():
            raise _fail(
                "SDAI-ANALYSIS-002",
                f"analysis source must not be a symlink: {_portable(root, path)}",
            )
        if not path.is_file() or path.suffix.casefold() not in _SUPPORTED_SUFFIXES:
            continue
        ensure_within_project(feature_dir, path, label="feature analysis source")
        result.append(path)
    return tuple(sorted(result, key=lambda item: _portable(root, item).casefold()))


def _read_source(root: Path, path: Path) -> str:
    try:
        return read_utf8_text(path)
    except (OSError, TextEncodingError) as exc:
        raise _fail(
            "SDAI-ANALYSIS-003",
            f"unable to read UTF-8 analysis source '{_portable(root, path)}': {exc}",
        ) from exc


def _line_declaration(line: str) -> tuple[str, str] | None:
    declaration = _DECLARATION.match(line)
    if declaration:
        return _normalize_id(declaration.group("id")), declaration.group("title").strip()
    yaml_declaration = _YAML_ID.match(line)
    if yaml_declaration:
        return _normalize_id(yaml_declaration.group("id")), ""
    return None


def _parse_entities_and_edges(
    root: Path,
    path: Path,
    text: str,
) -> tuple[list[IndexedEntity], list[RelationshipEdge]]:
    source = _portable(root, path)
    entities: list[IndexedEntity] = []
    relationships: list[RelationshipEdge] = []
    current_id: str | None = None
    current_index: int | None = None

    for line_number, line in enumerate(text.splitlines(), start=1):
        declaration = _line_declaration(line)
        if declaration is not None:
            entity_id, title = declaration
            entity = IndexedEntity(
                id=entity_id,
                kind=_entity_kind(entity_id),
                source=source,
                line=line_number,
                title=title,
            )
            entities.append(entity)
            current_id = entity_id
            current_index = len(entities) - 1

        status = _STATUS.match(line)
        if status and current_index is not None:
            current = entities[current_index]
            entities[current_index] = IndexedEntity(
                id=current.id,
                kind=current.kind,
                source=current.source,
                line=current.line,
                title=current.title,
                status=status.group("status").casefold(),
            )

        ids = tuple(_normalize_id(match.group("id")) for match in _ID.finditer(line))
        if not ids or current_id is None:
            continue
        for target in ids:
            if target == current_id:
                continue
            relationships.append(
                RelationshipEdge(
                    from_id=current_id,
                    to_id=target,
                    relation="references",
                    source=source,
                    line=line_number,
                )
            )

    return entities, relationships


def _schema_facts(root: Path, feature_id: str, *, environ: Mapping[str, str]) -> tuple[
    tuple[SchemaArtifactFact, ...], tuple[str, ...], tuple[str, ...]
]:
    graph = load_artifact_schema_graph(root, environ=environ)
    facts: list[SchemaArtifactFact] = []
    for artifact in graph.artifacts:
        rendered = artifact.path.replace("{feature}", feature_id)
        if "{domain}" in rendered:
            resolved_path = None
            exists = None
        else:
            candidate = ensure_within_project(
                root,
                root / Path(rendered),
                label=f"artifact schema '{artifact.id}' resolved path",
            )
            resolved_path = candidate.relative_to(root.resolve()).as_posix()
            exists = candidate.exists()
        facts.append(
            SchemaArtifactFact(
                id=artifact.id,
                path_template=artifact.path,
                resolved_path=resolved_path,
                type=artifact.type,
                required=artifact.required,
                depends_on=artifact.depends_on,
                source_layer=artifact.source_layer.value,
                exists=exists,
            )
        )
    return tuple(facts), graph.sources, graph.topological_order


def build_feature_artifact_index(
    project_root: Path,
    feature_id: str,
    *,
    environ: Mapping[str, str] | None = None,
) -> FeatureArtifactIndex:
    """Build deterministic read-only facts for a feature change.

    The function never writes feature/canonical state and performs no provider/model
    calls. Duplicate entity IDs are intentionally preserved as facts for later
    analysis rules to diagnose.
    """

    root = project_root.resolve()
    feature = validate_feature_id(feature_id)
    feature_dir = _feature_dir(root, feature)
    indexed_files: list[IndexedFile] = []
    entities: list[IndexedEntity] = []
    relationships: list[RelationshipEdge] = []

    for path in _files(root, feature_dir):
        text = _read_source(root, path)
        source = _portable(root, path)
        indexed_files.append(
            IndexedFile(
                source=source,
                sha256=_hash_text(text),
                size_bytes=len(text.encode("utf-8")),
            )
        )
        found_entities, found_relationships = _parse_entities_and_edges(root, path, text)
        entities.extend(found_entities)
        relationships.extend(found_relationships)

    entities.sort(key=lambda item: (item.id, item.source, item.line, item.kind, item.title))
    relationships = sorted(
        set(relationships),
        key=lambda item: (item.from_id, item.to_id, item.relation, item.source, item.line),
    )
    schema_artifacts, schema_sources, schema_order = _schema_facts(
        root,
        feature,
        environ=dict(environ or {}),
    )
    return FeatureArtifactIndex(
        feature_id=feature,
        files=tuple(indexed_files),
        entities=tuple(entities),
        relationships=tuple(relationships),
        schema_artifacts=schema_artifacts,
        schema_sources=schema_sources,
        schema_topological_order=schema_order,
    )
