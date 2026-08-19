from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import re


JSON_CONTRACT_CATALOG_API_VERSION = "sdai.json-contracts/v1"
JSON_CONTRACT_STABILITY = "stable-1.0"

_API_VERSION = re.compile(r"^sdai\.[a-z0-9][a-z0-9._-]*/v[1-9][0-9]*$")
_CONTRACT_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{1,127}$")
_OPERATION_CLASSES = frozenset({"analysis", "lifecycle", "read", "resume", "status"})


@dataclass(frozen=True, slots=True)
class StableJsonContract:
    """One automation-facing JSON compatibility surface frozen for SDAI 1.x."""

    contract_id: str
    api_version: str
    surface: str
    owner_module: str
    operation_class: str
    discriminator_field: str | None = None
    discriminator_value: str | None = None

    def __post_init__(self) -> None:
        if _CONTRACT_ID.fullmatch(self.contract_id) is None:
            raise ValueError(f"invalid stable JSON contract id: {self.contract_id!r}")
        if _API_VERSION.fullmatch(self.api_version) is None:
            raise ValueError(f"invalid SDAI API version: {self.api_version!r}")
        if not self.surface.strip():
            raise ValueError("stable JSON contract surface must be non-empty")
        if not self.owner_module.startswith("sdai."):
            raise ValueError("stable JSON contract owner_module must be an sdai module")
        if self.operation_class not in _OPERATION_CLASSES:
            raise ValueError(f"unsupported operation class: {self.operation_class!r}")
        if (self.discriminator_field is None) != (self.discriminator_value is None):
            raise ValueError("discriminator field and value must be supplied together")
        if self.discriminator_field is not None and not self.discriminator_field.strip():
            raise ValueError("discriminator field must be non-empty")
        if self.discriminator_value is not None and not self.discriminator_value.strip():
            raise ValueError("discriminator value must be non-empty")

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "id": self.contract_id,
            "apiVersion": self.api_version,
            "surface": self.surface,
            "ownerModule": self.owner_module,
            "operationClass": self.operation_class,
        }
        if self.discriminator_field is not None:
            result["discriminator"] = {
                "field": self.discriminator_field,
                "value": self.discriminator_value,
            }
        return result


def _contract(
    contract_id: str,
    api_version: str,
    surface: str,
    owner_module: str,
    operation_class: str,
    discriminator_field: str | None = None,
    discriminator_value: str | None = None,
) -> StableJsonContract:
    return StableJsonContract(
        contract_id,
        api_version,
        surface,
        owner_module,
        operation_class,
        discriminator_field,
        discriminator_value,
    )


