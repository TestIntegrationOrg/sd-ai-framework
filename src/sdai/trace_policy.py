from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json
import math
import os
from pathlib import Path
import re
from typing import Mapping

import yaml

from sdai.path_safety import PathSafetyError, ensure_within_project
from sdai.text import TextEncodingError, read_utf8_text
from sdai.trace_builder import TraceBuildResult, build_feature_trace_graph
from sdai.trace_freshness import (
    EvidenceFreshnessReport,
    ProofFreshness,
    evaluate_trace_coverage,
    evaluate_trace_evidence_file,
)
from sdai.trace_graph import TraceGraph, TraceNode, TraceNodeType


TRACE_POLICY_API_VERSION = "sdai.trace-policy/v1"
TRACE_POLICY_REPORT_API_VERSION = "sdai.trace-policy-report/v1"
_RISKS = ("trivial", "standard", "critical", "regulated")
_POLICY_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")


class TracePolicyError(RuntimeError):
    """Raised when trace policy cannot be resolved or evaluated safely."""


class TracePolicyLayer(str, Enum):
    BUILTIN = "builtin"
    ORG = "org"
    REPO = "repo"
    USER = "user"

    @property
    def priority(self) -> int:
        return {
            TracePolicyLayer.BUILTIN: 0,
            TracePolicyLayer.ORG: 10,
            TracePolicyLayer.REPO: 20,
            TracePolicyLayer.USER: 30,
        }[self]


class CoverageDimension(str, Enum):
    REQUIREMENTS = "requirements"
    TASKS = "tasks"
    CODE = "code"
    TESTS = "tests"
    SECURITY = "security"
    APPROVALS = "approvals"


_DEFAULTS: Mapping[str, Mapping[CoverageDimension, float]] = {
    "trivial": {
        CoverageDimension.REQUIREMENTS: 0.0,
        CoverageDimension.TASKS: 0.0,
        CoverageDimension.CODE: 0.0,
        CoverageDimension.TESTS: 0.0,
        CoverageDimension.SECURITY: 0.0,
        CoverageDimension.APPROVALS: 0.0,
    },
    "standard": {
        CoverageDimension.REQUIREMENTS: 80.0,
        CoverageDimension.TASKS: 80.0,
        CoverageDimension.CODE: 80.0,
        CoverageDimension.TESTS: 80.0,
        CoverageDimension.SECURITY: 0.0,
        CoverageDimension.APPROVALS: 0.0,
    },
    "critical": {
        dimension: 100.0 for dimension in CoverageDimension
    },
    "regulated": {
        dimension: 100.0 for dimension in CoverageDimension
    },
}

_PROOF_ORDER = {
    ProofFreshness.VALID: 0,
    ProofFreshness.STALE: 1,
    ProofFreshness.BLOCKED: 2,
    ProofFreshness.MISSING: 3,
}


@dataclass(frozen=True)
class TracePolicyContribution:
    layer: TracePolicyLayer
    source: str
    policy_id: str
    value: float

    def as_dict(self) -> dict[str, object]:
        return {
            "layer": self.layer.value,
            "source": self.source,
            "policy_id": self.policy_id,
            "value": self.value,
        }


@dataclass(frozen=True)
class EffectiveThreshold:
    dimension: CoverageDimension
    required_percent: float
    contributions: tuple[TracePolicyContribution, ...]

    def as_dict(self) -> dict[str, object]:
        enforced_by = [
            item.as_dict()
            for item in self.contributions
            if item.value == self.required_percent
        ]
        return {
            "dimension": self.dimension.value,
            "required_percent": self.required_percent,
            "contributions": [item.as_dict() for item in self.contributions],
            "enforced_by": enforced_by,
        }


@dataclass(frozen=True)
class CoverageDimensionResult:
    dimension: CoverageDimension
    numerator: int
    denominator: int
    actual_percent: float
    threshold: EffectiveThreshold
    compliant: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "dimension": self.dimension.value,
            "numerator": self.numerator,
            "denominator": self.denominator,
            "actual_percent": self.actual_percent,
            "required_percent": self.threshold.required_percent,
            "compliant": self.compliant,
            "threshold": self.threshold.as_dict(),
        }


