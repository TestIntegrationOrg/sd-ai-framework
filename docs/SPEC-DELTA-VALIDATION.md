# Delta Baseline and Parallel Conflict Validation

Issue #56 adds the read-only validation layer above the typed change model introduced in #55. It still does not write or promote `specs/current`.

## Two levels of baseline identity

SDAI validates both the current specification and every touched existing requirement.

### Specification identity

`load_current_spec()` from the #55 model hashes normalized UTF-8 Markdown:

```text
sha256(normalized current specification text)
```

`change.yaml` and each domain delta carry that identity as `baseline_spec_sha256`.

If current truth no longer has the same hash, validation emits:

```text
SDAI-SPECVAL-003  STALE_SPEC_BASELINE
```

This is fail-closed. A stale change must be explicitly rebased/re-authored rather than silently applied to different canonical truth.

### Requirement identity

Current requirement records use canonical single-line Markdown inside recognized requirement sections:

```markdown
## Functional Requirements
- FR-001: The service MUST sign a PowerShell file.

## Non-Functional Requirements
- NFR-001: Signing MUST complete within 2 seconds.

## Acceptance Criteria
- AC-001: A valid request returns a signed file.
```

Any `##` section whose heading contains `requirement`, plus `## Acceptance Criteria`, is a requirement section. Colon-bearing bullets outside those sections are not treated as requirements.

Each requirement identity is:

```text
sha256("<requirement-id>:<trimmed-definition>")
```

A `MODIFIED`, `REMOVED`, or `RENAMED` operation must have a `previous_hash` that matches this current identity. Otherwise validation emits:

```text
SDAI-SPECVAL-007  STALE_REQUIREMENT_BASELINE
```

This makes an edit to the same requirement detectable even when multiple feature changes were authored from an earlier baseline.

## Operation validation

Against current truth, the validator checks:

| Operation | Required current-state condition |
|---|---|
| `ADDED` | `requirement_id` does not already exist |
| `MODIFIED` | source exists and `previous_hash` matches |
| `REMOVED` | source exists and `previous_hash` matches |
| `RENAMED` | source exists, `previous_hash` matches, destination does not already exist |

A `null` domain baseline means the change expects a new domain. Only `ADDED` operations are valid while the domain is absent. If current truth appears before validation/promotion, the change becomes invalid.

## Current specification structural requirements

Requirement-level promotion cannot be deterministic when current truth has ambiguous structured records. Validation therefore blocks when:

- the same requirement ID appears more than once;
- an existing-domain current specification contains no recognized structured requirement records.

These checks do not rewrite Markdown; they only produce findings.

## Finding codes

| Code | Kind | Meaning |
|---|---|---|
| `SDAI-SPECVAL-001` | `CURRENT_SPEC_MISSING` | change expects existing domain but current truth is absent |
| `SDAI-SPECVAL-002` | `UNEXPECTED_CURRENT_SPEC` | change expects a new domain but current truth already exists |
| `SDAI-SPECVAL-003` | `STALE_SPEC_BASELINE` | whole current specification changed |
| `SDAI-SPECVAL-004` | `DUPLICATE_CURRENT_REQUIREMENT` | current truth contains a duplicate requirement ID |
| `SDAI-SPECVAL-005` | `ADDED_REQUIREMENT_EXISTS` | ADDED target already exists |
| `SDAI-SPECVAL-006` | `TARGET_REQUIREMENT_MISSING` | MODIFIED/REMOVED/RENAMED source does not exist |
| `SDAI-SPECVAL-007` | `STALE_REQUIREMENT_BASELINE` | source requirement hash changed |
| `SDAI-SPECVAL-008` | `RENAME_DESTINATION_EXISTS` | rename destination already exists |
| `SDAI-SPECVAL-009` | `PARALLEL_CHANGE_CONFLICT` | two feature changes address the same requirement identity |
| `SDAI-SPECVAL-010` | `NO_STRUCTURED_REQUIREMENTS` | existing current truth has no deterministic requirement records |

Reports have stable JSON representations with sorted feature/domain/finding order.

## Parallel feature conflicts

`detect_parallel_change_conflicts()` computes a read-only footprint for every selected change:

```text
(domain, operation.requirement_id)
(domain, RENAMED.new_requirement_id)
```

If two changes address the same identity in the same domain, SDAI emits `PARALLEL_CHANGE_CONFLICT` and records every related feature ID.

Examples that conflict:

```text
Feature A: MODIFIED FR-001
Feature B: REMOVED  FR-001
```

```text
Feature A: RENAMED FR-001 -> FR-010
Feature B: ADDED               FR-010
```

Disjoint requirement changes in the same domain do not produce a parallel-footprint conflict, though the whole-spec baseline must still be current when each change is validated.

## Read-only trust boundary

The #56 API performs only:

```text
load typed change
  ↓
load current truth
  ↓
parse structured requirement identities
  ↓
compare spec + requirement hashes
  ↓
calculate operation findings
  ↓
calculate multi-change footprints
  ↓
return deterministic JSON evidence
```

There is no write path to `specs/current`. Atomic promotion remains #57 and must consume a valid #56 report rather than reimplementing these checks in an AI/provider layer.
