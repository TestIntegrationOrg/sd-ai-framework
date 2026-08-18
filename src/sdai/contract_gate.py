from __future__ import annotations

from pathlib import Path
from typing import Mapping, Sequence

from sdai.constitution import ConstitutionError, load_constitution
from sdai.contract_policy import (
    ContractCriticality,
    ContractPolicyDecision,
    evaluate_contract_policy,
    load_effective_contract_policy,
)
from sdai.contracts import ContractDiffResult, ContractError
from sdai.trace_evidence import TraceEvidence, TraceEvidenceError, load_trace_evidence
from sdai.trace_freshness import (
    CommitPolicy,
    EvidenceFreshnessReport,
    TraceFreshnessError,
    evaluate_trace_evidence_freshness,
)


def load_contract_gate_evidence(
    project_root: Path,
    paths: Sequence[str | Path],
    *,
    commit_policy: CommitPolicy = CommitPolicy.ANCESTOR,
) -> tuple[tuple[TraceEvidence, ...], Mapping[str, EvidenceFreshnessReport]]:
    """Load canonical trace evidence and evaluate current freshness for a contract gate."""
    root = project_root.resolve()
    records: list[TraceEvidence] = []
    reports: dict[str, EvidenceFreshnessReport] = {}
    for raw_path in paths:
        path = Path(raw_path)
        try:
            record = load_trace_evidence(root, path)
            report = evaluate_trace_evidence_freshness(
                root,
                record,
                commit_policy=commit_policy,
            )
        except (TraceEvidenceError, TraceFreshnessError) as exc:
            raise ContractError(
                "SDAI-CONTRACT-POLICY-006",
                f"invalid contract governance evidence {path.as_posix()!r}: {exc}",
            ) from exc
        if record.evidence_id in reports:
            raise ContractError(
                "SDAI-CONTRACT-POLICY-006",
                f"duplicate contract governance evidence id: {record.evidence_id}",
            )
        records.append(record)
        reports[record.evidence_id] = report
    return tuple(records), reports


def evaluate_contract_gate(
    project_root: Path,
    diff: ContractDiffResult,
    *,
    criticality: ContractCriticality | str,
    evidence_paths: Sequence[str | Path] = (),
    environ: Mapping[str, str] | None = None,
    commit_policy: CommitPolicy = CommitPolicy.ANCESTOR,
) -> ContractPolicyDecision:
    """Evaluate a complete governed contract decision from current project truth."""
    root = project_root.resolve()
    policy = load_effective_contract_policy(root, environ=environ)
    try:
        constitution = load_constitution(root)
    except ConstitutionError as exc:
        raise ContractError(
            "SDAI-CONTRACT-POLICY-007",
            f"unable to load engineering constitution: {exc}",
        ) from exc
    records, reports = load_contract_gate_evidence(
        root,
        evidence_paths,
        commit_policy=commit_policy,
    )
    return evaluate_contract_policy(
        diff,
        policy,
        criticality=criticality,
        constitution=constitution,
        evidence=records,
        freshness_reports=reports,
    )
