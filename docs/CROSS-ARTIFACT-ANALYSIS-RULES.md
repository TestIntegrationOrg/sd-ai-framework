# Deterministic Cross-Artifact Analysis Rules

Issue #90 defines the repository-only rules that convert `sdai.analysis-index/v1` facts into `sdai.findings/v1`. These rules are intentionally explicit and conservative; they do not use an LLM to infer relationships that are not present in repository evidence.

## Read-only contract

`analyze_feature(...)`:

- builds the #89 feature artifact index;
- evaluates existing #74 artifact freshness records read-only;
- emits deterministic findings;
- performs no provider/model call;
- does not write feature, current-spec, approval, artifact-state, workflow, or framework files.

The same repository bytes and effective artifact schema produce the same finding JSON.

## Stable finding semantics

| Finding | Severity | Deterministic v1 rule |
|---|---|---|
| `ORPHAN_REQUIREMENT` | warning | A declared `FR-*`, `NFR-*`, or `REQ-*` has no explicit relationship, in either direction, to a **declared** `TASK-*`. A reference to an undeclared task does not count. |
| `ORPHAN_TASK` | warning | A declared `TASK-*` has no explicit relationship, in either direction, to a **declared** requirement. A reference to an undeclared requirement does not count. |
| `MISSING_NFR` | warning | The feature declares at least one requirement but declares no `NFR-*`. Empty/no-requirement features are left to requirements-quality rules instead of generating evidence-free analysis findings. |
| `ARCHITECTURE_CONFLICT` | blocking | The same `ADR-*` ID has two or more declarations with different normalized title/status evidence. Identical duplicate declarations are preserved by the index but do not count as a conflict. |
| `CONTRACT_CONFLICT` | blocking | The same contract-family ID (`CONTRACT-*`, `API-*`, `EVENT-*`, `SCHEMA-*`) has conflicting normalized title/status declarations. |
| `UNRESOLVED_ADR` | warning | ADR status is missing or is not `accepted`, `resolved`, or `superseded`. |
| `UNTESTED_SCENARIO` | warning | A declared `AC-*`/`SCN-*` has no explicit relationship, in either direction, to a declared `TEST-*`. |
| `UNAPPROVED_BREAKING_CHANGE` | blocking | A contract is explicitly breaking and has no explicitly related approved approval record. |
| `UNMITIGATED_THREAT` | blocking | A threat is unresolved and has no explicitly related mitigation in a completed/implemented terminal state. |
| `STALE_ARTIFACT` | blocking | 0.8 artifact-state evaluation returns `stale` **and** a prior hash-bound state record exists. Artifacts that merely have no historical state record are not relabeled as “became stale.” |

## Explicit breaking-change convention

Version 1 treats a contract as breaking only when repository evidence is explicit:

- `status: breaking`, `breaking-change`, `breaking_change`, or `breakingchange`; or
- the declaration title starts with `breaking` / `breaking change`; or
- the declaration title includes `[breaking]`.

Text such as `Non-breaking compatibility update` is not classified as breaking.

An approval counts only when:

1. an `APPROVAL-*` entity has status `approved`, `accepted`, `granted`, or `satisfied`; and
2. repository evidence explicitly relates that approval and the breaking contract (in either direction).

## ADR and threat terminal states

Resolved ADR states:

```text
accepted
resolved
superseded
```

Resolved threat states:

```text
mitigated
resolved
closed
accepted
```

Completed mitigation states:

```text
implemented
mitigated
resolved
accepted
complete
completed
closed
```

A mitigation that merely exists with `planned`, `proposed`, missing, or another nonterminal status does not suppress `UNMITIGATED_THREAT`.

## Finding evidence and deduplication

Every emitted finding contains source evidence. Entity findings carry the declaration source/line. Stale artifact findings carry both the artifact path and the previous hash-bound state-record source.

The engine normalizes duplicate rule results to one finding per:

```text
(finding code, entity id)
```

and merges unique source/line/detail evidence deterministically. This prevents duplicate declarations from producing repeated semantic findings while retaining all evidence needed to investigate the issue.

Global findings such as `MISSING_NFR` use a null entity ID and carry the actual requirement declarations as evidence.

## Relationship trust rule

A relationship counts only when both sides are declared entities of the expected kinds. For example:

```markdown
- TASK-001: Implement FR-999.
```

does **not** prevent `ORPHAN_TASK` if `FR-999` is never declared.

This avoids letting dangling references manufacture traceability coverage.

## Artifact freshness distinction

The 0.8 state engine correctly treats an existing artifact with no state record as stale from the state engine's perspective (“not proven fresh”). Cross-artifact analysis uses a narrower historical finding:

- no previous record → no `STALE_ARTIFACT` finding;
- previous record + current mismatch/evidence invalidation/dependency invalidation → `STALE_ARTIFACT`.

This distinction keeps brownfield analysis useful while preserving the stronger 0.8 freshness semantics for gates that explicitly require recorded evidence.

## Provider boundary

These rules do not attempt semantic inference such as “these two paragraphs probably conflict.” If repository evidence is insufficient for a deterministic rule, the analyzer does not invent a finding or relationship. Later semantic reviewer agents may add advisory findings through a separate contract, but deterministic SDAI remains the authority for the rules above.
