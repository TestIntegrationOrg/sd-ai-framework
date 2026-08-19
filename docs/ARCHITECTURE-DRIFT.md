# Architecture Drift

SD-AI 0.17 Architecture Drift compares an approved, hash-bound architecture model with deterministic repository reality and applies monotonic delivery policy. The SDAI engine remains authoritative: repository observers produce facts, the canonical comparator/security derivation produces findings, typed evidence proves approval, and policy decides whether delivery is blocked. AI/provider output is never approval authority.

## Approved topology

The current feature workspace stores approved architecture at:

`specs/changes/<FEATURE>/architecture/approved-topology.yaml`

Legacy `specs/<FEATURE>` workspaces remain supported by the existing feature workspace resolver. The topology uses:

- `apiVersion: sdai.architecture-topology/v1`
- `kind: ApprovedArchitecture`
- `metadata.id`: stable topology id
- `metadata.feature`: feature id
- `metadata.approvalEvidence`: repository-relative typed evidence path
- `spec.components`: component ids, repository roots, and optional module prefixes
- `spec.facts`: approved architecture facts

Each fact has a stable `id`, `kind`, `mode`, `source`, `target`, and JSON-safe `attributes`. Supported fact kinds are:

- `dependency`
- `communication`
- `data-ownership`
- `data-access`
- `trust-boundary`
- `deployment`
- `contract`

Fact modes are `required`, `allowed`, and `forbidden`. Required facts must exist in repository observations; forbidden facts must not exist; undeclared observed facts remain explicit drift rather than being silently accepted.

Topology/component/fact ordering and semantic hashes are deterministic. Unsafe paths, duplicate identities, unsupported fact kinds, ambiguous component ownership, and malformed topology fail closed.

## Approval evidence

An approved topology is not authoritative merely because the YAML file exists. `metadata.approvalEvidence` must point to current typed evidence that:

- is an `approval` evidence record with status `passed`;
- binds the exact topology source/hash;
- names the exact topology subject and topology SHA-256 in `result.architectureApproval`;
- is current under the existing trace-evidence freshness rules; and
- is produced by an independent approval identity, not an AI/provider self-approval.

Stale or mismatched architecture approval is represented as a policy blocker. It cannot silently satisfy governance or authorize drift.

## Deterministic repository observers

The default 0.17 engine runs one provider-independent observer registry. All observation is bounded and repository-local; it does not execute builds, start services, contact networks, connect to databases, run deployment tooling, or query cloud control planes.

### Dependency observation

`repository-dependencies` resolves common language/module imports to approved component ownership using deterministic repository roots/module prefixes. Supported repository analysis includes Python, Java/Kotlin, .NET/F#, JavaScript/TypeScript, Go, and PowerShell declaration forms. Dynamic/ambiguous internal ownership fails closed where it could conceal coupling.

### Communication and contract observation

`repository-communications` observes literal inbound HTTP endpoints, outbound HTTP calls, event publish declarations, and approved contract bindings. Internal host/channel aliases are resolved to approved components; explicit external destinations remain external identities. OpenAPI/contract facts reuse the 0.16 canonical contract source/symbol hashes rather than reparsing contract truth independently.

### Data ownership and access observation

`repository-data` observes bounded SQL ownership/read/write/admin operations, common ORM entity/table mappings, and datasource/store configuration. Connection identities are normalized without emitting passwords, tokens, userinfo, query secrets, or raw connection strings. The observer never connects to a database or executes migrations/ORM tooling.

### Deployment observation

`repository-deployments` consumes explicitly declared `.sdai/deployments.yaml` sources using `sdai.deployment-sources/v1`. Supported declarative sources are Kubernetes YAML, Docker Compose, and bounded supported Terraform constructs. It never invokes `kubectl`, Docker, Terraform, providers/plugins, cloud CLIs, or network discovery. Secret values are not emitted into facts.

### Trust-boundary/security derivation

`trust-boundary-security` is derived from approved zone memberships/boundary rules plus the communication, data-access, and deployment observations above. It detects forbidden/unapproved crossings, missing zone ownership, direction/exposure changes, gateway bypass, missing controls, sensitive data movement, and missing required boundaries. The same canonical `ArchitectureDriftReport` carries both ordinary and security-topology findings.

## CLI

The stable governed CLI is:

```text
sdai architecture drift FEATURE [--json] [--path PATH]
```

Exit codes are intentionally CI-friendly:

