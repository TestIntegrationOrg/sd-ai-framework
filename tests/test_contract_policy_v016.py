from __future__ import annotations

from pathlib import Path

from sdai.contract_adapters import default_contract_registry
from sdai.contract_policy import (
    ContractChangeClass,
    ContractCriticality,
    ContractEvidenceType,
    ContractPolicyOutcome,
    classify_contract_diff,
    contract_policy_exit_code,
    evaluate_contract_policy,
    load_effective_contract_policy,
)
from sdai.contracts import (
    CompatibilityDirection,
    ContractDiffResult,
    ContractFinding,
    ContractSeverity,
    ContractSource,
    diff_contracts,
    load_contract_snapshot,
)
from sdai.trace_evidence import (
    EvidenceBinding,
    EvidenceBindingKind,
    EvidenceKind,
    EvidenceProducer,
    EvidenceStatus,
    TraceEvidence,
)
from sdai.trace_freshness import (
    CommitPolicy,
    EvidenceFreshnessReport,
    ProofFreshness,
)
from sdai.trace_graph import TraceProvenance


_SHA_A = "sha256:" + "a" * 64
_SHA_B = "sha256:" + "b" * 64
_SHA_C = "sha256:" + "c" * 64
_CONSTITUTION = "d" * 64
_COMMIT = "e" * 40


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _project(root: Path) -> Path:
    _write(root / ".sdai" / "config.yaml", "operating_mode: individual\n")
    return root


def _schema_snapshot(root: Path, name: str, text: str):
    path = f"contracts/{name}.json"
    _write(root / path, text)
    return load_contract_snapshot(
        root,
        ContractSource(source_id=name, kind="json-schema", path=path),
    )


def _breaking_diff(root: Path) -> ContractDiffResult:
    before = _schema_snapshot(root, "before", '{"type":"string"}')
    after = _schema_snapshot(root, "after", '{"type":"integer"}')
    return diff_contracts(before, after, default_contract_registry((before, after)))


def _freshness(record: TraceEvidence, state: ProofFreshness = ProofFreshness.VALID) -> EvidenceFreshnessReport:
    return EvidenceFreshnessReport(
        evidence_id=record.evidence_id,
        subject=record.subject,
        freshness=state,
        evidence_git_commit=record.git_commit,
        current_git_commit=record.git_commit,
        commit_policy=CommitPolicy.ANCESTOR,
        commit_reachable=state is ProofFreshness.VALID,
        bindings=(),
        reasons=("fresh" if state is ProofFreshness.VALID else "stale test proof",),
    )


def _evidence(
    evidence_id: str,
    evidence_type: ContractEvidenceType,
    diff: ContractDiffResult,
    policy_sha256: str,
    *,
    provider: str | None = None,
    model: str | None = None,
    candidate_sha256: str | None = None,
) -> TraceEvidence:
    if evidence_type is ContractEvidenceType.ARCHITECTURE_APPROVAL:
        kind = EvidenceKind.APPROVAL
        role = "architecture-approver"
        artifact = "specs/demo/architecture-approval.md"
    elif evidence_type is ContractEvidenceType.MIGRATION_PLAN:
        kind = EvidenceKind.REVIEW
        role = "release-planner"
        artifact = "specs/demo/migration-plan.md"
    else:
        kind = EvidenceKind.REVIEW
        role = "architect"
        artifact = "specs/demo/adr.md"
    return TraceEvidence(
        evidence_id=evidence_id,
        kind=kind,
        status=EvidenceStatus.RECORDED,
        subject=f"contract-policy:{evidence_type.value}",
        git_commit=_COMMIT,
        bindings=(EvidenceBinding(EvidenceBindingKind.ARTIFACT, artifact, _SHA_A),),
        provenance=(TraceProvenance(source=artifact, line=1),),
        producer=EvidenceProducer(role, provider=provider, model=model),
        result={
            "contractPolicy": {
                "evidenceType": evidence_type.value,
                "baselineSha256": diff.before.sha256,
                "candidateSha256": candidate_sha256 or diff.after.sha256,
                "diffSha256": diff.sha256,
                "policySha256": policy_sha256,
                "constitutionSha256": "sha256:" + _CONSTITUTION,
            }
        },
    )


