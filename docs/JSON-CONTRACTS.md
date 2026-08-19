# SDAI 1.0 JSON automation contracts

SDAI 1.0 treats its core machine-facing read, analysis, status, resume, and lifecycle JSON outputs as compatibility contracts. The authoritative machine-inspectable inventory is exposed by `sdai.json_contracts.stable_json_contract_catalog()` and canonical JSON by `stable_json_contract_catalog_json()`.

The catalog API is `sdai.json-contracts/v1` and its stability marker is `stable-1.0`. The 1.0 floor currently contains 54 explicit automation contracts. It preserves established subsystem identities instead of renumbering them: Workflow Engine 2 remains on its existing `v2` contracts, while older surfaces promoted into the stable boundary receive additive `sdai.* /v1` identities without removing their legacy `version: 1` fields.

## 1.x compatibility rule

For a contract listed in the stable catalog during the 1.x line:

- existing fields keep their names, JSON types, and meaning;
- existing discriminators and version identifiers keep their meaning;
- additive fields are allowed when existing consumers can safely ignore them;
- removing or renaming a field, changing its type or meaning, or changing discriminator semantics requires an explicitly versioned successor contract and migration guidance;
- `--json` stdout is machine output and must remain free of human prose, progress banners, and unrelated diagnostics;
- subsystem-specific canonical hashes remain owned by their existing subsystem; the 1.0 catalog does not replace or recompute those authorities;
- inclusion in this catalog does not imply that internal checkpoints, caches, temporary diagnostics, or other non-automation persistence files are public stable schemas.

A future additive automation surface may be added to the catalog without changing existing entries. A breaking change to an existing entry must not silently edit its API version in place.

## Stable 1.0 inventory

| Catalog ID | API version | Surface |
|---|---|---|
| `analysis.report` | `sdai.findings/v1` | `sdai analyze FEATURE --json` |
| `architecture.drift` | `sdai.architecture-drift/v1` | `sdai architecture drift FEATURE --json` |
| `artifact.explain` | `sdai.artifact-state/v1` | `sdai artifact explain FEATURE ARTIFACT --json` |
| `artifact.status` | `sdai.artifact-state-report/v1` | `sdai artifact status FEATURE --json` |
| `audit.report` | `sdai.audit-report/v1` | `sdai audit FEATURE --json` |
| `context.explain` | `sdai.context-explain/v1` | `sdai context explain FEATURE --json` |
| `contract.check` | `sdai.contract-result/v1` | `sdai contract check SOURCE --json` |
| `contract.diff` | `sdai.contract-diff/v1` | `sdai contract diff SOURCE --against PATH --json` |
| `contract.inspect` | `sdai.contract-result/v1` | `sdai contract inspect --json` |
| `convergence.state` | `sdai.convergence-state/v1` | `sdai converge FEATURE --json` |
| `diagnostics.report` | `sdai.diagnostics/v1` | `sdai diagnostics FEATURE --json` |
| `eval.report` | `sdai.eval-report/v1` | `sdai skill eval NAME --json` / `sdai agent eval NAME --json` |
| `execution.resume` | `sdai.execution-resume-result/v1` | `sdai execution resume FEATURE --run RUN --json` |
| `execution.status` | `sdai.execution-resume-plan/v1` | `sdai execution status FEATURE --run RUN --json` |
| `integration.info` | `sdai.integration-info/v1` | `sdai integration info ID --json` |
| `integration.lifecycle` | `sdai.integration-lifecycle-result/v1` | install/repair/upgrade/remove JSON results |
| `integration.search` | `sdai.integration-search/v1` | `sdai integration search --json` |
| `integration.status` | `sdai.integration-status-command/v1` | `sdai integration status ID --json` |
| `multi-repo.feature-graph` | `sdai.multi-repo-feature-graph/v1` | `sdai feature graph FEATURE --json` |
| `multi-repo.run-plan` | `sdai.multi-repo-run-plan/v1` | `sdai run FEATURE --all --plan --json` |
| `multi-repo.verification` | `sdai.multi-repo-verification/v1` | `sdai verify --all-repos --feature FEATURE --json` |
| `pack.certification` | `sdai.pack-certification-decision/v1` | `sdai pack certification ... --json` |
| `pack.info` | `sdai.pack-info/v1` | `sdai pack info COORDINATE --json` |
| `pack.lifecycle` | `sdai.pack-lifecycle-result/v1` | install/update/remove Pack JSON results |
| `pack.outdated` | `sdai.pack-outdated/v1` | `sdai pack outdated --json` |
| `pack.search` | `sdai.pack-search/v1` | `sdai pack search QUERY --json` |
| `schema.definition` | `sdai.artifact-schema-definition/v1` | `sdai schema show ARTIFACT --json` |
| `schema.graph` | `sdai.artifact-schema-graph/v1` | schema list/validate/graph JSON |
| `skill.resolution` | `sdai.skill-resolution/v1` | `sdai skill resolve ... --json` |
| `spec.diff` | `sdai.spec-diff/v1` | `sdai spec diff FEATURE --json` |
| `spec.promotion-approval` | `sdai.spec-promotion-approval/v1` | `sdai spec approve FEATURE --by ACTOR --json` |
| `spec.promotion-preview` | `sdai.spec-promotion-preview/v1` | `sdai spec promote FEATURE --dry-run --json` |
| `spec.promotion-result` | `sdai.spec-promotion-result/v1` | `sdai spec promote FEATURE --json` |
| `spec.validation` | `sdai.spec-validation/v1` | `sdai spec validate FEATURE --json` |
| `store.context` | `sdai.specification-store-context/v1` | `sdai store context --json` |
| `store.create` | `sdai.specification-store-create-result/v1` | `sdai store create ... --json` |
| `store.doctor` | `sdai.specification-store-doctor/v1` | `sdai store doctor --json` |
| `store.list` | `sdai.specification-store-list/v1` | `sdai store list --json` |
| `store.register` | `sdai.specification-store-register-result/v1` | `sdai store register PATH --json` |
| `technology.report` | `sdai.technology-report/v1` | `sdai tech detect --json` |
| `trace.coverage` | `sdai.trace-coverage/v1` | `sdai trace coverage FEATURE --json` |
| `trace.export` | `sdai.trace-graph/v1` | `sdai trace export FEATURE --format json` |
| `trace.missing` | `sdai.trace-missing/v1` | `sdai trace missing FEATURE --json` |
| `trace.policy` | `sdai.trace-policy-report/v1` | `sdai trace policy FEATURE --json` |
| `trace.requirement` | `sdai.trace-requirement/v1` | `sdai trace requirement FEATURE REQUIREMENT --json` |
| `trace.summary` | `sdai.trace-summary/v1` | `sdai trace FEATURE --json` |
| `verify.report` | `sdai.verify-report/v1` | `sdai verify FEATURE --json` |
| `workflow.explain-current` | `sdai.workflow-resolution/v2` | `sdai workflow explain NAME --json` for Workflow Engine 2 definitions |
| `workflow.explain-legacy` | `sdai.workflow-definition/v1` | `sdai workflow explain NAME --json` for pre-v9 definitions |
| `workflow.graph` | `sdai.workflow-graph/v2` | `sdai workflow graph NAME --json` |
| `workflow.resolution` | `sdai.workflow-resolution/v2` | `sdai workflow resolve NAME --json` |
| `workflow.resume` | `sdai.workflow-resume-result/v2` | `sdai workflow resume FEATURE --run RUN --json` |
| `workflow.status` | `sdai.workflow-run-status/v2` | `sdai workflow status FEATURE --run RUN --json` |
| `workflow.validation` | `sdai.workflow-validation/v2` | `sdai workflow validate NAME --json` |

