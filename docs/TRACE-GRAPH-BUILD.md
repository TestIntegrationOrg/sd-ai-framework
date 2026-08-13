# Canonical Trace Graph Build

SDAI 0.10 builds a feature-level trace graph as a deterministic, read-only projection of facts already present in the repository. The builder never calls an AI provider and never invents semantic relationships.

## API

```python
from sdai.trace_builder import build_feature_trace_graph

result = build_feature_trace_graph(project_root, "FEATURE-123", environ={})
graph = result.graph
gaps = result.gaps
```

The build-result envelope is versioned as `sdai.trace-build/v1`. The canonical graph remains `sdai.trace-graph/v1`.

## Inputs

The builder combines four fact sources:

1. **0.9 cross-artifact facts** for requirements, scenarios, ADRs, contracts, threats, tasks, tests, and approvals.
2. **Explicit architecture declarations** for `RFC-*` and `COMPONENT-*` identifiers found in feature artifacts.
3. **Repository source/test files** that explicitly reference known trace identifiers.
4. **Validated `sdai.trace-evidence/v1` records** stored under the feature directory.

No source is mutated during graph construction.

## Explicit relationships only

Cross-artifact references and source-code/test references become `references` edges. Typed evidence becomes `evidenced-by` only when its `subject` exactly names an existing canonical trace node ID.

The builder deliberately does not infer relationships such as `implements`, `verifies`, `designed-by`, or `contains` from prose, filenames, directory layout, or model output. Later SDAI slices may promote explicit structured declarations to richer edge types, but this builder preserves truth rather than guessing intent.

## Missing and ambiguous endpoints

`TraceGraph` itself contains only valid edges whose endpoints exist. Unresolved facts are preserved alongside it as sorted `TraceGap` records. Examples include:

- `missing-endpoint`
- `ambiguous-endpoint`
- `missing-evidence-subject`

This keeps incomplete brownfield traceability inspectable instead of silently dropping it or synthesizing a relationship.

## Source and test discovery

Supported source suffixes include common Python, Java/Kotlin, .NET, Go, Rust, JavaScript/TypeScript, C/C++, shell, PowerShell, Ruby, PHP, Scala, and Swift files.

A repository file is added as a code/test node only when it contains an explicit trace identifier. Generated/dependency directories such as `.git`, `.sdai`, virtual environments, `node_modules`, `dist`, `build`, and `target` are excluded.

Test paths are recognized using conventional `tests`/`test` directories and test filename conventions.

### Portable file identity

Repository paths may contain UTF-8 characters that are intentionally not legal in portable trace entity IDs. Code/test nodes therefore use:

```text
path-sha256:<sha256-of-repository-relative-UTF8-POSIX-path>
```

The original readable path remains in node metadata and provenance. This makes node identity deterministic across Windows/Linux without losing human-readable evidence location.

## Typed evidence

Only files identifying themselves as `sdai.trace-evidence/v1` are treated as typed evidence candidates. They must pass the strict evidence validator before entering the graph.

Evidence graph semantics use `truth_sha256`, not the complete producer record hash. Consequently a provider/model-only change does not alter graph truth, graph JSON, or graph hash. Different evidence truth for the same evidence ID fails closed.

The evidence node records truth-relevant fields only:

- evidence kind
- status
- Git commit
- `truth_sha256`

Provider and model metadata remain available in the durable evidence record for audit but are not graph semantics.

## Determinism and conflicts

The underlying `TraceGraph` contract canonicalizes nodes, edges, metadata, and provenance and provides a stable SHA-256 graph hash. `TraceBuildResult` separately hashes the graph identity plus its sorted gap set.

Conflicting duplicate semantic nodes or conflicting evidence truth fail closed. Identical duplicates can merge deterministically through the graph contract.

## Read-only and path safety

The builder performs no writes. Feature and repository paths are resolved inside the project root, source/test symlinks fail closed, paths are normalized to repository-relative POSIX form, and textual trace sources are read as UTF-8.

This behavior is intended for both greenfield and brownfield repositories: incomplete links become gaps, while unsafe or ambiguous canonical facts fail closed.

## Error families

- `SDAI-TRACE-BUILD-001`: project/path/symlink safety errors.
- `SDAI-TRACE-BUILD-002`: cross-artifact or UTF-8 source ingestion errors.
- `SDAI-TRACE-BUILD-003`: typed evidence validation errors.
- `SDAI-TRACE-BUILD-004`: canonical graph or evidence-truth conflicts.
