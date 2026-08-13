from __future__ import annotations

from collections import defaultdict
import re
from pathlib import Path
from typing import Iterable, Mapping

from sdai.artifact_state import ArtifactFreshness, ArtifactStateError, evaluate_artifact_states
from sdai.cross_artifact import (
    AnalysisFinding,
    AnalysisReport,
    FeatureArtifactIndex,
    IndexedEntity,
    RelationshipEdge,
    SourceEvidence,
    build_feature_artifact_index,
)


class CrossArtifactAnalysisError(RuntimeError):
    """Raised when deterministic analysis cannot safely evaluate repository state."""


_ACCEPTED_ADR = frozenset({"accepted", "resolved", "superseded"})
_APPROVED = frozenset({"approved", "accepted", "granted", "satisfied"})
_RESOLVED_THREAT = frozenset({"mitigated", "resolved", "closed", "accepted"})
_COMPLETED_MITIGATION = frozenset(
    {"implemented", "mitigated", "resolved", "accepted", "complete", "completed", "closed"}
)
_BREAKING_STATUS = frozenset({"breaking", "breaking-change", "breaking_change", "breakingchange"})
_BREAKING_TITLE = re.compile(r"(?:^breaking(?:\s+change)?\b|\[breaking\])", re.IGNORECASE)

_SEVERITY = {
    "ORPHAN_REQUIREMENT": "warning",
    "ORPHAN_TASK": "warning",
    "MISSING_NFR": "warning",
    "ARCHITECTURE_CONFLICT": "blocking",
    "CONTRACT_CONFLICT": "blocking",
    "UNRESOLVED_ADR": "warning",
    "UNTESTED_SCENARIO": "warning",
    "UNAPPROVED_BREAKING_CHANGE": "blocking",
    "UNMITIGATED_THREAT": "blocking",
    "STALE_ARTIFACT": "blocking",
}


def _fail(code: str, message: str) -> CrossArtifactAnalysisError:
    return CrossArtifactAnalysisError(f"{code}: {message}")


def _evidence(entity: IndexedEntity, detail: str | None = None) -> SourceEvidence:
    return SourceEvidence(
        source=entity.source,
        line=entity.line,
        entity_id=entity.id,
        detail=detail,
    )


def _entities_by_kind(index: FeatureArtifactIndex) -> dict[str, tuple[IndexedEntity, ...]]:
    grouped: dict[str, list[IndexedEntity]] = defaultdict(list)
    for entity in index.entities:
        grouped[entity.kind].append(entity)
    return {
        kind: tuple(sorted(values, key=lambda item: (item.id, item.source, item.line)))
        for kind, values in grouped.items()
    }


def _edges_by_id(index: FeatureArtifactIndex) -> dict[str, tuple[RelationshipEdge, ...]]:
    grouped: dict[str, list[RelationshipEdge]] = defaultdict(list)
    for edge in index.relationships:
        grouped[edge.from_id].append(edge)
        grouped[edge.to_id].append(edge)
    return {
        entity_id: tuple(
            sorted(
                values,
                key=lambda item: (
                    item.from_id,
                    item.to_id,
                    item.source,
                    item.line,
                ),
            )
        )
        for entity_id, values in grouped.items()
    }


def _connected(
    entity_id: str,
    target_ids: set[str],
    edge_map: Mapping[str, tuple[RelationshipEdge, ...]],
) -> bool:
    for edge in edge_map.get(entity_id, ()):
        other = edge.to_id if edge.from_id == entity_id else edge.from_id
        if other in target_ids:
            return True
    return False


def _relationship_evidence(
    entity_id: str,
    target_ids: set[str],
    edge_map: Mapping[str, tuple[RelationshipEdge, ...]],
) -> tuple[SourceEvidence, ...]:
    evidence: list[SourceEvidence] = []
    for edge in edge_map.get(entity_id, ()):
        other = edge.to_id if edge.from_id == entity_id else edge.from_id
        if other in target_ids:
            evidence.append(
                SourceEvidence(
                    source=edge.source,
                    line=edge.line,
                    entity_id=entity_id,
                    detail=f"explicit relationship {edge.from_id} -> {edge.to_id}",
                )
            )
    return tuple(evidence)


