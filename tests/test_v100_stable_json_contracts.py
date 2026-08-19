from __future__ import annotations

from hashlib import sha256
import json

from sdai.architecture_drift import ARCHITECTURE_DRIFT_API_VERSION
from sdai.artifact_state_cli import (
    ARTIFACT_STATE_EXPLAIN_API_VERSION,
    ARTIFACT_STATE_REPORT_API_VERSION,
)
from sdai.audit_report import AUDIT_REPORT_API_VERSION
from sdai.context_explain import CONTEXT_EXPLAIN_API_VERSION
from sdai.contracts import CONTRACT_DIFF_API_VERSION, CONTRACT_RESULT_API_VERSION
from sdai.convergence import CONVERGENCE_STATE_API_VERSION
from sdai.cross_artifact import AnalysisReport
from sdai.diagnostics import DIAGNOSTICS_API_VERSION
from sdai.evals import EVAL_REPORT_API_VERSION
from sdai.execution_resume import ResumePlan
from sdai.integration_cli import (
    INTEGRATION_INFO_API_VERSION,
    INTEGRATION_LIFECYCLE_RESULT_API_VERSION,
    INTEGRATION_SEARCH_API_VERSION,
    INTEGRATION_STATUS_COMMAND_API_VERSION,
)
from sdai.json_contracts import (
    JSON_CONTRACT_CATALOG_API_VERSION,
    JSON_CONTRACT_STABILITY,
    StableJsonContract,
    StableJsonContractCatalog,
    stable_json_contract_catalog,
    stable_json_contract_catalog_json,
)
from sdai.multi_repo_feature_graph import MULTI_REPO_FEATURE_GRAPH_API_VERSION
from sdai.multi_repo_run import MULTI_REPO_RUN_PLAN_API_VERSION, MultiRepoExitClass
from sdai.multi_repo_verify import MultiRepoVerificationReport
from sdai.schema_cli import (
    ARTIFACT_SCHEMA_DEFINITION_API_VERSION,
    ARTIFACT_SCHEMA_GRAPH_API_VERSION,
)
from sdai.skill_cli import SKILL_RESOLUTION_API_VERSION
from sdai.spec_cli import (
    SPEC_DIFF_API_VERSION,
    SPEC_PROMOTION_APPROVAL_API_VERSION,
    SPEC_PROMOTION_PREVIEW_API_VERSION,
    SPEC_PROMOTION_RESULT_API_VERSION,
    SPEC_VALIDATION_API_VERSION,
)
from sdai.specification_store_lifecycle import (
    STORE_CONTEXT_API_VERSION,
    STORE_CREATE_RESULT_API_VERSION,
    STORE_DOCTOR_API_VERSION,
    STORE_LIST_API_VERSION,
    STORE_REGISTER_RESULT_API_VERSION,
)
from sdai.tech_cli import TECHNOLOGY_REPORT_API_VERSION
from sdai.trace_graph import TRACE_GRAPH_API_VERSION
from sdai.trace_policy import TRACE_POLICY_REPORT_API_VERSION
from sdai.verification import VERIFY_REPORT_API_VERSION
from sdai.workflow_cli import (
    WORKFLOW_LEGACY_DEFINITION_API_VERSION,
    WORKFLOW_VALIDATION_API_VERSION,
)
from sdai.workflow_graph import WORKFLOW_GRAPH_API_VERSION, WORKFLOW_RESOLUTION_API_VERSION
from sdai.workflow_machine import WORKFLOW_RESUME_API_VERSION, WORKFLOW_RUN_STATUS_API_VERSION


ZERO_SHA = "sha256:" + ("0" * 64)

