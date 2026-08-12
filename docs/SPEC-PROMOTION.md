# Deterministic Current-Spec Promotion

Issue #57 is the first SDAI 0.7 slice allowed to update canonical `specs/current` truth. It consumes the typed parser from #55 and the read-only validator from #56; it does not ask an AI/provider to decide whether a change is valid or approved.

## Commands

```bash
sdai spec validate SIGN-123
sdai spec validate SIGN-123 --json

sdai spec diff SIGN-123
sdai spec diff SIGN-123 --json
sdai spec diff SIGN-123 --json --include-content

sdai spec approve SIGN-123 \
  --by architect@example.com \
  --role architect \
  --note "Reviewed proposed current truth"

sdai spec promote SIGN-123 --dry-run
sdai spec promote SIGN-123 --dry-run --json
sdai spec promote SIGN-123
```

`--dry-run` intentionally works before approval. Reviewers must be able to inspect the exact proposed truth transition before they approve it. A real promotion always requires a satisfied promotion approval.

## Promotion flow

```text
load typed change
       ↓
#56 baseline + requirement validation
       ↓
render complete proposed current specs
       ↓
verify operation effects deterministically
       ↓
semantic diff
       ↓
hash-bound promotion approval
       ↓
acquire local promotion lock
       ↓
re-validate + re-render under lock
       ↓
stage every affected domain
       ↓
replace canonical domain files
       ↓
verify post-write hashes
       ↓
write promotion evidence
       ↓
move change workspace into archive
```

No AI/provider call is part of this path.

## Semantic rendering

Existing current Markdown outside structured requirement records is preserved. The engine changes only recognized requirement lines from #56:

```markdown
- <requirement-id>: <definition>
```

inside recognized requirement/acceptance-criteria sections.

- `MODIFIED` keeps the ID/section and replaces the definition.
- `REMOVED` removes the source requirement line.
- `RENAMED` changes the ID while preserving the definition and section.
- `ADDED` inserts a new requirement into a deterministic section.

For well-known IDs, section routing is explicit:

| ID prefix | Section |
|---|---|
| `FR-` | `Functional Requirements` |
| `NFR-` | `Non-Functional Requirements` |
| `AC-` | `Acceptance Criteria` |

For organization-specific IDs, SDAI first reuses the section of existing requirements in the same ID family. If there is only one requirement section, it uses that section. Otherwise it uses/creates `## Requirements`.

A new domain is created from its ADDED operations with deterministic requirement sections.

## Semantic diff

`sdai spec diff` records, per domain:

- current spec SHA-256 (or absent for a new domain)
- proposed spec SHA-256
- canonical target path
- operation
- source/destination requirement IDs
- section
- before/after definitions
- change reason

JSON omits full proposed content by default. `--include-content` is explicit because full specifications can be significantly larger or contain sensitive design context.

Parallel-change footprint conflicts from #56 are shown as warnings in the diff. They do not create a deadlock where neither overlapping feature can promote: the first valid/approved promotion may proceed; the other change then fails its spec/requirement baseline validation and must be explicitly rebased.

## Hash-bound approval

Promotion approval lives in the feature change workspace:

```text
specs/changes/<FEATURE>/approvals/spec-promotion.yaml
```

The approval document is bound to:

```text
complete SpecChangeBundle SHA-256
+
current spec SHA-256 for every affected domain
```

Changing any delta or changing current truth therefore makes the old approval stale.

The existing `.sdai/approval-policies.yaml` policy named `spec-promotion` may require:

- minimum distinct approvals
- required roles
- allowed approver identities

If no explicit policy exists, one distinct approval is required.

### Current identity limitation

0.7 approval identities are local role/identity assertions, consistent with SDAI's current approval model. The approval file is protected from external workspace-writing agents because `specs/**` is a framework-protected path, but this is not cryptographic identity proof. GitHub Enterprise/OIDC/SSO-backed approval evidence remains the dedicated 0.18 milestone.

## Transaction and rollback

Before mutation, SDAI stages every proposed current file on the same filesystem. It then replaces target files and verifies the resulting SHA-256 values.

If a caught error occurs before the change workspace is archived, SDAI restores every already-replaced current domain from its exact original bytes (or removes a newly-created domain file). The integration tests inject a failure on the second domain replacement and require the first domain to be byte-for-byte restored.

This provides process-level transactional behavior for handled failures. It is not a distributed transaction or a guarantee against machine/power loss between filesystem operations. Stronger durable ledger/recovery behavior is planned in the later execution-ledger/provenance milestones.

A repository-local lock prevents two SDAI promotion processes from intentionally writing current truth concurrently.

## Promotion evidence and archive

A successful promotion writes `promotion.yaml` into the change workspace with:

- promotion ID/time
- complete change-bundle SHA-256
- hash-bound approval decision/identities
- before/after current-spec SHA-256 values
- semantic changes
- parallel-conflict evidence

The entire change workspace is then moved to a unique archive path. There is no public API in this slice that edits archived promotion evidence.

## Error codes

| Code | Meaning |
|---|---|
| `SDAI-SPECPROMO-001` | #56 validation blocks promotion/diff |
| `SDAI-SPECPROMO-002` | current Markdown cannot be rendered deterministically |
| `SDAI-SPECPROMO-003` | rendered proposed truth does not match delta semantics |
| `SDAI-SPECPROMO-004` | approval missing, stale, malformed, or policy-invalid |
| `SDAI-SPECPROMO-005` | change/evidence changed during promotion preparation |
| `SDAI-SPECPROMO-006` | concurrent local promotion lock exists |
| `SDAI-SPECPROMO-007` | transaction/write verification failed and was rolled back |
| `SDAI-SPECPROMO-008` | archive/evidence collision |
| `SDAI-SPECPROMO-009` | rollback itself was incomplete and requires operator intervention |

## Trust boundary

```text
AI/human authors proposed change files
                ↓
SDAI parser + validator calculate eligibility
                ↓
SDAI renderer calculates proposed current truth
                ↓
human approval binds exact hashes
                ↓
SDAI deterministic transaction writes current truth
```

AI agents cannot approve themselves, change validation outcomes, or write `specs/current` through external workspace-write execution. Only the deterministic lifecycle command owns promotion.