def _conflicting_duplicates(
    entities: Iterable[IndexedEntity],
    *,
    code: str,
    label: str,
) -> list[AnalysisFinding]:
    grouped: dict[str, list[IndexedEntity]] = defaultdict(list)
    for entity in entities:
        grouped[entity.id].append(entity)
    findings: list[AnalysisFinding] = []
    for entity_id in sorted(grouped):
        declarations = grouped[entity_id]
        signatures = {
            (item.title.strip().casefold(), (item.status or "").strip().casefold())
            for item in declarations
        }
        if len(declarations) < 2 or len(signatures) < 2:
            continue
        findings.append(
            AnalysisFinding(
                code=code,
                severity=_SEVERITY[code],
                message=(
                    f"{label} '{entity_id}' has conflicting declarations with different "
                    "title/status evidence."
                ),
                entity_id=entity_id,
                evidence=tuple(
                    _evidence(
                        item,
                        f"title={item.title!r}; status={item.status or '-'}",
                    )
                    for item in sorted(declarations, key=lambda value: (value.source, value.line))
                ),
            )
        )
    return findings


def _orphan_findings(
    grouped: Mapping[str, tuple[IndexedEntity, ...]],
    edge_map: Mapping[str, tuple[RelationshipEdge, ...]],
) -> list[AnalysisFinding]:
    findings: list[AnalysisFinding] = []
    requirements = grouped.get("requirement", ())
    tasks = grouped.get("task", ())
    requirement_ids = {item.id for item in requirements}
    task_ids = {item.id for item in tasks}

    for requirement in requirements:
        if _connected(requirement.id, task_ids, edge_map):
            continue
        findings.append(
            AnalysisFinding(
                code="ORPHAN_REQUIREMENT",
                severity=_SEVERITY["ORPHAN_REQUIREMENT"],
                message=f"Requirement '{requirement.id}' has no explicit relationship to a declared task.",
                entity_id=requirement.id,
                evidence=(_evidence(requirement, "requirement declaration"),),
            )
        )
    for task in tasks:
        if _connected(task.id, requirement_ids, edge_map):
            continue
        findings.append(
            AnalysisFinding(
                code="ORPHAN_TASK",
                severity=_SEVERITY["ORPHAN_TASK"],
                message=f"Task '{task.id}' has no explicit relationship to a declared requirement.",
                entity_id=task.id,
                evidence=(_evidence(task, "task declaration"),),
            )
        )
    return findings


def _missing_nfr(grouped: Mapping[str, tuple[IndexedEntity, ...]]) -> list[AnalysisFinding]:
    requirements = grouped.get("requirement", ())
    if not requirements or any(item.id.startswith("NFR-") for item in requirements):
        return []
    return [
        AnalysisFinding(
            code="MISSING_NFR",
            severity=_SEVERITY["MISSING_NFR"],
            message="Feature declares requirements but no explicit NFR-* requirement.",
            evidence=tuple(_evidence(item, "non-NFR requirement declaration") for item in requirements),
        )
    ]


def _unresolved_adrs(grouped: Mapping[str, tuple[IndexedEntity, ...]]) -> list[AnalysisFinding]:
    findings: list[AnalysisFinding] = []
    for adr in grouped.get("adr", ()):
        status = (adr.status or "").casefold()
        if status in _ACCEPTED_ADR:
            continue
        findings.append(
            AnalysisFinding(
                code="UNRESOLVED_ADR",
                severity=_SEVERITY["UNRESOLVED_ADR"],
                message=f"ADR '{adr.id}' is unresolved (status={adr.status or 'missing'}).",
                entity_id=adr.id,
                evidence=(_evidence(adr, f"status={adr.status or 'missing'}"),),
            )
        )
    return findings


