from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
import json
import os
from pathlib import Path
import tempfile
from typing import Iterable, Mapping

import yaml

from sdai.artifact_schemas import (
    ArtifactDefinition,
    ArtifactSchemaGraph,
    load_artifact_schema_graph,
)
from sdai.path_safety import ensure_within_project
from sdai.spec_changes import validate_change_feature_id, validate_domain_id
from sdai.text import TextEncodingError, read_utf8_text


class ArtifactStateError(RuntimeError):
    """Raised when artifact state/evidence cannot be evaluated safely."""


class ArtifactFreshness(StrEnum):
    FRESH = "fresh"
    STALE = "stale"
    MISSING = "missing"
    BLOCKED = "blocked"


RISK_PROFILES = frozenset({"trivial", "standard", "critical", "regulated"})
_EVIDENCE_KINDS = frozenset({"approval", "validation", "verification", "evidence"})
_STATE_KEYS = frozenset(
    {
        "version",
        "artifact_id",
        "artifact_path",
        "definition_sha256",
        "artifact_sha256",
        "dependency_sha256",
        "evidence",
    }
)
_EVIDENCE_KEYS = frozenset({"kind", "id", "source", "source_sha256"})
_TEXT_TYPES = frozenset(
    {
        "markdown",
        "yaml",
        "json",
        "text",
        "openapi",
        "asyncapi",
        "json-schema",
        "protobuf",
        "drawio",
        "plantuml",
    }
)


@dataclass(frozen=True)
class ArtifactEvidenceInput:
    kind: str
    id: str
    source: str


@dataclass(frozen=True)
class ArtifactEvidenceBinding:
    kind: str
    id: str
    source: str
    source_sha256: str

    def as_dict(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "id": self.id,
            "source": self.source,
            "source_sha256": self.source_sha256,
        }


@dataclass(frozen=True)
class ArtifactEvidenceState:
    kind: str
    id: str
    source: str
    recorded_sha256: str
    current_sha256: str | None
    fresh: bool
    reason: str

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "id": self.id,
            "source": self.source,
            "recorded_sha256": self.recorded_sha256,
            "current_sha256": self.current_sha256,
            "fresh": self.fresh,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class ArtifactStateRecord:
    artifact_id: str
    artifact_path: str
    definition_sha256: str
    artifact_sha256: str
    dependency_sha256: dict[str, str]
    evidence: tuple[ArtifactEvidenceBinding, ...]
    source: str

    def as_dict(self) -> dict[str, object]:
        return {
            "version": 1,
            "artifact_id": self.artifact_id,
            "artifact_path": self.artifact_path,
            "definition_sha256": self.definition_sha256,
            "artifact_sha256": self.artifact_sha256,
            "dependency_sha256": {
                key: self.dependency_sha256[key]
                for key in sorted(self.dependency_sha256)
            },
            "evidence": [item.as_dict() for item in self.evidence],
        }


@dataclass(frozen=True)
class ArtifactState:
    artifact_id: str
    path: str
    required: bool
    freshness: ArtifactFreshness
    current_sha256: str | None
    recorded_sha256: str | None
    definition_sha256: str
    recorded_definition_sha256: str | None
    dependencies: tuple[str, ...]
    reasons: tuple[str, ...]
    evidence: tuple[ArtifactEvidenceState, ...]
    record_source: str | None

    def as_dict(self) -> dict[str, object]:
        return {
            "artifact_id": self.artifact_id,
            "path": self.path,
            "required": self.required,
            "freshness": self.freshness.value,
            "current_sha256": self.current_sha256,
            "recorded_sha256": self.recorded_sha256,
            "definition_sha256": self.definition_sha256,
            "recorded_definition_sha256": self.recorded_definition_sha256,
            "dependencies": list(self.dependencies),
            "reasons": list(self.reasons),
            "evidence": [item.as_dict() for item in self.evidence],
            "record_source": self.record_source,
        }