- `0`: evaluated successfully and effective policy has no blocker;
- `2`: evaluated successfully and effective architecture policy blocks delivery;
- `1`: malformed, unsafe, ambiguous, or otherwise unevaluable input.

`--json` emits a canonical `sdai.architecture-drift-evaluation/v1` object containing the effective policy, policy decision, canonical drift report (when available), governance status, and stable SHA-256 identity.

Legacy `sdai architecture FEATURE ...` lifecycle/artifact validation remains owned by the original lifecycle parser. Only the nested `architecture drift` surface is intercepted by the versioned entrypoint.

## Stable JSON/API surfaces

0.17 composes existing versioned surfaces rather than replacing them:

- `sdai.architecture-topology/v1`
- `sdai.architecture-observation/v1`
- `sdai.architecture-drift/v1`
- `sdai.architecture-drift-policy/v1`
- `sdai.architecture-drift-policy-decision/v1`
- `sdai.architecture-drift-evaluation/v1`
- existing `sdai.trace-graph/v1` for trace projection
- existing verification report contract for `sdai verify`

Architecture topology/components/facts and current drift findings are projected into the existing TraceGraph with explicit `architecture_role` metadata. Typed architecture approval is linked with the existing evidence relation rather than a separate approval graph.

`sdai verify FEATURE` projects architecture drift into the existing verification model. Trust-boundary drift maps to security, contract drift to contract verification, and dependency/data/deployment drift to architecture-intent verification. Policy-blocking drift is blocking; non-blocking drift remains visible as warning-level deterministic evidence.

## Monotonic policy

Repository policy lives at:

`.sdai/architecture-drift-policy.yaml`

It uses `apiVersion: sdai.architecture-drift-policy/v1` and `kind: ArchitectureDriftPolicy`.

External organization/user policy may be supplied through:

- `SDAI_ORG_ARCHITECTURE_DRIFT_POLICY_PATH`
- `SDAI_USER_ARCHITECTURE_DRIFT_POLICY_PATH`

Precedence is core -> organization -> repository -> user, but precedence cannot weaken authority. Effective governance is monotonic:

- if any layer requires architecture governance, lower layers cannot turn it off;
- `warning` is a stricter blocking threshold than `error`;
- category-specific thresholds can only make the effective threshold stricter.

The core default is backward-compatible: topology governance is not required and only `error` drift blocks delivery. A repository/organization may strengthen this to require topology and/or block warning-level drift.

Policy files are bounded, duplicate-key safe, and path-safe. Repository policy symlinks and external policy leaf symlinks fail closed; organization policy must be managed outside the repository workspace.

## Error classes

Stable fail-closed families include:

- `SDAI-ARCH-DRIFT-*`: topology/evidence/observation/comparator validation
- `SDAI-ARCH-REPOSITORY-*`: repository ownership and safe-source access
- `SDAI-ARCH-COMM-*`: communication/contract observation
- `SDAI-ARCH-DATA-*`: data ownership/access observation
- `SDAI-ARCH-SECURITY-*`: trust-zone/boundary derivation and ambiguity
- `SDAI-ARCH-DEPLOY-*`: deployment manifest/source observation
- `SDAI-ARCH-POLICY-*`: governance policy parsing/path/merge evaluation
- `SDAI-ARCH-ENGINE-*`: governed evaluation serialization/safety

Malformed or ambiguous inputs never silently downgrade to “no drift.”

## Backward compatibility and migration

Projects without approved topology remain backward compatible: **projects without approved topology** receive no new architecture blocker unless effective policy explicitly requires architecture governance. Existing feature trace/verify behavior remains available, and the legacy feature workspace layout continues to resolve.

A recommended adoption sequence is:

1. add stable component ids, roots, and module prefixes;
2. add required/allowed/forbidden facts for the material architecture boundaries you want governed;
3. generate independent, hash-bound typed architecture approval evidence;
4. run `sdai architecture drift FEATURE --json` and address unsafe/ambiguous observations;
5. start with the core `error` threshold, then explicitly tighten organization/repository policy when warning-level drift should also block delivery;
6. use `sdai trace export`, `sdai trace ...`, and `sdai verify` to audit topology, evidence, and findings through existing authority surfaces.

## Intentional limitations

0.17 observes deterministic version-controlled repository truth, not live runtime truth. It does not discover undocumented runtime topology, execute arbitrary source/build code, inspect live databases, call cloud/Kubernetes/Docker/Terraform APIs, infer dynamic destinations from execution, or allow provider output to certify compliance. Unsupported/dynamic cases remain explicit or fail closed when guessing could hide a policy-relevant internal edge.