def test_known_breaking_and_unknown_findings_have_stable_classes(tmp_path: Path) -> None:
    diff = _breaking_diff(tmp_path)
    change_class, classifications = classify_contract_diff(diff)
    assert change_class is ContractChangeClass.BREAKING
    assert {item.code for item in classifications} == {"SDAI-CONTRACT-JSONSCHEMA-DIFF-010"}
    assert {item.change_class for item in classifications} == {ContractChangeClass.BREAKING}

    unknown = ContractDiffResult(
        before=diff.before,
        after=diff.after,
        direction=CompatibilityDirection.BACKWARD,
        findings=(
            ContractFinding(
                code="SDAI-CONTRACT-JSONSCHEMA-DIFF-999",
                severity=ContractSeverity.ERROR,
                message="future finding",
            ),
        ),
        sha256=_SHA_B,
    )
    unknown_class, unknown_findings = classify_contract_diff(unknown)
    assert unknown_class is ContractChangeClass.UNKNOWN
    assert unknown_findings[0].change_class is ContractChangeClass.UNKNOWN


def test_core_policy_blocks_unknown_and_requires_critical_migration_and_architecture_evidence(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path / "project")
    policy = load_effective_contract_policy(root, environ={})
    critical = policy.rule_for(ContractCriticality.CRITICAL)
    assert critical.allow_breaking is True
    assert critical.allow_unknown is False
    assert critical.required_evidence == (
        ContractEvidenceType.ARCHITECTURE_APPROVAL,
        ContractEvidenceType.MIGRATION_PLAN,
    )


def test_lower_policy_layers_cannot_weaken_core_or_organization_requirements(tmp_path: Path) -> None:
    root = _project(tmp_path / "project")
    org = tmp_path / "org-contract-policy.yaml"
    user = tmp_path / "user-contract-policy.yaml"
    policy_header = "apiVersion: sdai.contract-policy/v1\nkind: ContractPolicy\nrules:\n"
    _write(
        org,
        policy_header
        + "  critical:\n"
        + "    allowBreaking: false\n"
        + "    allowUnknown: false\n"
        + "    requiredEvidence: [adr]\n",
    )
    _write(
        root / ".sdai" / "contract-policy.yaml",
        policy_header
        + "  critical:\n"
        + "    allowBreaking: true\n"
        + "    allowUnknown: true\n"
        + "    requiredEvidence: []\n",
    )
    _write(
        user,
        policy_header
        + "  critical:\n"
        + "    allowBreaking: true\n"
        + "    allowUnknown: true\n"
        + "    requiredEvidence: []\n",
    )
    policy = load_effective_contract_policy(
        root,
        environ={
            "SDAI_ORG_CONTRACT_POLICY_PATH": str(org),
            "SDAI_USER_CONTRACT_POLICY_PATH": str(user),
        },
    )
    rule = policy.rule_for("critical")
    assert rule.allow_breaking is False
    assert rule.allow_unknown is False
    assert rule.required_evidence == (
        ContractEvidenceType.ADR,
        ContractEvidenceType.ARCHITECTURE_APPROVAL,
        ContractEvidenceType.MIGRATION_PLAN,
    )


def test_effective_policy_hash_is_independent_of_absolute_external_policy_paths(tmp_path: Path) -> None:
    policy_text = (
        "apiVersion: sdai.contract-policy/v1\n"
        "kind: ContractPolicy\n"
        "rules:\n"
        "  standard:\n"
        "    requiredEvidence: [adr]\n"
    )
    first_root = _project(tmp_path / "one" / "project")
    second_root = _project(tmp_path / "two" / "project")
    first_org = tmp_path / "one" / "org.yaml"
    second_org = tmp_path / "two" / "different-name.yaml"
    _write(first_org, policy_text)
    _write(second_org, policy_text)
    first = load_effective_contract_policy(
        first_root,
        environ={"SDAI_ORG_CONTRACT_POLICY_PATH": str(first_org)},
    )
    second = load_effective_contract_policy(
        second_root,
        environ={"SDAI_ORG_CONTRACT_POLICY_PATH": str(second_org)},
    )
    assert first.sha256 == second.sha256
    assert first.to_dict()["rules"] == second.to_dict()["rules"]


def test_breaking_critical_change_is_blocked_without_required_evidence(tmp_path: Path) -> None:
    root = _project(tmp_path / "project")
    diff = _breaking_diff(root)
    policy = load_effective_contract_policy(root, environ={})
    decision = evaluate_contract_policy(
        diff,
        policy,
        criticality="critical",
        constitution=_CONSTITUTION,
    )
    assert decision.change_class is ContractChangeClass.BREAKING
    assert decision.outcome is ContractPolicyOutcome.BLOCKED
    assert decision.allowed is False
    assert contract_policy_exit_code(decision) == 2
    assert any("architecture-approval" in reason and "migration-plan" in reason for reason in decision.reasons)


