# Multi-Repository Feature Graph

SD-AI 0.15 composes centralized SpecificationStore content, explicit repository ownership, and repository-local traceability into a single deterministic read-only feature graph.

The graph API version is:

```text
sdai.multi-repo-feature-graph/v1
```

## CLI

Human-readable output:

```bash
sdai feature graph FEATURE-123 --path .
```

Canonical JSON output:

```bash
sdai feature graph FEATURE-123 --json --path .
```

The command returns exit code `0` when the graph contains no error findings and `2` when deterministic error findings are present. Warnings do not change the success exit code.

The nested `feature graph` command is dispatched before the legacy feature-intake parser. Existing feature creation remains unchanged:

```bash
sdai feature FEATURE-123 \
  --title "Feature title" \
  --description "Feature description" \
  --path .
```

## Inputs

Graph construction is strictly local and read-only. It consumes only explicitly declared participants:

- `.sdai/specification-stores.yaml` for referenced SpecificationStores;
- immutable store manifest/content snapshots from the 0.15 store-reference contract;
- `.sdai/feature-repositories.yaml` for repository ownership and explicit local repository paths;
- the existing repository-local `TraceGraph` for each participating repository that contains `specs/changes/<FEATURE>`.

The builder does not clone, fetch, pull, push, create branches, update manifests, rewrite trace artifacts, or discover undeclared repositories.

## Nodes

`v1` defines these node types:

- `store`
- `repository`
- `requirement`
- `scenario`
- `rfc`
- `adr`
- `component`
- `contract`
- `threat`
- `task`
- `code`
- `test`
- `approval`
- `evidence`
- `pr-reference`

Existing trace graph node identities are preserved exactly. For example, `requirement:FR-001`, `task:TASK-001`, and code/test path-hash node IDs are not renamed when composed into the multi-repository graph.

`pr-reference` is a versioned node type reserved by this contract so later 0.15 slices do not require a schema break. #185 does **not** infer or synthesize PR references. Explicit requirement/task/implementation/evidence-to-PR ingestion and relationships are implemented by #187.

## Facts and provenance

A node or edge can have multiple immutable facts from different participants. Facts are canonical JSON and include their own SHA-256. Provenance retains useful identities and hashes without emitting participant absolute paths.

Examples of bound provenance include:

- SpecificationStore manifest SHA-256;
- SpecificationStore content snapshot SHA-256;
- exact store content-file SHA-256 for a change/delta source;
- feature-repository manifest/source SHA-256;
- repository-resolution SHA-256;
- repository trace graph/build SHA-256;
- ownership route decision SHA-256 and selector provenance;
- existing trace node/edge provenance.

The graph itself has a canonical `graphSha256` over all nodes, edges, findings, and top-level input-resolution hashes.

## Relationships

Every existing repository `TraceRelation` is copied verbatim with the exact source and target node IDs. Missing trace relationships are never guessed.

The composition layer adds only explicit deterministic relationships:

- `declares`: a bound SpecificationStore declares a requirement from a feature delta;
- `renamed-to`: an explicit `RENAMED` specification delta links the old and new requirement identities;
- `owned-by`: a requirement, contract, component, or task has exactly one repository selected by the 0.15 ownership router.

If routing is ambiguous or unavailable, the graph emits a finding and does not invent an `owned-by` edge.

## Deterministic findings

Findings are sorted canonically and are included in the graph hash. Current `v1` finding classes include:

- `SDAI-FEATURE-GRAPH-NO-STORE`
- `SDAI-FEATURE-GRAPH-STALE-CONTENT`
- `SDAI-FEATURE-GRAPH-MISSING-REPOSITORIES`
- `SDAI-FEATURE-GRAPH-MISSING-REPOSITORY`
- `SDAI-FEATURE-GRAPH-AMBIGUOUS-REPOSITORIES`
- `SDAI-FEATURE-GRAPH-AMBIGUOUS-ROUTING`
- `SDAI-FEATURE-GRAPH-MISSING-OWNERSHIP`
- `SDAI-FEATURE-GRAPH-REPOSITORY-HEALTH`
- `SDAI-FEATURE-GRAPH-AMBIGUOUS-TRACE`
- `SDAI-FEATURE-GRAPH-MISSING-REPOSITORY-TRACE`
- `SDAI-FEATURE-GRAPH-DISCONNECTED`

Warnings preserve useful partial graph information. Unsafe or invalid ownership that makes repository resolution unknowable is represented as an error finding and no ownership edge is synthesized.

## Read-only guarantee

Building or printing the graph must not change any participant bytes. Regression coverage snapshots the initialized project, central SpecificationStore, and API/UI/shared repositories before and after repeated graph construction and requires byte-for-byte equality.

Store content is read through its immutable content snapshot. Repository trace provenance that carries a declaration SHA-256 is rechecked against the current file bytes and stale content is surfaced as a deterministic error finding.

## Canonical output

JSON output is UTF-8, key-sorted, compact canonical JSON. Arrays are ordered deterministically by semantic identity. Local absolute repository/store paths are never part of the graph JSON.

The top-level payload contains:

```json
{
  "apiVersion": "sdai.multi-repo-feature-graph/v1",
  "featureId": "FEATURE-123",
  "inputs": {
    "repositoryResolutionSha256": "sha256:...",
    "storeResolutionSha256": "sha256:..."
  },
  "nodes": [],
  "edges": [],
  "findings": [],
  "graphSha256": "sha256:..."
}
```

The exact `graphSha256` changes whenever any graph-visible participant fact, relationship, finding, or bound input-resolution hash changes.