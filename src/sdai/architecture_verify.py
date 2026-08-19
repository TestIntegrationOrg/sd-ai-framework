from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
from typing import Iterable, Mapping

from sdai.architecture_drift import ArchitectureDriftFinding, ArchitectureFactKind
from sdai.architecture_engine import ArchitectureDriftEvaluation, evaluate_architecture_drift
from sdai.trace_graph import TraceProvenance
from sdai.verification import (
    VerificationCategory,
    VerificationFinding,
    VerificationFindingSource,
    VerificationReport,
    VerificationSeverity,
    VerificationStatus,
)
from sdai.verify_engine import verify_feature as _base_verify_feature


def _canonical_hash(value: Mapping[str, object]) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + sha256(encoded).hexdigest()


def _plain(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    if isinstance(value, list):
        return [_plain(item) for item in value]
    return value


def _provenance(values: Iterable[TraceProvenance], feature_id: str) -> tuple[TraceProvenance, ...]:
    by_location: dict[tuple[str, int], TraceProvenance] = {}
    for item in values:
        previous = by_location.get(item.location)
        if previous is None:
            by_location[item.location] = item
            continue
        by_location[item.location] = min(
            (previous, item),
            key=lambda value: (value.declaration_sha256 or "", value.detail or ""),
        )
    if not by_location:
        return (
            TraceProvenance(
                f"specs/changes/{feature_id}/requirements.md",
                1,
                detail="architecture drift governance",
            ),
        )
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


def _category(kind: ArchitectureFactKind) -> VerificationCategory:
    if kind is ArchitectureFactKind.TRUST_BOUNDARY:
        return VerificationCategory.SECURITY
    if kind is ArchitectureFactKind.CONTRACT:
        return VerificationCategory.CONTRACT
    return VerificationCategory.ARCHITECTURE_INTENT


def _finding(
    evaluation: ArchitectureDriftEvaluation,
    finding: ArchitectureDriftFinding,
) -> VerificationFinding:
    blocked = evaluation.policy.blocks(finding)
    return VerificationFinding(
        code="SDAI_VERIFY_ARCH_" + finding.code.replace("-", "_"),
        source=VerificationFindingSource.DETERMINISTIC,
        category=_category(finding.kind),
        severity=VerificationSeverity.BLOCKING if blocked else VerificationSeverity.WARNING,
        status=VerificationStatus.FAIL,
        message=finding.message,
        subject=f"architecture:{finding.kind.value}:{finding.source}->{finding.target}",
        provenance=_provenance(
            (*finding.approved_provenance, *finding.observed_provenance),
            evaluation.feature_id,
        ),
        metadata={
            "architecture_code": finding.code,
            "architecture_kind": finding.kind.value,
            "architecture_severity": finding.severity.value,
            "architecture_report_sha256": evaluation.report.sha256 if evaluation.report else None,
            "architecture_policy_sha256": evaluation.policy.sha256,
            "approved_fact_id": finding.approved_fact_id,
            "attributes": _plain(finding.attributes),
            "policy_blocked": blocked,
        },
    )


def _policy_findings(evaluation: ArchitectureDriftEvaluation) -> list[VerificationFinding]:
    result: list[VerificationFinding] = []
    report_codes = {finding.code for finding in evaluation.report.findings} if evaluation.report else set()
    for blocker in evaluation.decision.blockers:
        if blocker.code in report_codes:
            continue
        missing_topology = blocker.code == "ARCH-POLICY-TOPOLOGY-REQUIRED"
        result.append(
            VerificationFinding(
                code="SDAI_VERIFY_ARCH_" + blocker.code.replace("-", "_"),
                source=VerificationFindingSource.DETERMINISTIC,
                category=(
                    VerificationCategory.ARCHITECTURE_INTENT
                    if missing_topology
                    else VerificationCategory.APPROVAL
                ),
                severity=VerificationSeverity.BLOCKING,
                status=(VerificationStatus.MISSING if missing_topology else VerificationStatus.BLOCKED),
                message=blocker.reason,
                subject=f"feature:{evaluation.feature_id}",
                provenance=_provenance((), evaluation.feature_id),
                metadata={
                    "architecture_policy_sha256": evaluation.policy.sha256,
                    "architecture_evaluation_sha256": evaluation.sha256,
                },
            )
        )
    return result


def _is_architecture_approval_gap(
    finding: VerificationFinding,
    evaluation: ArchitectureDriftEvaluation,
) -> bool:
    if evaluation.report is None:
        return False
    if finding.code != "SDAI_VERIFY_TRACE_GAP":
        return False
    metadata = finding.metadata or {}
    return (
        metadata.get("gap_kind") == "missing-evidence-subject"
        and metadata.get("target")
        == f"architecture-topology:{evaluation.feature_id}:{evaluation.report.topology_sha256}"
    )


def _filter_resolved_architecture_gap(
    findings: Iterable[VerificationFinding],
    evaluation: ArchitectureDriftEvaluation,
) -> list[VerificationFinding]:
    # Older trace builds record architecture approval as an unresolved evidence
    # subject because the topology subject is not a node id. The architecture trace
    # projection resolves that relationship. Match either the exact approval subject
    # carried in the finding message/metadata or leave unrelated gaps untouched.
    result: list[VerificationFinding] = []
    topology_prefix = f"architecture-topology:{evaluation.feature_id}:"
    for finding in findings:
        if evaluation.report is not None and finding.code == "SDAI_VERIFY_TRACE_GAP":
            metadata = finding.metadata or {}
            target = metadata.get("target")
            if metadata.get("gap_kind") == "missing-evidence-subject" and isinstance(target, str) and target.startswith(topology_prefix):
                continue
        result.append(finding)
    return result


def verify_feature_with_architecture(
    project_root: Path,
    feature_id: str,
    *,
    risk: str = "standard",
    environ: Mapping[str, str] | None = None,
) -> VerificationReport:
    """Return the ordinary verification report enriched with governed architecture drift."""
    base = _base_verify_feature(project_root, feature_id, risk=risk, environ=environ)
    evaluation = evaluate_architecture_drift(project_root, feature_id, environ=environ)
    findings = _filter_resolved_architecture_gap(base.findings, evaluation)
    if evaluation.report is not None:
        findings.extend(_finding(evaluation, item) for item in evaluation.report.findings)
    findings.extend(_policy_findings(evaluation))
    input_sha256 = _canonical_hash(
        {
            "base_input_sha256": base.input_sha256,
            "architecture_evaluation_sha256": evaluation.sha256,
        }
    )
    return VerificationReport(
        feature_id=base.feature_id,
        git_commit=base.git_commit,
        input_sha256=input_sha256,
        findings=tuple(findings),
        semantic_reviews=base.semantic_reviews,
    )


__all__ = ["verify_feature_with_architecture"]
