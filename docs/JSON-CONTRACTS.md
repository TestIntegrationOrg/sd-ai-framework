# SDAI 1.0 JSON automation contracts

SDAI 1.0 treats its major machine-facing read, analysis, status, resume, and integration-lifecycle JSON outputs as compatibility contracts. The authoritative machine-inspectable inventory is exposed by `sdai.json_contracts.stable_json_contract_catalog()` and canonical JSON by `stable_json_contract_catalog_json()`.

The catalog API is `sdai.json-contracts/v1` and its stability marker is `stable-1.0`. It records the existing API version for each surface; it does not renumber established contracts. In particular, Workflow Engine 2 already uses `v2` contracts and those identifiers remain `v2` at the 1.0 boundary.

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

| Catalog ID | Existing API version | Surface |
|---|---|---|
| `analysis.report` | `sdai.findings/v1` | `sdai analyze FEATURE --json` |
| `architecture.drift` | `sdai.architecture-drift/v1` | `sdai architecture drift FEATURE --json` |
| `audit.report` | `sdai.audit-report/v1` | `sdai audit FEATURE --json` |
| `context.explain` | `sdai.context-explain/v1` | `sdai context explain FEATURE --json` |
| `contract.check` | `sdai.contract-result/v1` | `sdai contract check SOURCE --json` |
| `contract.diff` | `sdai.contract-diff/v1` | `sdai contract diff BEFORE AFTER --json` |
| `contract.inspect` | `sdai.contract-result/v1` | `sdai contract inspect --json` |
| `diagnostics.report` | `sdai.diagnostics/v1` | `sdai diagnostics FEATURE --json` |
| `execution.resume` | `sdai.execution-resume-result/v1` | `sdai execution resume FEATURE --run RUN --json` |
| `execution.status` | `sdai.execution-resume-plan/v1` | `sdai execution status FEATURE --run RUN --json` |
| `integration.info` | `sdai.integration-info/v1` | `sdai integration info ID --json` |
| `integration.lifecycle` | `sdai.integration-lifecycle-result/v1` | install/repair/upgrade/remove JSON results |
| `integration.search` | `sdai.integration-search/v1` | `sdai integration search --json` |
| `integration.status` | `sdai.integration-status-command/v1` | `sdai integration status ID --json` |
| `multi-repo.feature-graph` | `sdai.multi-repo-feature-graph/v1` | `sdai feature-graph FEATURE --json` |
| `multi-repo.run-plan` | `sdai.multi-repo-run-plan/v1` | `sdai multi-repo run FEATURE --json` |
| `multi-repo.verification` | `sdai.multi-repo-verification/v1` | `sdai multi-repo verify FEATURE --json` |
| `trace.coverage` | `sdai.trace-coverage/v1` | `sdai trace coverage FEATURE --json` |
| `trace.export` | `sdai.trace-graph/v1` | `sdai trace export FEATURE --format json` |
| `trace.missing` | `sdai.trace-missing/v1` | `sdai trace missing FEATURE --json` |
| `trace.requirement` | `sdai.trace-requirement/v1` | `sdai trace requirement FEATURE REQUIREMENT --json` |
| `trace.summary` | `sdai.trace-summary/v1` | `sdai trace FEATURE --json` |
| `verify.report` | `sdai.verify-report/v1` | `sdai verify FEATURE --json` |
| `workflow.graph` | `sdai.workflow-graph/v2` | `sdai workflow graph NAME --json` |
| `workflow.resolution` | `sdai.workflow-resolution/v2` | `sdai workflow resolve NAME --json` |
| `workflow.resume` | `sdai.workflow-resume-result/v2` | `sdai workflow resume FEATURE --run RUN --json` |
| `workflow.status` | `sdai.workflow-run-status/v2` | `sdai workflow status FEATURE --run RUN --json` |
| `workflow.validation` | `sdai.workflow-validation/v2` | `sdai workflow validate NAME --json` |

`contract.check` and `contract.inspect` intentionally share `sdai.contract-result/v1`; their existing `kind` discriminator (`ContractCheckResult` or `ContractInspection`) distinguishes the payload variants. `contract.diff` has its established independent `sdai.contract-diff/v1` identity.

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

This stability declaration is identity-independent and does not add or imply the held 0.18/#25 enterprise identity/approval capability. GitHub Enterprise/OIDC/SSO identity verification, approver signatures/timestamps, and identity-backed authorization remain outside this work until explicitly resumed.