def _untested_scenarios(
    grouped: Mapping[str, tuple[IndexedEntity, ...]],
    edge_map: Mapping[str, tuple[RelationshipEdge, ...]],
) -> list[AnalysisFinding]:
    test_ids = {item.id for item in grouped.get("test", ())}
    findings: list[AnalysisFinding] = []
    for scenario in grouped.get("scenario", ()):
        if _connected(scenario.id, test_ids, edge_map):
            continue
        findings.append(
            AnalysisFinding(
                code="UNTESTED_SCENARIO",
                severity=_SEVERITY["UNTESTED_SCENARIO"],
                message=f"Scenario '{scenario.id}' has no explicit relationship to a declared test.",
                entity_id=scenario.id,
                evidence=(_evidence(scenario, "scenario declaration"),),
            )
        )
    return findings


def _is_breaking(contract: IndexedEntity) -> bool:
    status = (contract.status or "").casefold()
    return status in _BREAKING_STATUS or bool(_BREAKING_TITLE.search(contract.title.strip()))


def _unapproved_breaking_changes(
    grouped: Mapping[str, tuple[IndexedEntity, ...]],
    edge_map: Mapping[str, tuple[RelationshipEdge, ...]],
) -> list[AnalysisFinding]:
    approvals = grouped.get("approval", ())
    approved_ids = {item.id for item in approvals if (item.status or "").casefold() in _APPROVED}
    findings: list[AnalysisFinding] = []
    for contract in grouped.get("contract", ()):
        if not _is_breaking(contract):
            continue
        related = _relationship_evidence(contract.id, approved_ids, edge_map)
        if related:
            continue
        evidence: list[SourceEvidence] = [
            _evidence(contract, f"breaking contract status/title; status={contract.status or '-'}")
        ]
        for approval in approvals:
            if _connected(contract.id, {approval.id}, edge_map):
                evidence.append(
                    _evidence(
                        approval,
                        f"related approval is not approved (status={approval.status or 'missing'})",
                    )
                )
        findings.append(
            AnalysisFinding(
                code="UNAPPROVED_BREAKING_CHANGE",
                severity=_SEVERITY["UNAPPROVED_BREAKING_CHANGE"],
                message=f"Breaking contract '{contract.id}' has no explicitly related approved approval record.",
                entity_id=contract.id,
                evidence=tuple(evidence),
            )
        )
    return findings


def _unmitigated_threats(
    grouped: Mapping[str, tuple[IndexedEntity, ...]],
    edge_map: Mapping[str, tuple[RelationshipEdge, ...]],
) -> list[AnalysisFinding]:
    mitigations = grouped.get("mitigation", ())
    completed_ids = {
        item.id
        for item in mitigations
        if (item.status or "").casefold() in _COMPLETED_MITIGATION
    }
    findings: list[AnalysisFinding] = []
    for threat in grouped.get("threat", ()):
        if (threat.status or "").casefold() in _RESOLVED_THREAT:
            continue
        if _connected(threat.id, completed_ids, edge_map):
            continue
        evidence: list[SourceEvidence] = [
            _evidence(threat, f"threat status={threat.status or 'missing'}")
        ]
        for mitigation in mitigations:
            if _connected(threat.id, {mitigation.id}, edge_map):
                evidence.append(
                    _evidence(
                        mitigation,
                        f"related mitigation is incomplete (status={mitigation.status or 'missing'})",
                    )
                )
        findings.append(
            AnalysisFinding(
                code="UNMITIGATED_THREAT",
                severity=_SEVERITY["UNMITIGATED_THREAT"],
                message=f"Threat '{threat.id}' is unresolved and has no explicitly related completed mitigation.",
                entity_id=threat.id,
                evidence=tuple(evidence),
            )
        )
    return findings