EXPECTED_CONTRACTS = (
    ("analysis.report", "sdai.findings/v1"),
    ("architecture.drift", "sdai.architecture-drift/v1"),
    ("artifact.explain", "sdai.artifact-state/v1"),
    ("artifact.status", "sdai.artifact-state-report/v1"),
    ("audit.report", "sdai.audit-report/v1"),
    ("context.explain", "sdai.context-explain/v1"),
    ("contract.check", "sdai.contract-result/v1"),
    ("contract.diff", "sdai.contract-diff/v1"),
    ("contract.inspect", "sdai.contract-result/v1"),
    ("convergence.state", "sdai.convergence-state/v1"),
    ("diagnostics.report", "sdai.diagnostics/v1"),
    ("eval.report", "sdai.eval-report/v1"),
    ("execution.resume", "sdai.execution-resume-result/v1"),
    ("execution.status", "sdai.execution-resume-plan/v1"),
    ("integration.info", "sdai.integration-info/v1"),
    ("integration.lifecycle", "sdai.integration-lifecycle-result/v1"),
    ("integration.search", "sdai.integration-search/v1"),
    ("integration.status", "sdai.integration-status-command/v1"),
    ("multi-repo.feature-graph", "sdai.multi-repo-feature-graph/v1"),
    ("multi-repo.run-plan", "sdai.multi-repo-run-plan/v1"),
    ("multi-repo.verification", "sdai.multi-repo-verification/v1"),
    ("pack.certification", "sdai.pack-certification-decision/v1"),
    ("pack.info", "sdai.pack-info/v1"),
    ("pack.lifecycle", "sdai.pack-lifecycle-result/v1"),
    ("pack.outdated", "sdai.pack-outdated/v1"),
    ("pack.search", "sdai.pack-search/v1"),
    ("schema.definition", "sdai.artifact-schema-definition/v1"),
    ("schema.graph", "sdai.artifact-schema-graph/v1"),
    ("skill.resolution", "sdai.skill-resolution/v1"),
    ("spec.diff", "sdai.spec-diff/v1"),
    ("spec.promotion-approval", "sdai.spec-promotion-approval/v1"),
    ("spec.promotion-preview", "sdai.spec-promotion-preview/v1"),
    ("spec.promotion-result", "sdai.spec-promotion-result/v1"),
    ("spec.validation", "sdai.spec-validation/v1"),
    ("store.context", "sdai.specification-store-context/v1"),
    ("store.create", "sdai.specification-store-create-result/v1"),
    ("store.doctor", "sdai.specification-store-doctor/v1"),
    ("store.list", "sdai.specification-store-list/v1"),
    ("store.register", "sdai.specification-store-register-result/v1"),
    ("technology.report", "sdai.technology-report/v1"),
    ("trace.coverage", "sdai.trace-coverage/v1"),
    ("trace.export", "sdai.trace-graph/v1"),
    ("trace.missing", "sdai.trace-missing/v1"),
    ("trace.policy", "sdai.trace-policy-report/v1"),
    ("trace.requirement", "sdai.trace-requirement/v1"),
    ("trace.summary", "sdai.trace-summary/v1"),
    ("verify.report", "sdai.verify-report/v1"),
    ("workflow.explain-current", "sdai.workflow-resolution/v2"),
    ("workflow.explain-legacy", "sdai.workflow-definition/v1"),
    ("workflow.graph", "sdai.workflow-graph/v2"),
    ("workflow.resolution", "sdai.workflow-resolution/v2"),
    ("workflow.resume", "sdai.workflow-resume-result/v2"),
    ("workflow.status", "sdai.workflow-run-status/v2"),
    ("workflow.validation", "sdai.workflow-validation/v2"),
)


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def test_catalog_is_deterministic_versioned_sorted_and_self_hashed() -> None:
    first = stable_json_contract_catalog_json()
    second = stable_json_contract_catalog_json()
    assert first == second
    assert first.endswith("\n")

    payload = json.loads(first)
    assert payload["apiVersion"] == JSON_CONTRACT_CATALOG_API_VERSION == "sdai.json-contracts/v1"
    assert payload["stability"] == JSON_CONTRACT_STABILITY == "stable-1.0"
    assert payload["compatibility"] == {
        "line": "1.x",
        "additiveFields": "allowed",
        "removalRenameTypeOrMeaningChange": "requires-versioned-successor",
        "migrationGuidanceRequiredForBreakingChange": True,
        "jsonStdout": "machine-clean",
        "internalPersistenceSchemas": "not-implied-stable",
    }
    claimed = payload.pop("catalogSha256")
    assert claimed == "sha256:" + sha256(_canonical(payload)).hexdigest()
    assert stable_json_contract_catalog().sha256 == claimed


def test_exact_1x_automation_contract_floor_is_pinned() -> None:
    catalog = stable_json_contract_catalog()
    actual = tuple((item.contract_id, item.api_version) for item in catalog.contracts)
    assert actual == EXPECTED_CONTRACTS
    assert tuple(item.contract_id for item in catalog.contracts) == tuple(
        sorted(item.contract_id for item in catalog.contracts)
    )


