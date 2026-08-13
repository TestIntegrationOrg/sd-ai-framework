from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
from typing import Mapping

from sdai.analysis_rules import analyze_feature
from sdai.artifact_state import ArtifactFreshness, ArtifactStateReport, evaluate_artifact_states
from sdai.cross_artifact import AnalysisFinding, SourceEvidence
from sdai.models import validate_feature_id
from sdai.path_safety import PathSafetyError, ensure_within_project
from sdai.trace_builder import TraceBuildResult, TraceGap, build_feature_trace_graph
from sdai.trace_evidence import EvidenceKind, EvidenceStatus, TraceEvidence, load_trace_evidence
from sdai.trace_freshness import (
    EvidenceFreshnessReport,
    ProofFreshness,
    evaluate_trace_evidence_freshness,
)
from sdai.trace_graph import TraceNode, TraceNodeType, TraceProvenance
from sdai.trace_policy import CoverageDimension, TracePolicyReport, evaluate_trace_policy
from sdai.verification import (
    SemanticReviewDimension,
    SemanticReviewEvidence,
    SemanticReviewState,
    VerificationCategory,
    VerificationFinding,
    VerificationFindingSource,
    VerificationReport,
    VerificationSeverity,
    VerificationStatus,
    evaluate_semantic_review_freshness,
    load_semantic_review_evidence,
)


class VerifyEngineError(RuntimeError):
    """Raised when current feature verification cannot be evaluated safely."""


_RISKS = frozenset({"trivial", "standard", "critical", "regulated"})


@dataclass(frozen=True)
class _ReviewEvaluation:
    review: SemanticReviewEvidence
    state: SemanticReviewState


@dataclass(frozen=True)
class _TypedEvidenceEvaluation:
    evidence: TraceEvidence
    freshness: EvidenceFreshnessReport


def _fail(code: str, message: str) -> VerifyEngineError:
    return VerifyEngineError(f"{code}: {message}")


def _canonical_hash(value: Mapping[str, object]) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + sha256(encoded).hexdigest()


def _risk(value: str) -> str:
    normalized = value.strip().lower() if isinstance(value, str) else ""
    if normalized not in _RISKS:
        raise _fail("SDAI-VERIFY-ENGINE-001", f"risk must be one of: {', '.join(sorted(_RISKS))}")
    return normalized