# This tuple is deliberately explicit rather than discovered dynamically. It is the
# 1.0 compatibility floor. Subsystem constants/tests cross-check these values so an
# accidental output-version change cannot silently rewrite the catalog.
_STABLE_CONTRACTS = (
    _contract("analysis.report", "sdai.findings/v1", "sdai analyze FEATURE --json", "sdai.cross_artifact", "analysis"),
    _contract("architecture.drift", "sdai.architecture-drift/v1", "sdai architecture drift FEATURE --json", "sdai.architecture_drift", "analysis"),
    _contract("artifact.explain", "sdai.artifact-state/v1", "sdai artifact explain FEATURE ARTIFACT --json", "sdai.artifact_state_cli", "read"),
    _contract("artifact.status", "sdai.artifact-state-report/v1", "sdai artifact status FEATURE --json", "sdai.artifact_state_cli", "status"),
    _contract("audit.report", "sdai.audit-report/v1", "sdai audit FEATURE --json", "sdai.audit_report", "read"),
    _contract("context.explain", "sdai.context-explain/v1", "sdai context explain FEATURE --json", "sdai.context_explain", "read"),
    _contract("contract.check", "sdai.contract-result/v1", "sdai contract check SOURCE --json", "sdai.contracts", "analysis", "kind", "ContractCheckResult"),
    _contract("contract.diff", "sdai.contract-diff/v1", "sdai contract diff SOURCE --against PATH --json", "sdai.contracts", "analysis", "kind", "ContractDiffResult"),
    _contract("contract.inspect", "sdai.contract-result/v1", "sdai contract inspect --json", "sdai.contracts", "read", "kind", "ContractInspection"),
    _contract("convergence.state", "sdai.convergence-state/v1", "sdai converge FEATURE --json", "sdai.convergence", "lifecycle"),
    _contract("diagnostics.report", "sdai.diagnostics/v1", "sdai diagnostics FEATURE --json", "sdai.diagnostics", "status"),
    _contract("eval.report", "sdai.eval-report/v1", "sdai skill|agent eval NAME --json", "sdai.evals", "analysis"),
    _contract("execution.resume", "sdai.execution-resume-result/v1", "sdai execution resume FEATURE --run RUN --json", "sdai.execution_resume", "resume"),
    _contract("execution.status", "sdai.execution-resume-plan/v1", "sdai execution status FEATURE --run RUN --json", "sdai.execution_resume", "status"),
    _contract("integration.info", "sdai.integration-info/v1", "sdai integration info ID --json", "sdai.integration_cli", "read"),
    _contract("integration.lifecycle", "sdai.integration-lifecycle-result/v1", "sdai integration install|repair|upgrade|remove ID --json", "sdai.integration_cli", "lifecycle"),
    _contract("integration.search", "sdai.integration-search/v1", "sdai integration search --json", "sdai.integration_cli", "read"),
    _contract("integration.status", "sdai.integration-status-command/v1", "sdai integration status ID --json", "sdai.integration_cli", "status"),
    _contract("migration.apply", "sdai.migration-result/v1", "sdai migrate apply --json", "sdai.migration", "lifecycle"),
    _contract("migration.plan", "sdai.migration-plan/v1", "sdai migrate plan --json", "sdai.migration", "read"),
    _contract("migration.rollback", "sdai.migration-rollback/v1", "sdai migrate rollback MIGRATION_ID --json", "sdai.migration", "lifecycle"),
    _contract("multi-repo.feature-graph", "sdai.multi-repo-feature-graph/v1", "sdai feature graph FEATURE --json", "sdai.multi_repo_feature_graph", "read"),
    _contract("multi-repo.run-plan", "sdai.multi-repo-run-plan/v1", "sdai run FEATURE --all --plan --json", "sdai.multi_repo_run", "read"),
    _contract("multi-repo.verification", "sdai.multi-repo-verification/v1", "sdai verify --all-repos --feature FEATURE --json", "sdai.multi_repo_verify", "analysis"),
    _contract("pack.certification", "sdai.pack-certification-decision/v1", "sdai pack certification --json", "sdai.pack_cli", "analysis"),
    _contract("pack.info", "sdai.pack-info/v1", "sdai pack info COORDINATE --json", "sdai.pack_cli", "read"),
    _contract("pack.lifecycle", "sdai.pack-lifecycle-result/v1", "sdai pack install|update|remove ... --json", "sdai.pack_cli", "lifecycle"),
    _contract("pack.outdated", "sdai.pack-outdated/v1", "sdai pack outdated --json", "sdai.pack_cli", "status"),
    _contract("pack.search", "sdai.pack-search/v1", "sdai pack search QUERY --json", "sdai.pack_cli", "read"),
    _contract("schema.definition", "sdai.artifact-schema-definition/v1", "sdai schema show ARTIFACT --json", "sdai.schema_cli", "read"),
    _contract("schema.graph", "sdai.artifact-schema-graph/v1", "sdai schema list|validate|graph --json", "sdai.schema_cli", "read"),
    _contract("skill.resolution", "sdai.skill-resolution/v1", "sdai skill resolve ... --json", "sdai.skill_cli", "analysis"),
    _contract("spec.diff", "sdai.spec-diff/v1", "sdai spec diff FEATURE --json", "sdai.spec_cli", "analysis"),
    _contract("spec.promotion-approval", "sdai.spec-promotion-approval/v1", "sdai spec approve FEATURE --by ACTOR --json", "sdai.spec_cli", "lifecycle"),
    _contract("spec.promotion-preview", "sdai.spec-promotion-preview/v1", "sdai spec promote FEATURE --dry-run --json", "sdai.spec_cli", "read"),
    _contract("spec.promotion-result", "sdai.spec-promotion-result/v1", "sdai spec promote FEATURE --json", "sdai.spec_cli", "lifecycle"),
    _contract("spec.validation", "sdai.spec-validation/v1", "sdai spec validate FEATURE --json", "sdai.spec_cli", "analysis"),
    _contract("store.context", "sdai.specification-store-context/v1", "sdai store context --json", "sdai.specification_store_lifecycle", "read"),
    _contract("store.create", "sdai.specification-store-create-result/v1", "sdai store create ... --json", "sdai.specification_store_lifecycle", "lifecycle"),
    _contract("store.doctor", "sdai.specification-store-doctor/v1", "sdai store doctor --json", "sdai.specification_store_lifecycle", "status"),
    _contract("store.list", "sdai.specification-store-list/v1", "sdai store list --json", "sdai.specification_store_lifecycle", "read"),
    _contract("store.register", "sdai.specification-store-register-result/v1", "sdai store register PATH --json", "sdai.specification_store_lifecycle", "lifecycle"),
    _contract("technology.report", "sdai.technology-report/v1", "sdai tech detect --json", "sdai.tech_cli", "analysis"),
    _contract("trace.coverage", "sdai.trace-coverage/v1", "sdai trace coverage FEATURE --json", "sdai.trace_cli", "analysis"),
    _contract("trace.export", "sdai.trace-graph/v1", "sdai trace export FEATURE --format json", "sdai.trace_graph", "read"),
    _contract("trace.missing", "sdai.trace-missing/v1", "sdai trace missing FEATURE --json", "sdai.trace_cli", "analysis"),
    _contract("trace.policy", "sdai.trace-policy-report/v1", "sdai trace policy FEATURE --json", "sdai.trace_policy", "analysis"),
    _contract("trace.requirement", "sdai.trace-requirement/v1", "sdai trace requirement FEATURE REQUIREMENT --json", "sdai.trace_cli", "read"),
    _contract("trace.summary", "sdai.trace-summary/v1", "sdai trace FEATURE --json", "sdai.trace_cli", "read"),
    _contract("verify.report", "sdai.verify-report/v1", "sdai verify FEATURE --json", "sdai.verification", "analysis"),
    _contract("workflow.explain-current", "sdai.workflow-resolution/v2", "sdai workflow explain NAME --json (workflowVersion >= 9)", "sdai.workflow_graph", "read"),
    _contract("workflow.explain-legacy", "sdai.workflow-definition/v1", "sdai workflow explain NAME --json (workflowVersion < 9)", "sdai.workflow_cli", "read"),
    _contract("workflow.graph", "sdai.workflow-graph/v2", "sdai workflow graph NAME --json", "sdai.workflow_graph", "read"),
    _contract("workflow.resolution", "sdai.workflow-resolution/v2", "sdai workflow resolve NAME --json", "sdai.workflow_graph", "read"),
    _contract("workflow.resume", "sdai.workflow-resume-result/v2", "sdai workflow resume FEATURE --run RUN --json", "sdai.workflow_machine", "resume"),
    _contract("workflow.status", "sdai.workflow-run-status/v2", "sdai workflow status FEATURE --run RUN --json", "sdai.workflow_machine", "status"),
    _contract("workflow.validation", "sdai.workflow-validation/v2", "sdai workflow validate NAME --json", "sdai.workflow_cli", "analysis"),
)


