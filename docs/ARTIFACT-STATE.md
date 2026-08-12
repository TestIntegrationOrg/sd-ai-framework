# Artifact Freshness, Staleness, and Evidence Invalidation

SDAI 0.8 computes lifecycle freshness from the effective artifact DAG introduced by the artifact-schema registry. Freshness is deterministic framework state; an AI/provider does not decide whether an artifact or approval is current.

## State model

Every active artifact is reported as one of:

| State | Meaning |
|---|---|
| `fresh` | artifact content, effective definition, dependency bindings, dependency states, and bound evidence still match the recorded state |
| `stale` | artifact exists but its own hash/definition/dependency/evidence binding changed, or an upstream dependency is stale |
| `missing` | the artifact content itself does not exist |
| `blocked` | artifact content exists but one or more dependency artifacts are missing/blocked |

The engine evaluates artifacts in deterministic topological order. Staleness is therefore transitive:

```text
requirements changed
        ↓
architecture stale
        ↓
plan stale
        ↓
tasks stale
        ↓
tests / verification stale
```

An unrelated DAG branch remains fresh when none of its own content, dependencies, definitions, or evidence changed.

## Canonical hashes

Text artifact types are read through SDAI's strict UTF-8 boundary and normalized to LF before SHA-256 calculation. This makes CRLF/LF checkout differences equivalent.

Directory artifacts use length-prefixed canonical framing for every relative path/content pair before SHA-256 calculation. This prevents ambiguous concatenation layouts from producing the same preimage representation.

Each state record binds:

```yaml
version: 1
artifact_id: architecture
domain: null
artifact_path: specs/changes/SIGN-123/architecture.md
definition_sha256: sha256:...
artifact_sha256: sha256:...
dependency_sha256:
  requirements: sha256:...
evidence:
  - kind: approval
    id: architecture-approval
    source: specs/changes/SIGN-123/approvals/architecture.yaml
    source_sha256: sha256:...
```

Records live under the protected feature workspace:

```text
specs/changes/<FEATURE>/.sdai/artifact-state/<artifact-id>.yaml
```

Artifacts whose schema paths contain `{domain}` use a separate record key for each materialized domain:

```text
specs/changes/<FEATURE>/.sdai/artifact-state/<artifact-id>--<domain>.yaml
```

The record also stores the expected domain value. Recording `signing` can therefore never overwrite or satisfy the freshness record for `certificates`.

The effective artifact-definition hash covers lifecycle semantics such as path, type, required state, dependencies, risk applicability, and organization mandates. Changing schema semantics therefore invalidates old artifact evidence even if the artifact file bytes did not change.

## Evidence invalidation

State records may bind existing deterministic evidence files using these evidence categories:

- `approval`
- `validation`
- `verification`
- `evidence`

The state engine does not decide whether an approval or validation passed. The producer of that evidence must make the decision first. Once bound, SDAI invalidates that evidence when its source file is missing or its normalized SHA-256 changes.

Evidence sources must be portable repository-relative POSIX paths. Absolute paths, drive-letter paths, backslashes, parent/dot segments, empty segments, Windows-invalid filename characters, reserved DOS device names, and control characters fail closed both when a binding is created and when a persisted state record is parsed.

An evidence change stales the owning artifact and propagates staleness downstream through the DAG.

## Recording boundary

`record_artifact_state(...)` is a framework integration API for deterministic validators and approval engines. It deliberately has no generic CLI equivalent.

Before a downstream record can be written, all direct dependency artifacts must already be `fresh`. This prevents a caller from refreshing a plan/task/test baseline while its prerequisite requirement or architecture evidence is stale.

External workspace-writing AI providers cannot use this mechanism to approve themselves: state records live under `specs/**`, which is already a protected SDAI source-of-truth boundary. Future identity-backed approvals will strengthen who may produce approval evidence; freshness invalidation is independent of that identity mechanism.

## Read-only CLI

```bash
sdai artifact status SIGN-123
sdai artifact status SIGN-123 --risk critical --json
sdai artifact explain SIGN-123 architecture
sdai artifact explain SIGN-123 architecture --json
```

The CLI only evaluates existing content/evidence. It cannot mark an artifact fresh.

## Risk and domain resolution

`--risk` accepts:

```text
trivial | standard | critical | regulated
```

Artifacts whose `applies_to` includes the selected risk are active. Their dependency closure is also included even when a dependency has a narrower applicability declaration, because an explicit dependency edge remains a prerequisite of the active artifact.

Schemas using `{domain}` require `--domain <domain>` for state evaluation. Domain-scoped state records are isolated by domain as described above.

## Fail-closed behavior

State/evidence parsing rejects malformed or unsafe input with stable error families:

| Code | Meaning |
|---|---|
| `SDAI-STATE-001` | invalid risk/domain/path-materialization input |
| `SDAI-STATE-002` | malformed artifact-state record |
| `SDAI-STATE-003` | unsafe/unreadable artifact or evidence content |
| `SDAI-STATE-004` | invalid attempt to record/bind state evidence |

`dependency_sha256` must be an actual mapping even for dependency-free artifacts; falsy lists/strings/booleans are not silently coerced to an empty mapping.

Symlinked artifact/state/evidence files are rejected. State JSON is deterministic and provider/model independent.

## Relationship to later roadmap slices

This slice establishes freshness/invalidation semantics only. Cross-artifact semantic findings (`ORPHAN_REQUIREMENT`, `ARCHITECTURE_CONFLICT`, etc.) belong to the 0.9 analyze engine. Identity-backed approvals belong to the later enterprise identity milestone. Durable execution ledgers and completion evidence build on these hashes rather than replacing them.
