# `sdai analyze` — Read-Only Cross-Artifact Analysis

Issue #91 exposes the deterministic #89/#90 analysis engine as a stable engineer/CI command.

```bash
sdai analyze SIGN-123
sdai analyze SIGN-123 --json
sdai analyze SIGN-123 --risk critical --json
```

The command requires an initialized SDAI project and reads repository evidence only.

## Exit codes

| Exit | Meaning |
|---|---|
| `0` | Analysis completed and no `blocking` finding exists. Warnings/suggestions/info may still be present. |
| `1` | SDAI could not perform analysis because input/configuration/repository evidence is invalid or unavailable. |
| `2` | Analysis completed successfully and at least one deterministic `blocking` finding exists. |

This distinction allows CI to separate “the analyzer itself failed” from “the analyzer ran and found a release-blocking inconsistency.”

## JSON mode

```bash
sdai analyze SIGN-123 --json
```

writes exactly one `sdai.findings/v1` JSON document to stdout. It does not prepend summaries, labels, progress messages, or provider output.

Example shape:

```json
{
  "apiVersion": "sdai.findings/v1",
  "feature_id": "SIGN-123",
  "index_sha256": "sha256:...",
  "findings": [
    {
      "code": "UNTESTED_SCENARIO",
      "severity": "warning",
      "entity_id": "AC-004",
      "message": "...",
      "evidence": [
        {
          "source": "specs/changes/SIGN-123/requirements.md",
          "line": 42,
          "entity_id": "AC-004"
        }
      ]
    }
  ]
}
```

Configuration/input errors are written to stderr by the normal SDAI command boundary and return exit `1`; JSON stdout remains empty on those failures.

## Human mode

Human output starts with a deterministic summary and index identity:

```text
Analysis feature=SIGN-123 findings=3 blocking=1 warnings=2 suggestions=0 info=0
Index: sha256:...
```

Findings are shown in severity order (`blocking`, `warning`, `suggestion`, `info`), then by code/entity/message. Each finding prints its repository-relative POSIX `source:line` evidence.

Example:

```text
BLOCKING   UNAPPROVED_BREAKING_CHANGE entity=CONTRACT-001: ...
  specs/changes/SIGN-123/contracts/api.yaml:8 [CONTRACT-001] — ...
```

## Risk option

`--risk` selects the 0.8 ArtifactSchema/artifact-state risk profile used by the `STALE_ARTIFACT` rule:

```text
trivial
standard   (default)
critical
regulated
```

The risk option does not change semantic/provider behavior and does not invoke an agent.

## Read-only guarantee

`sdai analyze` does not:

- create or update analysis artifacts;
- rewrite specifications/current truth;
- update artifact-state records;
- grant or modify approvals;
- run providers/models;
- change workflow state;
- write caches into the feature directory.

Tests snapshot the initialized workspace before and after human/JSON analysis and require byte-for-byte equality.

If persistent analysis evidence is desired later, that must be a separate explicit lifecycle operation with its own hash/provenance semantics; a read-only diagnostic command must not silently become a write path.

## Relationship to later roadmap slices

`sdai analyze` reports consistency facts/findings only. It does not remediate findings automatically and does not mutate approved source-of-truth artifacts to make code/specification appear consistent.

0.10 traceability and 0.11 verify/converge will consume these stable finding/index contracts rather than re-parsing human CLI text.
