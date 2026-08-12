# Artifact Schema Registry and Dependency DAG

SDAI 0.8 introduces file-driven artifact definitions so requirements, architecture, plans, tasks, tests, verification, and organization-specific artifacts can participate in a deterministic lifecycle graph without adding Python branches for each artifact type.

## Extension contract

Artifact schemas use the shared `sdai/v1` extension envelope:

```yaml
apiVersion: sdai/v1
kind: ArtifactSchema
metadata:
  id: company-lifecycle
  version: 1.0.0
  description: Company artifact requirements
spec:
  artifacts:
    - id: operational-readiness
      path: specs/changes/{feature}/operational-readiness.md
      type: markdown
      required: true
      depends_on: [architecture, tests]
      applies_to: [critical, regulated]
```

The initial schema model supports:

- stable artifact IDs
- portable repository-relative path templates
- artifact types
- required/optional state
- dependency edges
- risk applicability metadata
- authoritative locks
- layered provenance

Supported path placeholders are `{feature}` and `{domain}`. Paths use POSIX separators and must remain portable across Windows and Linux. Absolute paths, parent traversal, Windows drive paths, reserved DOS device names, malformed placeholders, and backslash-based paths fail validation.

## Built-in schema

The default lifecycle graph is itself data, packaged at:

```text
src/sdai/builtin_schemas/core.yaml
```

It is not a Python hard-coded artifact list. The built-in graph includes requirements, architecture, ADR, security, plan, tasks, tests, and verification relationships.

## Layering and precedence

Artifact schema contributions are resolved in deterministic authority order:

```text
builtin -> organization -> repository -> user
```

Sources are:

```text
builtin       packaged `sdai/builtin_schemas/*.yaml`
organization  absolute file/directory from SDAI_ORG_SCHEMA_PATH
repository    .sdai/schemas/*.yaml
user          absolute file/directory from SDAI_USER_SCHEMA_PATH
```

External organization/user schema roots must be absolute real files/directories and may not be symlinks. Repository schema files remain contained by the project root and may not be symlinks.

Two definitions of the same artifact in one layer fail closed instead of using last-write-wins behavior.

## Organization non-weakening rules

Organization policy is authoritative.

An organization schema may make an artifact required, add mandatory dependency edges, or lock an artifact definition. Lower repository/user layers cannot:

- disable an organization-required artifact
- make an organization-required artifact optional
- remove an explicitly organization-mandated dependency edge
- override any organization-locked artifact

SDAI tracks the exact organization mandate separately from the current effective definition. This matters when a repository adds an extra dependency after the organization layer: a user layer may later remove that repository-only edge as long as every organization-mandated edge remains. Lower-layer additions are never accidentally promoted into organization policy.

A lower layer that removes an artifact still must leave a valid graph. If another effective artifact depends on the removed artifact, missing-edge validation fails.

## DAG validation

The effective graph is deterministic and fail-closed.

Validation rejects:

- duplicate artifact definitions in the same layer
- malformed/unsafe paths
- invalid artifact/risk/type metadata
- self-dependencies
- references to missing artifacts
- dependency cycles
- forbidden organization-policy weakening

Topological order is calculated deterministically from artifact IDs and dependency edges.

## CLI

```bash
sdai schema list
sdai schema show requirements
sdai schema validate
sdai schema graph
```

All commands support the initialized-project boundary. `list`, `validate`, and `graph` support `--json`; `show` supports `--json` for one artifact.

JSON graph output includes:

- effective artifact definitions
- dependency edges
- topological order
- source layers
- field-level contribution history
- explicit organization-required and organization-dependency evidence

This is the explainability/provenance input for the 0.8 staleness engine in issue #74.

## Trust boundary

The artifact graph is deterministic framework state. AI/provider output may propose or author artifacts, but it does not decide whether the graph is valid, whether organization requirements can be weakened, or which dependency order is effective.

```text
schema files
    ↓
strict sdai/v1 parsing
    ↓
layered deterministic merge
    ↓
organization non-weakening checks
    ↓
DAG validation
    ↓
effective graph + provenance
```

The next 0.8 slice binds actual artifact content/evidence hashes to this effective graph and computes fresh/stale/missing/blocked state without provider involvement.

## Stable error families

| Code | Meaning |
|---|---|
| `SDAI-SCHEMA-001` | malformed schema/envelope/field metadata |
| `SDAI-SCHEMA-002` | unsafe or non-portable artifact path template |
| `SDAI-SCHEMA-003` | duplicate artifact definition in one layer |
| `SDAI-SCHEMA-004` | authoritative organization/lock rule violation |
| `SDAI-SCHEMA-005` | missing dependency |
| `SDAI-SCHEMA-006` | self-dependency or dependency cycle |
| `SDAI-SCHEMA-008` | invalid schema source path/symlink boundary |