def _stale_artifacts(
    project_root: Path,
    feature_id: str,
    *,
    risk: str,
    environ: Mapping[str, str],
) -> list[AnalysisFinding]:
    try:
        states = evaluate_artifact_states(
            project_root,
            feature_id,
            risk=risk,
            environ=environ,
        )
    except ArtifactStateError as exc:
        raise _fail("SDAI-ANALYSIS-STATE-001", f"unable to evaluate artifact freshness: {exc}") from exc
    findings: list[AnalysisFinding] = []
    for state in states.states:
        if state.freshness is not ArtifactFreshness.STALE or state.record_source is None:
            continue
        findings.append(
            AnalysisFinding(
                code="STALE_ARTIFACT",
                severity=_SEVERITY["STALE_ARTIFACT"],
                message=f"Artifact '{state.artifact_id}' has stale hash-bound state evidence.",
                entity_id=state.artifact_id,
                evidence=(
                    SourceEvidence(
                        source=state.path,
                        line=1,
                        entity_id=state.artifact_id,
                        detail="; ".join(state.reasons) or "artifact state is stale",
                    ),
                    SourceEvidence(
                        source=state.record_source,
                        line=1,
                        entity_id=state.artifact_id,
                        detail="previous hash-bound artifact state record",
                    ),
                ),
            )
        )
    return findings


def _dedupe_findings(findings: Iterable[AnalysisFinding]) -> tuple[AnalysisFinding, ...]:
    grouped: dict[tuple[str, str | None], list[AnalysisFinding]] = defaultdict(list)
    for finding in findings:
        grouped[(finding.code, finding.entity_id)].append(finding)

    result: list[AnalysisFinding] = []
    for key in sorted(grouped, key=lambda item: (item[0], item[1] or "")):
        values = grouped[key]
        first = sorted(
            values,
            key=lambda item: (
                item.message,
                tuple((ev.source, ev.line, ev.detail or "") for ev in item.evidence),
            ),
        )[0]
        evidence_by_key: dict[tuple[str, int, str | None, str | None], SourceEvidence] = {}
        for finding in values:
            for evidence in finding.evidence:
                evidence_by_key[
                    (evidence.source, evidence.line, evidence.entity_id, evidence.detail)
                ] = evidence
        evidence = tuple(
            evidence_by_key[item]
            for item in sorted(
                evidence_by_key,
                key=lambda value: (
                    value[0],
                    value[1],
                    value[2] or "",
                    value[3] or "",
                ),
            )
        )
        result.append(
            AnalysisFinding(
                code=first.code,
                severity=first.severity,
                message=first.message,
                entity_id=first.entity_id,
                evidence=evidence,
            )
        )
    return tuple(result)


def analyze_feature(
    project_root: Path,
    feature_id: str,
    *,
    risk: str = "standard",
    environ: Mapping[str, str] | None = None,
) -> AnalysisReport:
    """Run deterministic, read-only cross-artifact rules over current repository evidence."""

    env = dict(environ or {})
    index = build_feature_artifact_index(project_root, feature_id, environ=env)
    grouped = _entities_by_kind(index)
    edge_map = _edges_by_id(index)

    findings: list[AnalysisFinding] = []
    findings.extend(_orphan_findings(grouped, edge_map))
    findings.extend(_missing_nfr(grouped))
    findings.extend(
        _conflicting_duplicates(
            grouped.get("adr", ()),
            code="ARCHITECTURE_CONFLICT",
            label="ADR",
        )
    )
    findings.extend(
        _conflicting_duplicates(
            grouped.get("contract", ()),
            code="CONTRACT_CONFLICT",
            label="Contract",
        )
    )
    findings.extend(_unresolved_adrs(grouped))
    findings.extend(_untested_scenarios(grouped, edge_map))
    findings.extend(_unapproved_breaking_changes(grouped, edge_map))
    findings.extend(_unmitigated_threats(grouped, edge_map))
    findings.extend(
        _stale_artifacts(
            project_root,
            index.feature_id,
            risk=risk,
            environ=env,
        )
    )
    return AnalysisReport(
        feature_id=index.feature_id,
        index_sha256=index.sha256,
        findings=_dedupe_findings(findings),
    )