def test_catalog_matches_subsystem_version_authorities() -> None:
    expected = {
        "architecture.drift": ARCHITECTURE_DRIFT_API_VERSION,
        "artifact.explain": ARTIFACT_STATE_EXPLAIN_API_VERSION,
        "artifact.status": ARTIFACT_STATE_REPORT_API_VERSION,
        "audit.report": AUDIT_REPORT_API_VERSION,
        "context.explain": CONTEXT_EXPLAIN_API_VERSION,
        "contract.check": CONTRACT_RESULT_API_VERSION,
        "contract.diff": CONTRACT_DIFF_API_VERSION,
        "contract.inspect": CONTRACT_RESULT_API_VERSION,
        "convergence.state": CONVERGENCE_STATE_API_VERSION,
        "diagnostics.report": DIAGNOSTICS_API_VERSION,
        "eval.report": EVAL_REPORT_API_VERSION,
        "integration.info": INTEGRATION_INFO_API_VERSION,
        "integration.lifecycle": INTEGRATION_LIFECYCLE_RESULT_API_VERSION,
        "integration.search": INTEGRATION_SEARCH_API_VERSION,
        "integration.status": INTEGRATION_STATUS_COMMAND_API_VERSION,
        "multi-repo.feature-graph": MULTI_REPO_FEATURE_GRAPH_API_VERSION,
        "multi-repo.run-plan": MULTI_REPO_RUN_PLAN_API_VERSION,
        "schema.definition": ARTIFACT_SCHEMA_DEFINITION_API_VERSION,
        "schema.graph": ARTIFACT_SCHEMA_GRAPH_API_VERSION,
        "skill.resolution": SKILL_RESOLUTION_API_VERSION,
        "spec.diff": SPEC_DIFF_API_VERSION,
        "spec.promotion-approval": SPEC_PROMOTION_APPROVAL_API_VERSION,
        "spec.promotion-preview": SPEC_PROMOTION_PREVIEW_API_VERSION,
        "spec.promotion-result": SPEC_PROMOTION_RESULT_API_VERSION,
        "spec.validation": SPEC_VALIDATION_API_VERSION,
        "store.context": STORE_CONTEXT_API_VERSION,
        "store.create": STORE_CREATE_RESULT_API_VERSION,
        "store.doctor": STORE_DOCTOR_API_VERSION,
        "store.list": STORE_LIST_API_VERSION,
        "store.register": STORE_REGISTER_RESULT_API_VERSION,
        "technology.report": TECHNOLOGY_REPORT_API_VERSION,
        "trace.export": TRACE_GRAPH_API_VERSION,
        "trace.policy": TRACE_POLICY_REPORT_API_VERSION,
        "verify.report": VERIFY_REPORT_API_VERSION,
        "workflow.explain-current": WORKFLOW_RESOLUTION_API_VERSION,
        "workflow.explain-legacy": WORKFLOW_LEGACY_DEFINITION_API_VERSION,
        "workflow.graph": WORKFLOW_GRAPH_API_VERSION,
        "workflow.resolution": WORKFLOW_RESOLUTION_API_VERSION,
        "workflow.resume": WORKFLOW_RESUME_API_VERSION,
        "workflow.status": WORKFLOW_RUN_STATUS_API_VERSION,
        "workflow.validation": WORKFLOW_VALIDATION_API_VERSION,
    }
    catalog = stable_json_contract_catalog()
    for contract_id, api_version in expected.items():
        entry = catalog.by_id(contract_id)
        assert entry is not None
        assert entry.api_version == api_version


def test_representative_runtime_serializers_keep_registered_api_versions() -> None:
    catalog = stable_json_contract_catalog()

    analysis = AnalysisReport(feature_id="JSON-100", index_sha256=ZERO_SHA, findings=())
    analysis_version = analysis.as_dict()["apiVersion"]
    assert analysis_version == catalog.by_id("analysis.report").api_version  # type: ignore[union-attr]

    plan = ResumePlan(
        run_id="run-1",
        feature_id="JSON-100",
        run_status="running",
        current_head="a" * 40,
        repository_clean=True,
        repository_status="",
        checkpoint_status="current",
        last_sequence=0,
        last_sha256=ZERO_SHA,
        task_order=(),
        tasks=(),
        resume_task_id=None,
        resume_action=None,
        blocked_reason=None,
        plan_sha256=ZERO_SHA,
    )
    assert plan.as_dict()["apiVersion"] == catalog.by_id("execution.status").api_version  # type: ignore[union-attr]

    verification = MultiRepoVerificationReport(
        feature_id="JSON-100",
        graph_sha256=ZERO_SHA,
        plan_sha256=ZERO_SHA,
        graph_findings_json="[]",
        repositories=(),
        exit_class=MultiRepoExitClass.SUCCESS,
    )
    assert verification.as_dict()["apiVersion"] == catalog.by_id("multi-repo.verification").api_version  # type: ignore[union-attr]


def test_contract_result_variants_keep_their_existing_kind_discriminators() -> None:
    catalog = stable_json_contract_catalog()
    inspect_entry = catalog.by_id("contract.inspect")
    check_entry = catalog.by_id("contract.check")
    diff_entry = catalog.by_id("contract.diff")
    assert inspect_entry is not None and check_entry is not None and diff_entry is not None
    assert (inspect_entry.discriminator_field, inspect_entry.discriminator_value) == (
        "kind",
        "ContractInspection",
    )
    assert (check_entry.discriminator_field, check_entry.discriminator_value) == (
        "kind",
        "ContractCheckResult",
    )
    assert (diff_entry.discriminator_field, diff_entry.discriminator_value) == (
        "kind",
        "ContractDiffResult",
    )


def test_catalog_validation_rejects_ambiguous_or_unversioned_entries() -> None:
    valid = StableJsonContract(
        "sample.status",
        "sdai.sample/v1",
        "sdai sample --json",
        "sdai.sample",
        "status",
    )
    try:
        StableJsonContractCatalog((valid, valid))
    except ValueError as exc:
        assert "unique" in str(exc)
    else:
        raise AssertionError("duplicate stable contract ids must fail closed")

    try:
        StableJsonContract(
            "sample.bad",
            "sdai.sample",
            "sdai sample --json",
            "sdai.sample",
            "read",
        )
    except ValueError as exc:
        assert "API version" in str(exc)
    else:
        raise AssertionError("unversioned automation contracts must fail closed")