def test_fresh_hash_bound_migration_and_human_architecture_approval_allow_critical_breaking_change(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path / "project")
    diff = _breaking_diff(root)
    policy = load_effective_contract_policy(root, environ={})
    approval = _evidence("approval-1", ContractEvidenceType.ARCHITECTURE_APPROVAL, diff, policy.sha256)
    migration = _evidence("migration-1", ContractEvidenceType.MIGRATION_PLAN, diff, policy.sha256)
    reports = {
        approval.evidence_id: _freshness(approval),
        migration.evidence_id: _freshness(migration),
    }
    first = evaluate_contract_policy(
        diff,
        policy,
        criticality=ContractCriticality.CRITICAL,
        constitution=_CONSTITUTION,
        evidence=(migration, approval),
        freshness_reports=reports,
    )
    second = evaluate_contract_policy(
        diff,
        policy,
        criticality=ContractCriticality.CRITICAL,
        constitution=_CONSTITUTION,
        evidence=(approval, migration),
        freshness_reports=reports,
    )
    assert first.allowed
    assert first.outcome is ContractPolicyOutcome.ALLOWED
    assert contract_policy_exit_code(first) == 0
    assert first.to_json() == second.to_json()
    assert first.sha256 == second.sha256


def test_stale_or_mismatched_evidence_cannot_satisfy_required_gate(tmp_path: Path) -> None:
    root = _project(tmp_path / "project")
    diff = _breaking_diff(root)
    policy = load_effective_contract_policy(root, environ={})
    approval = _evidence("approval-1", ContractEvidenceType.ARCHITECTURE_APPROVAL, diff, policy.sha256)
    stale_migration = _evidence("migration-1", ContractEvidenceType.MIGRATION_PLAN, diff, policy.sha256)
    reports = {
        approval.evidence_id: _freshness(approval),
        stale_migration.evidence_id: _freshness(stale_migration, ProofFreshness.STALE),
    }
    stale = evaluate_contract_policy(
        diff,
        policy,
        criticality="critical",
        constitution=_CONSTITUTION,
        evidence=(approval, stale_migration),
        freshness_reports=reports,
    )
    assert not stale.allowed
    migration_assessment = next(
        item for item in stale.evidence if item.evidence_type is ContractEvidenceType.MIGRATION_PLAN
    )
    assert not migration_assessment.accepted
    assert any("not fresh" in reason for reason in migration_assessment.reasons)

    mismatched = _evidence(
        "migration-2",
        ContractEvidenceType.MIGRATION_PLAN,
        diff,
        policy.sha256,
        candidate_sha256=_SHA_C,
    )
    reports[mismatched.evidence_id] = _freshness(mismatched)
    mismatch_decision = evaluate_contract_policy(
        diff,
        policy,
        criticality="critical",
        constitution=_CONSTITUTION,
        evidence=(approval, mismatched),
        freshness_reports=reports,
    )
    assert not mismatch_decision.allowed
    mismatch_assessment = next(item for item in mismatch_decision.evidence if item.evidence_id == "migration-2")
    assert any("candidateSha256" in reason for reason in mismatch_assessment.reasons)


def test_ai_provider_cannot_self_approve_architecture_breaking_change(tmp_path: Path) -> None:
    root = _project(tmp_path / "project")
    diff = _breaking_diff(root)
    policy = load_effective_contract_policy(root, environ={})
    ai_approval = _evidence(
        "approval-ai",
        ContractEvidenceType.ARCHITECTURE_APPROVAL,
        diff,
        policy.sha256,
        provider="codex",
        model="example-model",
    )
    migration = _evidence("migration-1", ContractEvidenceType.MIGRATION_PLAN, diff, policy.sha256)
    reports = {
        ai_approval.evidence_id: _freshness(ai_approval),
        migration.evidence_id: _freshness(migration),
    }
    decision = evaluate_contract_policy(
        diff,
        policy,
        criticality="critical",
        constitution=_CONSTITUTION,
        evidence=(ai_approval, migration),
        freshness_reports=reports,
    )
    assert not decision.allowed
    assessment = next(item for item in decision.evidence if item.evidence_id == "approval-ai")
    assert any("AI provider/model" in reason for reason in assessment.reasons)


def test_non_breaking_change_is_allowed_without_governance_evidence(tmp_path: Path) -> None:
    root = _project(tmp_path / "project")
    before = _schema_snapshot(root, "before", '{"type":"string","enum":["a"]}')
    after = _schema_snapshot(root, "after", '{"type":["string","null"],"enum":["a",null]}')
    diff = diff_contracts(before, after, default_contract_registry((before, after)))
    policy = load_effective_contract_policy(root, environ={})
    decision = evaluate_contract_policy(
        diff,
        policy,
        criticality="critical",
        constitution=_CONSTITUTION,
    )
    assert decision.change_class is ContractChangeClass.NON_BREAKING
    assert decision.allowed
    assert decision.required_evidence == ()