`contract.check` and `contract.inspect` intentionally share `sdai.contract-result/v1`; their existing `kind` discriminator (`ContractCheckResult` or `ContractInspection`) distinguishes the payload variants. `contract.diff` has its established independent `sdai.contract-diff/v1` identity.

`workflow explain` is intentionally dual-versioned. Pre-v9 workflow definitions retain their historical payload fields and now add `sdai.workflow-definition/v1`; Workflow Engine 2 definitions continue to emit the existing `sdai.workflow-resolution/v2` contract. This is a compatibility boundary, not a forced migration of old workflow definitions.

The artifact schema/state, technology, skill-resolution, specification, and other older serializers that historically exposed a generic `version: 1` keep that field. The explicit `apiVersion` is additive and gives automation a namespaced contract identity without reinterpreting the legacy marker.

## Tooling example

```python
from sdai.json_contracts import stable_json_contract_catalog

catalog = stable_json_contract_catalog()
entry = catalog.by_id("diagnostics.report")
assert entry is not None
print(entry.api_version)  # sdai.diagnostics/v1
print(catalog.sha256)
```

For byte-level comparison or CI inventory checks, use `stable_json_contract_catalog_json()`. It returns canonical UTF-8 JSON with sorted keys, finite JSON values, deterministic contract ordering, a self-hash, and exactly one trailing newline.

## Scope boundary

This stability declaration is identity-independent and does not add or imply the held 0.18/#25 enterprise identity/approval capability. Existing approval payload fields remain evidence only; this work does not add GitHub Enterprise/OIDC/SSO identity verification, approver signatures/timestamps, distinct-approver identity enforcement, or identity-backed authorization. Those capabilities remain outside this release work until explicitly resumed.