@dataclass(frozen=True, slots=True)
class StableJsonContractCatalog:
    contracts: tuple[StableJsonContract, ...]

    def __post_init__(self) -> None:
        ids = [item.contract_id for item in self.contracts]
        if ids != sorted(ids):
            raise ValueError("stable JSON contracts must be sorted by id")
        if len(ids) != len(set(ids)):
            raise ValueError("stable JSON contract ids must be unique")

    def _body(self) -> dict[str, object]:
        return {
            "apiVersion": JSON_CONTRACT_CATALOG_API_VERSION,
            "stability": JSON_CONTRACT_STABILITY,
            "compatibility": {
                "line": "1.x",
                "additiveFields": "allowed",
                "removalRenameTypeOrMeaningChange": "requires-versioned-successor",
                "migrationGuidanceRequiredForBreakingChange": True,
                "jsonStdout": "machine-clean",
                "internalPersistenceSchemas": "not-implied-stable",
            },
            "contracts": [item.as_dict() for item in self.contracts],
        }

    @property
    def sha256(self) -> str:
        return "sha256:" + sha256(_canonical_bytes(self._body())).hexdigest()

    def as_dict(self) -> dict[str, object]:
        result = self._body()
        result["catalogSha256"] = self.sha256
        return result

    def to_json(self) -> str:
        return _canonical_bytes(self.as_dict()).decode("utf-8") + "\n"

    def by_id(self, contract_id: str) -> StableJsonContract | None:
        return next((item for item in self.contracts if item.contract_id == contract_id), None)


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def stable_json_contract_catalog() -> StableJsonContractCatalog:
    """Return the deterministic 1.0 automation-facing JSON compatibility catalog."""
    return StableJsonContractCatalog(_STABLE_CONTRACTS)


def stable_json_contract_catalog_json() -> str:
    """Return canonical newline-terminated JSON for CI/tooling introspection."""
    return stable_json_contract_catalog().to_json()


__all__ = [
    "JSON_CONTRACT_CATALOG_API_VERSION",
    "JSON_CONTRACT_STABILITY",
    "StableJsonContract",
    "StableJsonContractCatalog",
    "stable_json_contract_catalog",
    "stable_json_contract_catalog_json",
]