@dataclass(frozen=True)
class ArtifactStateReport:
    feature_id: str
    risk: str
    domain: str | None
    states: tuple[ArtifactState, ...]
    topological_order: tuple[str, ...]

    def by_id(self) -> dict[str, ArtifactState]:
        return {item.artifact_id: item for item in self.states}

    def as_dict(self) -> dict[str, object]:
        counts = {
            state.value: sum(1 for item in self.states if item.freshness is state)
            for state in ArtifactFreshness
        }
        return {
            "version": 1,
            "feature_id": self.feature_id,
            "risk": self.risk,
            "domain": self.domain,
            "counts": counts,
            "topological_order": list(self.topological_order),
            "artifacts": [item.as_dict() for item in self.states],
        }

    def to_json(self) -> str:
        return json.dumps(
            self.as_dict(),
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
        )


def _fail(code: str, message: str) -> ArtifactStateError:
    return ArtifactStateError(f"{code}: {message}")


def _sha256_bytes(data: bytes) -> str:
    return "sha256:" + sha256(data).hexdigest()


def _sha256_text(text: str) -> str:
    return _sha256_bytes(text.encode("utf-8"))


def _definition_sha256(definition: ArtifactDefinition) -> str:
    payload = {
        "id": definition.id,
        "path": definition.path,
        "type": definition.type,
        "required": definition.required,
        "depends_on": list(definition.depends_on),
        "applies_to": list(definition.applies_to),
        "organization_required": definition.organization_required,
        "organization_dependencies": list(definition.organization_dependencies),
    }
    return _sha256_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    )


def _portable(root: Path, path: Path) -> str:
    safe = ensure_within_project(root, path, label="artifact state path")
    return safe.relative_to(root.resolve()).as_posix()


def _validate_risk(risk: str) -> str:
    normalized = risk.strip().lower()
    if normalized not in RISK_PROFILES:
        raise _fail(
            "SDAI-STATE-001",
            f"risk must be one of: {', '.join(sorted(RISK_PROFILES))}",
        )
    return normalized


def _materialize_path(
    root: Path,
    definition: ArtifactDefinition,
    feature_id: str,
    domain: str | None,
) -> Path:
    feature = validate_change_feature_id(feature_id)
    rendered = definition.path.replace("{feature}", feature)
    if "{domain}" in rendered:
        if domain is None:
            raise _fail(
                "SDAI-STATE-001",
                f"artifact '{definition.id}' requires a domain value for path '{definition.path}'",
            )
        rendered = rendered.replace("{domain}", validate_domain_id(domain))
    candidate = root / Path(*rendered.split("/"))
    return ensure_within_project(root, candidate, label=f"artifact '{definition.id}' path")


def _hash_text_file(path: Path) -> str:
    try:
        return _sha256_text(read_utf8_text(path))
    except (TextEncodingError, OSError) as exc:
        raise _fail("SDAI-STATE-003", f"unable to hash UTF-8 artifact '{path}': {exc}") from exc


def _hash_directory(path: Path) -> str:
    entries: list[bytes] = []
    try:
        for child in sorted(path.rglob("*"), key=lambda item: item.relative_to(path).as_posix()):
            if child.is_symlink():
                raise _fail(
                    "SDAI-STATE-003",
                    f"directory artifact contains unsupported symlink: {child}",
                )
            if not child.is_file():
                continue
            relative = child.relative_to(path).as_posix().encode("utf-8")
            entries.append(relative + b"\0" + child.read_bytes())
    except OSError as exc:
        raise _fail("SDAI-STATE-003", f"unable to hash directory artifact '{path}': {exc}") from exc
    return _sha256_bytes(b"\n".join(entries))


def _artifact_hash(path: Path, definition: ArtifactDefinition) -> str | None:
    if not path.exists():
        return None
    if path.is_symlink():
        raise _fail(
            "SDAI-STATE-003",
            f"artifact '{definition.id}' must not be a symlink: {path}",
        )
    if definition.type == "directory":
        if not path.is_dir():
            raise _fail(
                "SDAI-STATE-003",
                f"artifact '{definition.id}' expects a directory: {path}",
            )
        return _hash_directory(path)
    if definition.type not in _TEXT_TYPES:
        raise _fail(
            "SDAI-STATE-003",
            f"artifact '{definition.id}' has unsupported hash type '{definition.type}'",
        )
    if not path.is_file():
        raise _fail(
            "SDAI-STATE-003",
            f"artifact '{definition.id}' expects a file: {path}",
        )
    return _hash_text_file(path)


