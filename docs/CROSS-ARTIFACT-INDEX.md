# Cross-Artifact Index and Findings Contract

Issue #89 establishes the read-only fact model used by SDAI 0.9 cross-artifact analysis. It intentionally does **not** decide whether a fact is a defect. The deterministic analysis rules and `sdai analyze` command are later slices (#90 and #91).

## Trust boundary

```text
ArtifactSchema DAG + feature repository files
                   ↓
         deterministic UTF-8 indexing
                   ↓
 entities + relationships + source/line evidence
                   ↓
       sdai.analysis-index/v1
                   ↓
         analysis rules (#90)
                   ↓
          sdai.findings/v1
```

No provider/model call occurs while building the index. No feature, current-spec, approval, workflow, or artifact-state file is written.

## Source boundary

The index reads regular, non-symlink UTF-8 files beneath:

```text
specs/changes/<FEATURE>/
```

Supported source types in v1:

- Markdown (`.md`, `.markdown`)
- YAML (`.yaml`, `.yml`)
- JSON (`.json`)
- text (`.txt`)

Framework-owned `.sdai` subdirectories are excluded from source discovery. Symlinked evidence and invalid UTF-8 fail closed rather than being followed or silently skipped.

The effective 0.8 ArtifactSchema DAG is embedded as read-only facts, including artifact identity, path template, rendered feature path where possible, dependency edges, required state, effective source layer, and whether the resolved feature artifact currently exists.

## Entity identities

The v1 normalizer recognizes these explicit ID families:

| Prefix | Kind |
|---|---|
| `FR-`, `NFR-`, `REQ-` | requirement |
| `AC-`, `SCN-` | scenario |
| `TASK-` | task |
| `TEST-` | test |
| `ADR-` | ADR |
| `CONTRACT-`, `API-`, `EVENT-`, `SCHEMA-` | contract |
| `THREAT-` | threat |
| `MITIGATION-` | mitigation |
| `APPROVAL-` | approval |

IDs are normalized to uppercase. A declaration is identified from an explicit Markdown-style ID declaration or a structured YAML-style ID field such as `task_id: TASK-001` or `threat_id: THREAT-001`.

Each entity records:

- normalized ID
- kind
- repository-relative POSIX source path
- one-based source line
- declaration title/definition text when present
- nearby explicit `status:` value when present
- a unique evidence key containing kind, ID, source, and line

Duplicate IDs are **not collapsed**. They remain separate evidence records so #90 can diagnose conflicts/duplicates without the index silently choosing a winner.

## Relationships

References to recognized IDs are associated with the current declared entity in a source file until the next declaration. Each relationship records:

```text
from_id
to_id
relation = references
source
line
```

For example:

```markdown
- TASK-001: Implement signing.
requirements: [FR-001, NFR-001]
tests: [TEST-001]
```

produces evidence-backed relationships:

```text
TASK-001 -> FR-001
TASK-001 -> NFR-001
TASK-001 -> TEST-001
```

The index does not decide whether those targets exist, whether a relation is sufficient, or whether it represents correct architecture. Those are analysis-rule responsibilities.

## Stable index identity

`FeatureArtifactIndex.to_json()` emits:

```json
{
  "apiVersion": "sdai.analysis-index/v1",
  "feature_id": "SIGN-123",
  "files": [],
  "entities": [],
  "relationships": [],
  "artifact_graph": {},
  "sha256": "sha256:..."
}
```

The SHA-256 is calculated from canonical JSON over the fact payload before the `sha256` display field is added. Text file hashes normalize CRLF/CR to LF so Windows and Linux produce the same evidence identity for the same logical UTF-8 text.

The same repository bytes + same effective artifact schema must produce byte-stable JSON.

## `sdai.findings/v1`

#89 also defines the transport contract that later deterministic rules will populate:

```json
{
  "apiVersion": "sdai.findings/v1",
  "feature_id": "SIGN-123",
  "index_sha256": "sha256:...",
  "findings": [
    {
      "code": "ORPHAN_TASK",
      "severity": "warning",
      "message": "...",
      "entity_id": "TASK-001",
      "evidence": [
        {
          "source": "specs/changes/SIGN-123/tasks.md",
          "line": 12,
          "entity_id": "TASK-001"
        }
      ]
    }
  ]
}
```

Supported finding severities are:

- `blocking`
- `warning`
- `suggestion`
- `info`

Finding codes are stable uppercase identifiers. Findings are deterministically ordered before JSON serialization.

## What #89 does not do

The index does not yet emit policy findings such as:

- `ORPHAN_REQUIREMENT`
- `ORPHAN_TASK`
- `MISSING_NFR`
- `ARCHITECTURE_CONFLICT`
- `CONTRACT_CONFLICT`
- `UNRESOLVED_ADR`
- `UNTESTED_SCENARIO`
- `UNAPPROVED_BREAKING_CHANGE`
- `UNMITIGATED_THREAT`
- `STALE_ARTIFACT`

Those rules belong to #90 and must consume this fact model without changing source artifacts.

The index also does not infer semantic links with an LLM. If a relationship is not backed by repository evidence, the deterministic index does not invent it.
