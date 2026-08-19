# Audit + Provenance (0.19)

SDAI 0.19 adds an identity-independent, tamper-evident provenance chain for agent execution, workflow execution, policy/configuration authority, trace evidence, inspection, and immutable export. The audit ledger is evidence about what SDAI executed and what inputs or artifacts were hash-bound; it is **not** an authorization database and it does not grant workflow authority.

## Authority model

Each feature has one canonical audit workspace. Current workspaces use `specs/changes/<FEATURE>/.sdai/audit/events.jsonl`; supported legacy workspaces under `specs/<FEATURE>` continue to work when there is no ambiguous duplicate current workspace. Every complete event is canonical JSON, sequence-ordered, SHA-256 identified, and chained to the previous event hash. Verification fails closed on a corrupt complete record, invalid hash chain, unsafe path, non-canonical record, or unsupported structure. A recoverable incomplete crash tail is handled by the ledger recovery rules; recovery never converts a corrupt complete record into a valid one.

The source ledger is authoritative. Reports, trace projections, export manifests, sink receipts, and relationship indexes are derived evidence. They do not rewrite or silently repair source ledger bytes.

## Provenance chain

Agent execution records a start event before provider creation/call and a terminal success/failure event after the provider boundary. Prompts, explicit context, provider output, and provider exception messages are hash-bound where required rather than persisted as raw audit content. If the start audit record cannot be persisted, the provider is not called. If terminal audit persistence fails, SDAI does not retry the provider merely to repair provenance.

Workflow execution records workflow/run/step provenance and binds the effective workflow, constitution, configuration, policy, state, produced artifacts, and related agent events. Deterministic actions and Workflow Engine 2 operations preserve their existing execution authority; audit instrumentation observes and binds those executions rather than becoming a second executor.

Trace construction verifies the audit chain first, projects the ledger and audit events into the canonical feature graph, and reuses existing typed evidence nodes where possible. Artifact mutation or deletion is reported through the existing trace/freshness diagnostics; audit provenance does not replace the typed-evidence freshness engine.

`sdai audit <FEATURE>` is the public read-only inspection surface. It verifies the ledger before reporting, supports deterministic filters, exposes bounded relationship gaps, and returns stable integrity/input exit semantics. `sdai audit export <FEATURE>` and the Python export API package the verified canonical JSONL into deterministic chunks and a hash-bound manifest.

The local filesystem sink is the reference immutable handoff implementation. Publication is staged, verified, atomic at the directory boundary, symlink-safe, and idempotent. Replaying the same export returns `already-present`; a mismatched/tampered existing export fails closed rather than being overwritten.

## Hash relationships

For a stable ledger state:

- ledger `head_sha256` identifies the final verified event;
- ledger `export_sha256` identifies the complete canonical JSONL byte stream;
- the audit report repeats those verified ledger identities and adds its own deterministic report digest;
- the trace ledger node carries the same head/export identities;
- the export manifest carries the same ledger head/export identities plus deterministic chunk descriptors;
- the sink receipt binds the sink ID, export ID, manifest hash, and chunk hashes.

Inspection, trace construction, package creation, and sink handoff must leave the source ledger bytes unchanged.

## Security and privacy boundaries

Audit/provenance is designed to record identities of artifacts and decisions without becoming a secret store. Raw prompt/context/output text, provider exception messages, credentials, sensitive workflow input values, and equivalent secret-bearing payloads must not appear in persisted ledger/report/trace/export/receipt surfaces except for explicitly permitted canonical hashes or non-secret classifications. File paths and identifiers are validated and bounded, project-relative paths cannot escape the workspace, and symlink components are rejected at persistence/export trust boundaries.

Local approval files remain local workflow assertions. **0.18 Identity-Backed Enterprise Approvals is held/deferred and is not implemented or required by 0.19.** An `approved_by` string or local approval artifact is provenance only; 0.19 must not label it `identityVerified`, enterprise-authorized, or otherwise imply external identity verification.

## Public extension points

The supported 0.19 extension boundary is deliberately narrow:

- `AuditLedger` for verified feature-scoped append/read/verify/export behavior;
- audit provenance value objects for bounded canonical events and bindings;
- `build_audit_report` / `sdai audit` for read-only inspection;
- canonical trace-building APIs for audit/typed-evidence linkage;
- `build_audit_export_package` for deterministic immutable packaging;
- `AuditExportSink` plus `AuditExportSinkRegistry` for sink adapters;
- `handoff_audit_export` for exactly-once-at-the-SDAI-boundary delivery semantics.

A custom sink must verify the supplied package, avoid mutating the source ledger, return a receipt bound to the same export/manifest/chunks, and must not cause SDAI to re-execute providers, plugins, deterministic actions, or workflows when delivery/persistence fails.

## Operator troubleshooting

Run `sdai audit <FEATURE> --json --path <project>` first. Integrity exit failures mean the source chain must be investigated; do not regenerate or overwrite it to make the report green. Relationship gaps identify missing/stale bound artifacts separately from ledger corruption. For export failures, verify the ledger first, then inspect the immutable sink directory for missing/tampered chunks or unsafe filesystem entries. An existing export with different bytes is a hard conflict and must not be overwritten.

For release validation, both PR-head and exact squash-merged `main` must pass the unfiltered Ubuntu/Windows × Python 3.11/3.12 matrix. Branch CI is not evidence for a different merged SHA. See `docs/releases/0.19-release-evidence.md` for the recorded release proof.