def _git_head(root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="strict",
            check=False,
            shell=False,
        )
    except (OSError, UnicodeError) as exc:
        raise _fail("SDAI-VERIFY-ENGINE-002", f"unable to execute git safely: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise _fail("SDAI-VERIFY-ENGINE-002", f"unable to resolve current Git commit: {detail}")
    commit = result.stdout.strip().casefold()
    if len(commit) not in {40, 64} or any(char not in "0123456789abcdef" for char in commit):
        raise _fail("SDAI-VERIFY-ENGINE-002", "git returned an invalid commit identity")
    return commit


def _requirements_provenance(feature_id: str) -> tuple[TraceProvenance, ...]:
    return (TraceProvenance(f"specs/changes/{feature_id}/requirements.md", 1),)


def _source_provenance(
    evidence: tuple[SourceEvidence, ...],
    *,
    feature_id: str,
) -> tuple[TraceProvenance, ...]:
    if not evidence:
        return _requirements_provenance(feature_id)
    result: list[TraceProvenance] = []
    seen: set[tuple[str, int]] = set()
    for item in evidence:
        key = (item.source, item.line)
        if key in seen:
            continue
        seen.add(key)
        result.append(
            TraceProvenance(
                item.source,
                item.line,
                detail=item.detail,
            )
        )
    return tuple(result) or _requirements_provenance(feature_id)


def _analysis_category(finding: AnalysisFinding) -> VerificationCategory:
    if finding.code == "STALE_ARTIFACT":
        return VerificationCategory.ARTIFACT_FRESHNESS
    if finding.code == "UNMITIGATED_THREAT":
        return VerificationCategory.SECURITY
    if finding.code == "UNAPPROVED_BREAKING_CHANGE":
        return VerificationCategory.APPROVAL
    if finding.code == "CONTRACT_CONFLICT":
        return VerificationCategory.CONTRACT
    return VerificationCategory.ANALYSIS


def _analysis_findings(feature_id: str, findings: tuple[AnalysisFinding, ...]) -> list[VerificationFinding]:
    result: list[VerificationFinding] = []
    for finding in findings:
        result.append(
            VerificationFinding(
                code=f"SDAI_VERIFY_ANALYSIS_{finding.code}",
                source=VerificationFindingSource.DETERMINISTIC,
                category=_analysis_category(finding),
                severity=(
                    VerificationSeverity.BLOCKING
                    if finding.severity == "blocking"
                    else VerificationSeverity.WARNING
                ),
                status=VerificationStatus.FAIL,
                message=finding.message,
                subject=finding.entity_id,
                provenance=_source_provenance(finding.evidence, feature_id=feature_id),
                metadata={"analysis_code": finding.code, "analysis_severity": finding.severity},
            )
        )
    return result


def _artifact_findings(
    feature_id: str,
    report: ArtifactStateReport,
    *,
    risk: str,
) -> list[VerificationFinding]:
    result: list[VerificationFinding] = []
    for state in report.states:
        if state.freshness is ArtifactFreshness.FRESH:
            continue
        if state.freshness is ArtifactFreshness.MISSING and not state.required:
            continue
        if state.required:
            severity = VerificationSeverity.BLOCKING
        else:
            severity = VerificationSeverity.WARNING
        status = {
            ArtifactFreshness.STALE: VerificationStatus.STALE,
            ArtifactFreshness.MISSING: VerificationStatus.MISSING,
            ArtifactFreshness.BLOCKED: VerificationStatus.BLOCKED,
        }[state.freshness]
        result.append(
            VerificationFinding(
                code=f"SDAI_VERIFY_ARTIFACT_{state.freshness.value.upper()}",
                source=VerificationFindingSource.DETERMINISTIC,
                category=VerificationCategory.ARTIFACT_FRESHNESS,
                severity=severity,
                status=status,
                message=(
                    f"Artifact '{state.artifact_id}' is {state.freshness.value}: "
                    + ("; ".join(state.reasons) or "current artifact state is not fresh")
                ),
                subject=f"artifact:{state.artifact_id}",
                provenance=(TraceProvenance(state.path, 1),),
                metadata={
                    "artifact_id": state.artifact_id,
                    "required": state.required,
                    "risk": risk,
                },
            )
        )
    return result


def _gap_finding(gap: TraceGap) -> VerificationFinding:
    return VerificationFinding(
        code="SDAI_VERIFY_TRACE_GAP",
        source=VerificationFindingSource.DETERMINISTIC,
        category=VerificationCategory.TRACE_COVERAGE,
        severity=VerificationSeverity.BLOCKING,
        status=VerificationStatus.MISSING,
        message=(
            f"Trace relationship '{gap.relation}' cannot resolve target '{gap.target}' "
            f"({gap.kind})."
        ),
        subject=gap.source_node_id or gap.target,
        provenance=(TraceProvenance(gap.source, gap.line, detail=gap.detail),),
        metadata={"gap_kind": gap.kind, "target": gap.target, "relation": gap.relation},
    )


def _policy_category(dimension: CoverageDimension) -> VerificationCategory:
    if dimension is CoverageDimension.SECURITY:
        return VerificationCategory.SECURITY
    if dimension is CoverageDimension.APPROVALS:
        return VerificationCategory.APPROVAL
    if dimension is CoverageDimension.TESTS:
        return VerificationCategory.TEST
    return VerificationCategory.TRACE_COVERAGE


def _policy_findings(feature_id: str, report: TracePolicyReport) -> list[VerificationFinding]:
    result: list[VerificationFinding] = []
    for finding in report.findings:
        result.append(
            VerificationFinding(
                code=f"SDAI_VERIFY_POLICY_{finding.dimension.value.upper()}",
                source=VerificationFindingSource.DETERMINISTIC,
                category=_policy_category(finding.dimension),
                severity=VerificationSeverity.BLOCKING,
                status=VerificationStatus.FAIL,
                message=finding.message,
                subject=f"feature:{feature_id}",
                provenance=_requirements_provenance(feature_id),
                metadata={
                    "dimension": finding.dimension.value,
                    "actual_percent": finding.actual_percent,
                    "required_percent": finding.required_percent,
                    "risk": report.risk,
                },
            )
        )
    return result


def _review_root(root: Path, feature_id: str) -> Path:
    try:
        return ensure_within_project(
            root,
            root / ".sdai" / "verification" / feature_id / "reviews",
            label="semantic review directory",
        )
    except PathSafetyError as exc:
        raise _fail("SDAI-VERIFY-ENGINE-003", "semantic review directory escapes project root") from exc


def _semantic_reviews(root: Path, feature_id: str) -> tuple[_ReviewEvaluation, ...]:
    directory = _review_root(root, feature_id)
    if not directory.exists():
        return ()
    if directory.is_symlink() or not directory.is_dir():
        raise _fail("SDAI-VERIFY-ENGINE-003", "semantic review path must be a regular directory")
    evaluations: list[_ReviewEvaluation] = []
    for path in sorted(directory.rglob("*.json"), key=lambda item: item.relative_to(root).as_posix().casefold()):
        if path.is_symlink() or not path.is_file():
            raise _fail(
                "SDAI-VERIFY-ENGINE-003",
                f"semantic review must be a regular non-symlink JSON file: {path.relative_to(root).as_posix()}",
            )
        review = load_semantic_review_evidence(root, path)
        state = evaluate_semantic_review_freshness(root, review)
        evaluations.append(_ReviewEvaluation(review=review, state=state))
    return tuple(evaluations)


def _required_semantic_reviews(
    graph_nodes: tuple[TraceNode, ...],
    feature_id: str,
    risk: str,
) -> tuple[tuple[SemanticReviewDimension, str, tuple[TraceProvenance, ...]], ...]:
    requirements = tuple(
        node for node in graph_nodes if node.type is TraceNodeType.REQUIREMENT
    )
    required: list[tuple[SemanticReviewDimension, str, tuple[TraceProvenance, ...]]] = []
    for node in requirements:
        required.append(
            (
                SemanticReviewDimension.REQUIREMENT_SATISFACTION,
                node.node_id,
                node.provenance,
            )
        )
    if risk in {"standard", "critical", "regulated"}:
        required.append(
            (
                SemanticReviewDimension.FAILURE_BEHAVIOR,
                f"feature:{feature_id}",
                _requirements_provenance(feature_id),
            )
        )
    if risk in {"critical", "regulated"}:
        required.extend(
            (
                (
                    SemanticReviewDimension.ARCHITECTURE_INTENT,
                    f"feature:{feature_id}",
                    _requirements_provenance(feature_id),
                ),
                (
                    SemanticReviewDimension.UNDOCUMENTED_BEHAVIOR,
                    f"feature:{feature_id}",
                    _requirements_provenance(feature_id),
                ),
            )
        )
    return tuple(required)


def _semantic_status(review: _ReviewEvaluation) -> VerificationStatus:
    state = review.state
    if state.freshness is ProofFreshness.MISSING:
        return VerificationStatus.MISSING
    if state.freshness is ProofFreshness.STALE:
        return VerificationStatus.STALE
    if review.review.status is EvidenceStatus.FAILED:
        return VerificationStatus.FAIL
    if review.review.status is EvidenceStatus.BLOCKED or state.freshness is ProofFreshness.BLOCKED:
        return VerificationStatus.BLOCKED
    return VerificationStatus.PASS


def _semantic_findings(
    feature_id: str,
    graph_nodes: tuple[TraceNode, ...],
    evaluations: tuple[_ReviewEvaluation, ...],
    *,
    risk: str,
) -> list[VerificationFinding]:
    grouped: dict[tuple[SemanticReviewDimension, str], list[_ReviewEvaluation]] = {}
    for item in evaluations:
        grouped.setdefault((item.review.dimension, item.review.subject), []).append(item)
    result: list[VerificationFinding] = []
    for dimension, subject, provenance in _required_semantic_reviews(graph_nodes, feature_id, risk):
        candidates = sorted(
            grouped.get((dimension, subject), ()),
            key=lambda item: (item.review.review_id, item.review.truth_sha256),
        )
        current_passes = [
            item
            for item in candidates
            if item.review.status is EvidenceStatus.PASSED
            and item.state.freshness is ProofFreshness.VALID
        ]
        current_negative = [
            item
            for item in candidates
            if _semantic_status(item) in {VerificationStatus.FAIL, VerificationStatus.BLOCKED}
        ]
        if current_negative:
            for item in current_negative:
                result.append(
                    VerificationFinding(
                        code="SDAI_VERIFY_SEMANTIC_REJECTED",
                        source=VerificationFindingSource.SEMANTIC,
                        category=item.review.category,
                        severity=VerificationSeverity.BLOCKING,
                        status=_semantic_status(item),
                        message=item.review.summary,
                        subject=subject,
                        evidence_truth_sha256=item.review.truth_sha256,
                        provenance=item.review.evidence.provenance,
                        metadata={"dimension": dimension.value, "review_id": item.review.review_id},
                    )
                )
            continue
        if current_passes:
            item = current_passes[0]
            result.append(
                VerificationFinding(
                    code="SDAI_VERIFY_SEMANTIC_PASS",
                    source=VerificationFindingSource.SEMANTIC,
                    category=item.review.category,
                    severity=VerificationSeverity.BLOCKING,
                    status=VerificationStatus.PASS,
                    message=item.review.summary,
                    subject=subject,
                    evidence_truth_sha256=item.review.truth_sha256,
                    provenance=item.review.evidence.provenance,
                    metadata={"dimension": dimension.value, "review_id": item.review.review_id},
                )
            )
            continue
        stale = [
            item
            for item in candidates
            if item.state.freshness in {ProofFreshness.STALE, ProofFreshness.MISSING}
        ]
        if stale:
            item = stale[0]
            result.append(
                VerificationFinding(
                    code="SDAI_VERIFY_SEMANTIC_STALE",
                    source=VerificationFindingSource.SEMANTIC,
                    category=item.review.category,
                    severity=VerificationSeverity.REVIEW,
                    status=_semantic_status(item),
                    message=(
                        f"Semantic review '{item.review.review_id}' is not current for "
                        f"{dimension.value}: {'; '.join(item.state.reasons)}"
                    ),
                    subject=subject,
                    evidence_truth_sha256=item.review.truth_sha256,
                    provenance=item.review.evidence.provenance,
                    metadata={"dimension": dimension.value, "review_id": item.review.review_id},
                )
            )
            continue
        result.append(
            VerificationFinding(
                code="SDAI_VERIFY_SEMANTIC_REQUIRED",
                source=VerificationFindingSource.DETERMINISTIC,
                category={
                    SemanticReviewDimension.REQUIREMENT_SATISFACTION: VerificationCategory.REQUIREMENT_SATISFACTION,
                    SemanticReviewDimension.ARCHITECTURE_INTENT: VerificationCategory.ARCHITECTURE_INTENT,
                    SemanticReviewDimension.FAILURE_BEHAVIOR: VerificationCategory.FAILURE_BEHAVIOR,
                    SemanticReviewDimension.UNDOCUMENTED_BEHAVIOR: VerificationCategory.UNDOCUMENTED_BEHAVIOR,
                }[dimension],
                severity=VerificationSeverity.REVIEW,
                status=VerificationStatus.REVIEW_REQUIRED,
                message=f"Current semantic review is required for {dimension.value} on {subject}.",
                subject=subject,
                provenance=provenance,
                metadata={"dimension": dimension.value, "risk": risk},
            )
        )
    return result


def _typed_evidence(
    root: Path,
    build: TraceBuildResult,
) -> tuple[_TypedEvidenceEvaluation, ...]:
    evaluations: dict[str, _TypedEvidenceEvaluation] = {}
    for node in build.graph.nodes:
        if node.type is not TraceNodeType.EVIDENCE:
            continue
        loaded: TraceEvidence | None = None
        for provenance in node.provenance:
            if not provenance.source.endswith(".json"):
                continue
            try:
                record = load_trace_evidence(root, Path(provenance.source))
            except RuntimeError:
                continue
            if record.evidence_id == node.entity_id:
                loaded = record
                break
        if loaded is None:
            continue
        evaluations[loaded.evidence_id] = _TypedEvidenceEvaluation(
            evidence=loaded,
            freshness=evaluate_trace_evidence_freshness(root, loaded),
        )
    return tuple(evaluations[key] for key in sorted(evaluations))


def _execution_findings(evaluations: tuple[_TypedEvidenceEvaluation, ...]) -> list[VerificationFinding]:
    grouped: dict[str, list[_TypedEvidenceEvaluation]] = {}
    for item in evaluations:
        if item.evidence.kind is EvidenceKind.EXECUTION:
            grouped.setdefault(item.evidence.subject, []).append(item)
    result: list[VerificationFinding] = []
    for subject in sorted(grouped):
        items = grouped[subject]
        if any(
            item.evidence.status is EvidenceStatus.PASSED
            and item.freshness.freshness is ProofFreshness.VALID
            for item in items
        ):
            continue
        current_failures = [
            item
            for item in items
            if item.evidence.status in {EvidenceStatus.FAILED, EvidenceStatus.BLOCKED}
            and item.freshness.freshness not in {ProofFreshness.STALE, ProofFreshness.MISSING}
        ]
        if current_failures:
            item = sorted(current_failures, key=lambda value: value.evidence.evidence_id)[0]
            result.append(
                VerificationFinding(
                    code="SDAI_VERIFY_EXECUTION_FAILED",
                    source=VerificationFindingSource.DETERMINISTIC,
                    category=VerificationCategory.EXECUTION,
                    severity=VerificationSeverity.BLOCKING,
                    status=VerificationStatus.FAIL,
                    message=f"Current execution evidence for {subject} reports failure or blockage.",
                    subject=subject,
                    provenance=item.evidence.provenance,
                    metadata={"evidence_id": item.evidence.evidence_id},
                )
            )
            continue
        stale = [
            item
            for item in items
            if item.freshness.freshness in {ProofFreshness.STALE, ProofFreshness.MISSING}
        ]
        if stale:
            item = sorted(stale, key=lambda value: value.evidence.evidence_id)[0]
            result.append(
                VerificationFinding(
                    code="SDAI_VERIFY_EXECUTION_STALE",
                    source=VerificationFindingSource.DETERMINISTIC,
                    category=VerificationCategory.EXECUTION,
                    severity=VerificationSeverity.WARNING,
                    status=VerificationStatus.STALE,
                    message=f"Execution evidence for {subject} is not current.",
                    subject=subject,
                    provenance=item.evidence.provenance,
                    metadata={"evidence_id": item.evidence.evidence_id},
                )
            )
    return result


def verify_feature(
    project_root: Path,
    feature_id: str,
    *,
    risk: str = "standard",
    environ: Mapping[str, str] | None = None,
) -> VerificationReport:
    """Evaluate current deterministic truth plus already-recorded semantic review evidence.

    This function never invokes a provider and never writes repository state.
    """
    root = project_root.resolve()
    feature = validate_feature_id(feature_id)
    selected_risk = _risk(risk)
    env = dict(os.environ if environ is None else environ)
    head = _git_head(root)

    artifact_report = evaluate_artifact_states(
        root,
        feature,
        risk=selected_risk,
        environ=env,
    )
    analysis_report = analyze_feature(
        root,
        feature,
        risk=selected_risk,
        environ=env,
    )
    trace_build = build_feature_trace_graph(root, feature, environ=env)
    policy_report = evaluate_trace_policy(
        root,
        feature,
        selected_risk,
        environ=env,
    )
    reviews = _semantic_reviews(root, feature)
    typed_evidence = _typed_evidence(root, trace_build)

    findings: list[VerificationFinding] = []
    findings.extend(_artifact_findings(feature, artifact_report, risk=selected_risk))
    findings.extend(_analysis_findings(feature, analysis_report.findings))
    findings.extend(_gap_finding(gap) for gap in trace_build.gaps)
    findings.extend(_policy_findings(feature, policy_report))
    findings.extend(
        _semantic_findings(
            feature,
            trace_build.graph.nodes,
            reviews,
            risk=selected_risk,
        )
    )
    findings.extend(_execution_findings(typed_evidence))

    input_sha256 = _canonical_hash(
        {
            "git_commit": head,
            "risk": selected_risk,
            "artifact_state": artifact_report.as_dict(),
            "analysis": analysis_report.as_dict(),
            "trace_graph_sha256": trace_build.graph.sha256,
            "trace_gaps": [gap.as_dict() for gap in trace_build.gaps],
            "trace_policy": policy_report.as_dict(),
            "semantic_reviews": [item.state.as_dict() for item in reviews],
            "typed_evidence": [item.freshness.as_dict() for item in typed_evidence],
        }
    )
    return VerificationReport(
        feature_id=feature,
        git_commit=head,
        input_sha256=input_sha256,
        findings=tuple(findings),
        semantic_reviews=tuple(item.state for item in reviews),
    )


__all__ = ["VerifyEngineError", "verify_feature"]