def _state_dir(root: Path, feature_id: str) -> Path:
    feature = validate_change_feature_id(feature_id)
    candidate = root / "specs" / "changes" / feature / ".sdai" / "artifact-state"
    return ensure_within_project(root, candidate, label="artifact state directory")


def _record_path(root: Path, feature_id: str, artifact_id: str) -> Path:
    candidate = _state_dir(root, feature_id) / f"{artifact_id}.yaml"
    return ensure_within_project(root, candidate, label=f"artifact state record '{artifact_id}'")


def _validate_hash(value: object, *, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value.startswith("sha256:")
        or len(value) != 71
        or any(char not in "0123456789abcdef" for char in value[7:])
    ):
        raise _fail("SDAI-STATE-002", f"{label} must be a lowercase sha256: hash")
    return value


def _parse_evidence(raw: object, *, source: str) -> tuple[ArtifactEvidenceBinding, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise _fail("SDAI-STATE-002", f"{source} evidence must be a list")
    result: list[ArtifactEvidenceBinding] = []
    seen: set[tuple[str, str]] = set()
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, Mapping):
            raise _fail("SDAI-STATE-002", f"{source} evidence #{index} must be a mapping")
        unknown = sorted(set(item) - _EVIDENCE_KEYS)
        if unknown:
            raise _fail(
                "SDAI-STATE-002",
                f"{source} evidence #{index} has unknown field(s): {', '.join(map(str, unknown))}",
            )
        kind = item.get("kind")
        evidence_id = item.get("id")
        evidence_source = item.get("source")
        if not isinstance(kind, str) or kind not in _EVIDENCE_KINDS:
            raise _fail(
                "SDAI-STATE-002",
                f"{source} evidence #{index} kind must be one of: {', '.join(sorted(_EVIDENCE_KINDS))}",
            )
        if not isinstance(evidence_id, str) or not evidence_id.strip():
            raise _fail("SDAI-STATE-002", f"{source} evidence #{index} id must be non-empty")
        if not isinstance(evidence_source, str) or not evidence_source.strip():
            raise _fail("SDAI-STATE-002", f"{source} evidence #{index} source must be non-empty")
        key = (kind, evidence_id.strip())
        if key in seen:
            raise _fail("SDAI-STATE-002", f"{source} contains duplicate evidence {kind}/{evidence_id}")
        seen.add(key)
        result.append(
            ArtifactEvidenceBinding(
                kind=kind,
                id=evidence_id.strip(),
                source=evidence_source.strip(),
                source_sha256=_validate_hash(
                    item.get("source_sha256"),
                    label=f"{source} evidence #{index} source_sha256",
                ),
            )
        )
    return tuple(result)