@dataclass(frozen=True)
class TracePolicyFinding:
    code: str
    severity: str
    dimension: CoverageDimension
    actual_percent: float
    required_percent: float
    message: str

    def as_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "severity": self.severity,
            "dimension": self.dimension.value,
            "actual_percent": self.actual_percent,
            "required_percent": self.required_percent,
            "message": self.message,
        }


@dataclass(frozen=True)
class RequirementPolicyState:
    requirement_id: str
    node_id: str
    current_proof: bool
    task_link: bool
    code_link: bool
    test_link: bool
    security_evidence: bool
    approval_evidence: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "requirement_id": self.requirement_id,
            "node_id": self.node_id,
            "requirements": self.current_proof,
            "tasks": self.task_link,
            "code": self.code_link,
            "tests": self.test_link,
            "security": self.security_evidence,
            "approvals": self.approval_evidence,
        }


@dataclass(frozen=True)
class TracePolicyReport:
    feature_id: str
    risk: str
    graph_sha256: str
    dimensions: tuple[CoverageDimensionResult, ...]
    requirements: tuple[RequirementPolicyState, ...]
    findings: tuple[TracePolicyFinding, ...]
    sources: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.findings

    def as_dict(self) -> dict[str, object]:
        return {
            "apiVersion": TRACE_POLICY_REPORT_API_VERSION,
            "feature_id": self.feature_id,
            "risk": self.risk,
            "graph_sha256": self.graph_sha256,
            "passed": self.passed,
            "sources": list(self.sources),
            "dimensions": [item.as_dict() for item in self.dimensions],
            "requirements": [item.as_dict() for item in self.requirements],
            "findings": [item.as_dict() for item in self.findings],
        }

    def to_json(self) -> str:
        return json.dumps(
            self.as_dict(),
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )


@dataclass(frozen=True)
class _PolicyDocument:
    layer: TracePolicyLayer
    source: str
    policy_id: str
    risks: Mapping[str, Mapping[CoverageDimension, float]]


def _fail(code: str, message: str) -> TracePolicyError:
    return TracePolicyError(f"{code}: {message}")


def _validate_risk(value: str) -> str:
    normalized = value.strip().lower() if isinstance(value, str) else ""
    if normalized not in _RISKS:
        raise _fail(
            "SDAI-TRACE-POLICY-001",
            f"risk must be one of: {', '.join(_RISKS)}",
        )
    return normalized