def _load_record(root: Path, feature_id: str, artifact_id: str) -> ArtifactStateRecord | None:
    path = _record_path(root, feature_id, artifact_id)
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise _fail("SDAI-STATE-002", f"artifact state record must be a regular file: {_portable(root, path)}")
    try:
        raw = yaml.safe_load(read_utf8_text(path)) or {}
    except (TextEncodingError, OSError, yaml.YAMLError) as exc:
        raise _fail("SDAI-STATE-002", f"unable to read artifact state record {_portable(root, path)}: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise _fail("SDAI-STATE-002", f"artifact state record {_portable(root, path)} must be a mapping")
    unknown = sorted(set(raw) - _STATE_KEYS)
    if unknown:
        raise _fail(
            "SDAI-STATE-002",
            f"artifact state record {_portable(root, path)} has unknown field(s): {', '.join(map(str, unknown))}",
        )
    if raw.get("version") != 1:
        raise _fail("SDAI-STATE-002", f"artifact state record {_portable(root, path)} version must be 1")
    record_artifact_id = raw.get("artifact_id")
    artifact_path = raw.get("artifact_path")
    if record_artifact_id != artifact_id:
        raise _fail(
            "SDAI-STATE-002",
            f"artifact state record {_portable(root, path)} artifact_id must be '{artifact_id}'",
        )
    if not isinstance(artifact_path, str) or not artifact_path.strip():
        raise _fail("SDAI-STATE-002", f"artifact state record {_portable(root, path)} artifact_path must be non-empty")
    dependencies = raw.get("dependency_sha256") or {}
    if not isinstance(dependencies, Mapping) or not all(
        isinstance(key, str) and key.strip() for key in dependencies
    ):
        raise _fail("SDAI-STATE-002", f"artifact state record {_portable(root, path)} dependency_sha256 must be a mapping")
    dependency_hashes = {
        str(key): _validate_hash(value, label=f"dependency_sha256.{key}")
        for key, value in dependencies.items()
    }
    return ArtifactStateRecord(
        artifact_id=artifact_id,
        artifact_path=artifact_path.strip(),
        definition_sha256=_validate_hash(raw.get("definition_sha256"), label="definition_sha256"),
        artifact_sha256=_validate_hash(raw.get("artifact_sha256"), label="artifact_sha256"),
        dependency_sha256=dependency_hashes,
        evidence=_parse_evidence(raw.get("evidence"), source=_portable(root, path)),
        source=_portable(root, path),
    )


def _source_hash(root: Path, source: str) -> str | None:
    candidate = ensure_within_project(root, root / Path(*source.split("/")), label="artifact evidence source")
    if not candidate.exists():
        return None
    if candidate.is_symlink() or not candidate.is_file():
        raise _fail("SDAI-STATE-003", f"evidence source must be a regular file: {source}")
    return _hash_text_file(candidate)


def _evaluate_evidence(
    root: Path,
    record: ArtifactStateRecord,
) -> tuple[ArtifactEvidenceState, ...]:
    result: list[ArtifactEvidenceState] = []
    for binding in record.evidence:
        current = _source_hash(root, binding.source)
        if current is None:
            fresh = False
            reason = "evidence source is missing"
        elif current != binding.source_sha256:
            fresh = False
            reason = "evidence source hash changed"
        else:
            fresh = True
            reason = "evidence source hash matches recorded binding"
        result.append(
            ArtifactEvidenceState(
                kind=binding.kind,
                id=binding.id,
                source=binding.source,
                recorded_sha256=binding.source_sha256,
                current_sha256=current,
                fresh=fresh,
                reason=reason,
            )
        )
    return tuple(result)


def _active_graph(graph: ArtifactSchemaGraph, risk: str) -> tuple[dict[str, ArtifactDefinition], tuple[str, ...]]:
    all_artifacts = graph.by_id()
    active_ids = {item.id for item in graph.artifacts if risk in item.applies_to}
    queue = list(active_ids)
    while queue:
        artifact_id = queue.pop()
        for dependency in all_artifacts[artifact_id].depends_on:
            if dependency not in active_ids:
                active_ids.add(dependency)
                queue.append(dependency)
    active = {artifact_id: all_artifacts[artifact_id] for artifact_id in active_ids}
    order = tuple(artifact_id for artifact_id in graph.topological_order if artifact_id in active)
    return active, order


def evaluate_artifact_states(
    project_root: Path,
    feature_id: str,
    *,
    risk: str = "standard",
    domain: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> ArtifactStateReport:
    root = project_root.resolve()
    feature = validate_change_feature_id(feature_id)
    risk_profile = _validate_risk(risk)
    domain_id = validate_domain_id(domain) if domain is not None else None
    graph = load_artifact_schema_graph(root, environ=environ)
    active, order = _active_graph(graph, risk_profile)

    current_hashes: dict[str, str | None] = {}
    paths: dict[str, str] = {}
    definitions: dict[str, str] = {}
    records: dict[str, ArtifactStateRecord | None] = {}
    for artifact_id in order:
        definition = active[artifact_id]
        path = _materialize_path(root, definition, feature, domain_id)
        paths[artifact_id] = _portable(root, path)
        definitions[artifact_id] = _definition_sha256(definition)
        current_hashes[artifact_id] = _artifact_hash(path, definition)
        records[artifact_id] = _load_record(root, feature, artifact_id)

    states_by_id: dict[str, ArtifactState] = {}
    states: list[ArtifactState] = []
    for artifact_id in order:
        definition = active[artifact_id]
        current_hash = current_hashes[artifact_id]
        record = records[artifact_id]
        reasons: list[str] = []
        evidence = () if record is None else _evaluate_evidence(root, record)

        if current_hash is None:
            freshness = ArtifactFreshness.MISSING
            reasons.append("artifact content is missing")
        else:
            blocked_dependencies = [
                dependency
                for dependency in definition.depends_on
                if states_by_id[dependency].freshness
                in {ArtifactFreshness.MISSING, ArtifactFreshness.BLOCKED}
            ]
            stale_dependencies = [
                dependency
                for dependency in definition.depends_on
                if states_by_id[dependency].freshness is ArtifactFreshness.STALE
            ]
            if blocked_dependencies:
                freshness = ArtifactFreshness.BLOCKED
                reasons.append(
                    "dependency content is missing/blocked: "
                    + ", ".join(blocked_dependencies)
                )
            elif record is None:
                freshness = ArtifactFreshness.STALE
                reasons.append("no hash-bound artifact state record exists")
            else:
                if record.artifact_path != paths[artifact_id]:
                    reasons.append("artifact path changed since state was recorded")
                if record.definition_sha256 != definitions[artifact_id]:
                    reasons.append("effective artifact definition changed since state was recorded")
                if record.artifact_sha256 != current_hash:
                    reasons.append("artifact content hash changed since state was recorded")
                expected_dependency_ids = tuple(sorted(definition.depends_on))
                recorded_dependency_ids = tuple(sorted(record.dependency_sha256))
                if recorded_dependency_ids != expected_dependency_ids:
                    reasons.append("dependency set changed since state was recorded")
                for dependency in definition.depends_on:
                    current_dependency_hash = current_hashes[dependency]
                    if (
                        current_dependency_hash is not None
                        and record.dependency_sha256.get(dependency) != current_dependency_hash
                    ):
                        reasons.append(
                            f"dependency '{dependency}' content hash changed since state was recorded"
                        )
                if stale_dependencies:
                    reasons.append(
                        "dependency state is stale: " + ", ".join(stale_dependencies)
                    )
                stale_evidence = [f"{item.kind}/{item.id}" for item in evidence if not item.fresh]
                if stale_evidence:
                    reasons.append(
                        "bound evidence is stale: " + ", ".join(stale_evidence)
                    )
                freshness = ArtifactFreshness.STALE if reasons else ArtifactFreshness.FRESH

        state = ArtifactState(
            artifact_id=artifact_id,
            path=paths[artifact_id],
            required=definition.required or definition.organization_required,
            freshness=freshness,
            current_sha256=current_hash,
            recorded_sha256=None if record is None else record.artifact_sha256,
            definition_sha256=definitions[artifact_id],
            recorded_definition_sha256=None if record is None else record.definition_sha256,
            dependencies=definition.depends_on,
            reasons=tuple(reasons),
            evidence=evidence,
            record_source=None if record is None else record.source,
        )
        states_by_id[artifact_id] = state
        states.append(state)

    return ArtifactStateReport(
        feature_id=feature,
        risk=risk_profile,
        domain=domain_id,
        states=tuple(states),
        topological_order=order,
    )


def _normalize_evidence_input(value: ArtifactEvidenceInput) -> ArtifactEvidenceInput:
    kind = value.kind.strip().lower()
    evidence_id = value.id.strip()
    source = value.source.strip().replace("\\", "/")
    if kind not in _EVIDENCE_KINDS:
        raise _fail(
            "SDAI-STATE-004",
            f"evidence kind must be one of: {', '.join(sorted(_EVIDENCE_KINDS))}",
        )
    if not evidence_id:
        raise _fail("SDAI-STATE-004", "evidence id must be non-empty")
    if not source or source.startswith("/") or ".." in Path(*source.split("/")).parts:
        raise _fail("SDAI-STATE-004", "evidence source must be a repository-relative path")
    return ArtifactEvidenceInput(kind, evidence_id, source)


def _bind_evidence(
    root: Path,
    values: Iterable[ArtifactEvidenceInput],
) -> tuple[ArtifactEvidenceBinding, ...]:
    result: list[ArtifactEvidenceBinding] = []
    seen: set[tuple[str, str]] = set()
    for raw in values:
        value = _normalize_evidence_input(raw)
        key = (value.kind, value.id)
        if key in seen:
            raise _fail("SDAI-STATE-004", f"duplicate evidence binding {value.kind}/{value.id}")
        seen.add(key)
        source_hash = _source_hash(root, value.source)
        if source_hash is None:
            raise _fail("SDAI-STATE-004", f"evidence source does not exist: {value.source}")
        result.append(
            ArtifactEvidenceBinding(
                kind=value.kind,
                id=value.id,
                source=value.source,
                source_sha256=source_hash,
            )
        )
    return tuple(sorted(result, key=lambda item: (item.kind, item.id, item.source)))


def _atomic_write_yaml(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="\n",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    )
    temp = Path(handle.name)
    try:
        with handle:
            handle.write(
                yaml.safe_dump(
                    dict(payload),
                    sort_keys=False,
                    allow_unicode=True,
                )
            )
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    except Exception:
        temp.unlink(missing_ok=True)
        raise


def record_artifact_state(
    project_root: Path,
    feature_id: str,
    artifact_id: str,
    *,
    risk: str = "standard",
    domain: str | None = None,
    evidence: Iterable[ArtifactEvidenceInput] = (),
    environ: Mapping[str, str] | None = None,
) -> ArtifactStateRecord:
    """Record a freshness baseline after deterministic validation/approval succeeds.

    This function does not decide whether an artifact is approved or valid. Callers
    such as validators/approval engines must make that decision first, then bind
    their evidence sources here. Dependency artifacts must already be fresh.
    """

    root = project_root.resolve()
    feature = validate_change_feature_id(feature_id)
    risk_profile = _validate_risk(risk)
    domain_id = validate_domain_id(domain) if domain is not None else None
    graph = load_artifact_schema_graph(root, environ=environ)
    active, order = _active_graph(graph, risk_profile)
    if artifact_id not in active:
        raise _fail(
            "SDAI-STATE-004",
            f"artifact '{artifact_id}' is not active for risk profile '{risk_profile}'",
        )
    definition = active[artifact_id]
    path = _materialize_path(root, definition, feature, domain_id)
    current_hash = _artifact_hash(path, definition)
    if current_hash is None:
        raise _fail("SDAI-STATE-004", f"cannot record missing artifact '{artifact_id}'")

    existing_report = evaluate_artifact_states(
        root,
        feature,
        risk=risk_profile,
        domain=domain_id,
        environ=environ,
    )
    existing = existing_report.by_id()
    not_fresh = [
        dependency
        for dependency in definition.depends_on
        if existing[dependency].freshness is not ArtifactFreshness.FRESH
    ]
    if not_fresh:
        raise _fail(
            "SDAI-STATE-004",
            f"cannot record artifact '{artifact_id}' until dependencies are fresh: {', '.join(not_fresh)}",
        )

    dependency_hashes = {
        dependency: existing[dependency].current_sha256
        for dependency in definition.depends_on
    }
    if any(value is None for value in dependency_hashes.values()):
        raise _fail(
            "SDAI-STATE-004",
            f"cannot record artifact '{artifact_id}' with missing dependency content",
        )
    evidence_bindings = _bind_evidence(root, evidence)
    record = ArtifactStateRecord(
        artifact_id=artifact_id,
        artifact_path=_portable(root, path),
        definition_sha256=_definition_sha256(definition),
        artifact_sha256=current_hash,
        dependency_sha256={key: str(value) for key, value in dependency_hashes.items()},
        evidence=evidence_bindings,
        source=_portable(root, _record_path(root, feature, artifact_id)),
    )
    _atomic_write_yaml(
        _record_path(root, feature, artifact_id),
        record.as_dict(),
    )
    return record