def _threshold(value: object, *, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _fail("SDAI-TRACE-POLICY-002", f"{label} must be a number from 0 to 100")
    numeric = float(value)
    if not math.isfinite(numeric) or numeric < 0.0 or numeric > 100.0:
        raise _fail("SDAI-TRACE-POLICY-002", f"{label} must be a finite number from 0 to 100")
    return round(numeric, 2)


def _external_paths(value: str | None, *, label: str) -> tuple[Path, ...]:
    if not value:
        return ()
    path = Path(value)
    if not path.is_absolute():
        raise _fail("SDAI-TRACE-POLICY-003", f"{label} must be an absolute file or directory path")
    if path.is_symlink():
        raise _fail("SDAI-TRACE-POLICY-003", f"{label} must not be a symlink")
    if path.is_file():
        return (path,)
    if path.is_dir():
        candidates = tuple(
            sorted(
                [*path.glob("*.yaml"), *path.glob("*.yml")],
                key=lambda item: (item.name.casefold(), item.name),
            )
        )
        for candidate in candidates:
            if candidate.is_symlink() or not candidate.is_file():
                raise _fail(
                    "SDAI-TRACE-POLICY-003",
                    f"{label} policy file must be a regular non-symlink file: {candidate}",
                )
        return candidates
    raise _fail("SDAI-TRACE-POLICY-003", f"{label} does not exist: {path}")


def _repo_policy(root: Path) -> tuple[Path, str] | None:
    try:
        path = ensure_within_project(root, root / ".sdai" / "trace-policy.yaml", label="trace policy")
    except PathSafetyError as exc:
        raise _fail("SDAI-TRACE-POLICY-003", "repository trace policy escapes project root") from exc
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise _fail(
            "SDAI-TRACE-POLICY-003",
            ".sdai/trace-policy.yaml must be a regular non-symlink file",
        )
    return path, path.relative_to(root).as_posix()


def _parse_policy(path: Path, *, layer: TracePolicyLayer, source: str) -> _PolicyDocument:
    try:
        raw = yaml.safe_load(read_utf8_text(path))
    except (OSError, TextEncodingError, yaml.YAMLError) as exc:
        raise _fail("SDAI-TRACE-POLICY-002", f"unable to parse trace policy {source}: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise _fail("SDAI-TRACE-POLICY-002", f"trace policy {source} must be a mapping")
    required_top = {"apiVersion", "kind", "metadata", "spec"}
    if set(raw) != required_top:
        raise _fail(
            "SDAI-TRACE-POLICY-002",
            f"trace policy {source} fields must be exactly: {', '.join(sorted(required_top))}",
        )
    if raw.get("apiVersion") != TRACE_POLICY_API_VERSION or raw.get("kind") != "TraceCoveragePolicy":
        raise _fail(
            "SDAI-TRACE-POLICY-002",
            f"trace policy {source} must use {TRACE_POLICY_API_VERSION} / TraceCoveragePolicy",
        )
    metadata = raw.get("metadata")
    if not isinstance(metadata, Mapping) or set(metadata) != {"id"}:
        raise _fail("SDAI-TRACE-POLICY-002", f"trace policy {source} metadata requires only id")
    policy_id = metadata.get("id")
    if not isinstance(policy_id, str) or not _POLICY_ID.fullmatch(policy_id):
        raise _fail("SDAI-TRACE-POLICY-002", f"trace policy {source} has invalid metadata.id")
    spec = raw.get("spec")
    if not isinstance(spec, Mapping) or set(spec) != {"risks"}:
        raise _fail("SDAI-TRACE-POLICY-002", f"trace policy {source} spec requires only risks")
    raw_risks = spec.get("risks")
    if not isinstance(raw_risks, Mapping):
        raise _fail("SDAI-TRACE-POLICY-002", f"trace policy {source} spec.risks must be a mapping")
    unknown_risks = sorted(str(key) for key in raw_risks if key not in _RISKS)
    if unknown_risks:
        raise _fail(
            "SDAI-TRACE-POLICY-002",
            f"trace policy {source} has unsupported risk(s): {', '.join(unknown_risks)}",
        )
    parsed: dict[str, Mapping[CoverageDimension, float]] = {}
    allowed_dimensions = {item.value for item in CoverageDimension}
    for risk, raw_dimensions in raw_risks.items():
        if not isinstance(risk, str) or not isinstance(raw_dimensions, Mapping):
            raise _fail("SDAI-TRACE-POLICY-002", f"trace policy {source} risk entries must be mappings")
        unknown_dimensions = sorted(str(key) for key in raw_dimensions if key not in allowed_dimensions)
        if unknown_dimensions:
            raise _fail(
                "SDAI-TRACE-POLICY-002",
                f"trace policy {source} risk {risk} has unsupported dimension(s): {', '.join(unknown_dimensions)}",
            )
        dimensions: dict[CoverageDimension, float] = {}
        for key, value in raw_dimensions.items():
            dimension = CoverageDimension(str(key))
            dimensions[dimension] = _threshold(value, label=f"{source} {risk}.{dimension.value}")
        parsed[risk] = dimensions
    return _PolicyDocument(layer=layer, source=source, policy_id=policy_id, risks=parsed)


def _policy_documents(root: Path, *, environ: Mapping[str, str]) -> tuple[_PolicyDocument, ...]:
    documents: list[_PolicyDocument] = []
    for path in _external_paths(
        environ.get("SDAI_ORG_TRACE_POLICY_PATH"),
        label="SDAI_ORG_TRACE_POLICY_PATH",
    ):
        documents.append(
            _parse_policy(path, layer=TracePolicyLayer.ORG, source=path.resolve().as_posix())
        )
    repo = _repo_policy(root)
    if repo is not None:
        path, source = repo
        documents.append(_parse_policy(path, layer=TracePolicyLayer.REPO, source=source))
    for path in _external_paths(
        environ.get("SDAI_USER_TRACE_POLICY_PATH"),
        label="SDAI_USER_TRACE_POLICY_PATH",
    ):
        documents.append(
            _parse_policy(path, layer=TracePolicyLayer.USER, source=path.resolve().as_posix())
        )
    return tuple(documents)


def resolve_trace_policy(
    project_root: Path,
    risk: str,
    *,
    environ: Mapping[str, str] | None = None,
) -> tuple[tuple[EffectiveThreshold, ...], tuple[str, ...]]:
    root = project_root.resolve()
    selected_risk = _validate_risk(risk)
    env = dict(os.environ if environ is None else environ)
    contributions: dict[CoverageDimension, list[TracePolicyContribution]] = {
        dimension: [
            TracePolicyContribution(
                layer=TracePolicyLayer.BUILTIN,
                source="builtin:trace-policy-defaults",
                policy_id="sdai-defaults",
                value=_DEFAULTS[selected_risk][dimension],
            )
        ]
        for dimension in CoverageDimension
    }
    sources = ["builtin:trace-policy-defaults"]
    for document in _policy_documents(root, environ=env):
        sources.append(f"{document.layer.value}:{document.source}")
        for dimension, value in document.risks.get(selected_risk, {}).items():
            contributions[dimension].append(
                TracePolicyContribution(
                    layer=document.layer,
                    source=document.source,
                    policy_id=document.policy_id,
                    value=value,
                )
            )
    thresholds: list[EffectiveThreshold] = []
    for dimension in CoverageDimension:
        items = tuple(
            sorted(
                contributions[dimension],
                key=lambda item: (item.layer.priority, item.source.casefold(), item.source, item.policy_id),
            )
        )
        # Non-weakening is monotonic by construction: later repo/user layers can
        # strengthen a threshold but can never reduce framework or organization minima.
        required = max(item.value for item in items)
        thresholds.append(
            EffectiveThreshold(
                dimension=dimension,
                required_percent=required,
                contributions=items,
            )
        )
    return tuple(thresholds), tuple(sources)


def _evidence_reports(root: Path, result: TraceBuildResult) -> dict[str, EvidenceFreshnessReport]:
    reports: dict[str, EvidenceFreshnessReport] = {}
    for node in result.graph.nodes:
        if node.type is not TraceNodeType.EVIDENCE:
            continue
        candidates: list[EvidenceFreshnessReport] = []
        seen_sources: set[str] = set()
        for provenance in node.provenance:
            source = provenance.source
            if source in seen_sources or not source.endswith(".json"):
                continue
            seen_sources.add(source)
            report = evaluate_trace_evidence_file(root, Path(source))
            if report.evidence_id == node.entity_id:
                candidates.append(report)
        if candidates:
            reports[node.entity_id] = max(
                candidates,
                key=lambda item: _PROOF_ORDER[item.freshness],
            )
    return reports


def _structural_reachable(graph: TraceGraph, start: str) -> set[str]:
    by_id = {node.node_id: node for node in graph.nodes}
    adjacency: dict[str, set[str]] = {node_id: set() for node_id in by_id}
    for edge in graph.edges:
        adjacency.setdefault(edge.source, set()).add(edge.target)
        adjacency.setdefault(edge.target, set()).add(edge.source)
    visited = {start}
    queue = [start]
    while queue:
        current = queue.pop(0)
        for neighbor in sorted(adjacency.get(current, ())):
            if neighbor in visited:
                continue
            node = by_id.get(neighbor)
            if node is None or node.type is TraceNodeType.EVIDENCE:
                continue
            if node.type is TraceNodeType.REQUIREMENT and neighbor != start:
                continue
            visited.add(neighbor)
            queue.append(neighbor)
    return visited


def _valid_evidence_for_requirement(
    graph: TraceGraph,
    requirement: TraceNode,
    structural: set[str],
    reports: Mapping[str, EvidenceFreshnessReport],
) -> tuple[TraceNode, ...]:
    by_id = {node.node_id: node for node in graph.nodes}
    evidence_ids: set[str] = set()
    for edge in graph.edges:
        if edge.source in structural:
            other = by_id.get(edge.target)
            if other is not None and other.type is TraceNodeType.EVIDENCE:
                evidence_ids.add(other.node_id)
        if edge.target in structural:
            other = by_id.get(edge.source)
            if other is not None and other.type is TraceNodeType.EVIDENCE:
                evidence_ids.add(other.node_id)
    result: list[TraceNode] = []
    for node_id in sorted(evidence_ids):
        node = by_id[node_id]
        report = reports.get(node.entity_id)
        if report is not None and report.freshness is ProofFreshness.VALID:
            result.append(node)
    return tuple(result)


def _requirement_states(
    graph: TraceGraph,
    reports: Mapping[str, EvidenceFreshnessReport],
) -> tuple[RequirementPolicyState, ...]:
    proofs = evaluate_trace_coverage(graph, reports)
    direct_current = {
        proof.source_node_id
        for proof in proofs
        if proof.satisfies_current_coverage
    }
    by_id = {node.node_id: node for node in graph.nodes}
    states: list[RequirementPolicyState] = []
    for requirement in graph.nodes:
        if requirement.type is not TraceNodeType.REQUIREMENT:
            continue
        structural = _structural_reachable(graph, requirement.node_id)
        reachable_nodes = [by_id[node_id] for node_id in structural if node_id in by_id]
        evidence = _valid_evidence_for_requirement(graph, requirement, structural, reports)
        evidence_kinds = {
            str((node.metadata or {}).get("kind", ""))
            for node in evidence
        }
        states.append(
            RequirementPolicyState(
                requirement_id=requirement.entity_id,
                node_id=requirement.node_id,
                current_proof=requirement.node_id in direct_current,
                task_link=any(node.type is TraceNodeType.TASK for node in reachable_nodes),
                code_link=any(node.type is TraceNodeType.CODE for node in reachable_nodes),
                test_link=(
                    any(node.type is TraceNodeType.TEST for node in reachable_nodes)
                    or "test" in evidence_kinds
                ),
                security_evidence="security" in evidence_kinds,
                approval_evidence="approval" in evidence_kinds,
            )
        )
    return tuple(sorted(states, key=lambda item: (item.requirement_id.casefold(), item.requirement_id)))


def _dimension_value(state: RequirementPolicyState, dimension: CoverageDimension) -> bool:
    return {
        CoverageDimension.REQUIREMENTS: state.current_proof,
        CoverageDimension.TASKS: state.task_link,
        CoverageDimension.CODE: state.code_link,
        CoverageDimension.TESTS: state.test_link,
        CoverageDimension.SECURITY: state.security_evidence,
        CoverageDimension.APPROVALS: state.approval_evidence,
    }[dimension]


def evaluate_trace_policy(
    project_root: Path,
    feature_id: str,
    risk: str,
    *,
    environ: Mapping[str, str] | None = None,
) -> TracePolicyReport:
    root = project_root.resolve()
    selected_risk = _validate_risk(risk)
    env = dict(os.environ if environ is None else environ)
    result = build_feature_trace_graph(root, feature_id, environ=env)
    reports = _evidence_reports(root, result)
    requirement_states = _requirement_states(result.graph, reports)
    thresholds, sources = resolve_trace_policy(root, selected_risk, environ=env)
    denominator = len(requirement_states)
    dimensions: list[CoverageDimensionResult] = []
    findings: list[TracePolicyFinding] = []
    for threshold in thresholds:
        numerator = sum(
            1
            for state in requirement_states
            if _dimension_value(state, threshold.dimension)
        )
        actual = 100.0 if denominator == 0 else round((numerator * 100.0) / denominator, 2)
        compliant = actual + 1e-9 >= threshold.required_percent
        result_item = CoverageDimensionResult(
            dimension=threshold.dimension,
            numerator=numerator,
            denominator=denominator,
            actual_percent=actual,
            threshold=threshold,
            compliant=compliant,
        )
        dimensions.append(result_item)
        if not compliant:
            findings.append(
                TracePolicyFinding(
                    code="SDAI-TRACE-POLICY-005",
                    severity="blocking",
                    dimension=threshold.dimension,
                    actual_percent=actual,
                    required_percent=threshold.required_percent,
                    message=(
                        f"{threshold.dimension.value} coverage {actual:.2f}% is below "
                        f"the effective {selected_risk} minimum {threshold.required_percent:.2f}%"
                    ),
                )
            )
    return TracePolicyReport(
        feature_id=result.graph.feature_id,
        risk=selected_risk,
        graph_sha256=result.graph.sha256,
        dimensions=tuple(dimensions),
        requirements=requirement_states,
        findings=tuple(findings),
        sources=sources,
    )
