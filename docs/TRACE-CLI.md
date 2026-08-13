# Trace CLI

SDAI 0.10 exposes the canonical trace graph through read-only engineer and CI commands.

## Commands

```text
sdai trace FEATURE
sdai trace requirement FEATURE <REQ-ID>
sdai trace missing FEATURE
sdai trace coverage FEATURE [--json]
sdai trace export FEATURE --format json
```

All commands accept `--path PATH`. Summary, requirement, missing, and coverage commands also support machine-clean JSON where documented by `--json`.

## `sdai trace FEATURE`

Builds the current canonical graph and prints node/edge/gap totals, graph SHA-256, typed nodes, typed relationships, and source:line provenance. `--json` returns the stable `sdai.trace-summary/v1` summary contract.

This command is descriptive only; it does not treat missing trace links as command failure.

## Requirement query

```text
sdai trace requirement SIGN-123 FR-001
```

Explains the canonical requirement node, incoming/outgoing typed relationships, current evidence proof state, and requirement-specific gaps.

The JSON contract is `sdai.trace-requirement/v1`.

Exit codes:

- `0` requirement exists
- `2` requirement is not present in the canonical graph
- `1` operational/validation failure

## Missing links

```text
sdai trace missing SIGN-123
```

Returns unresolved/ambiguous builder gaps plus requirements that have no **valid current** evidence proof. Historical stale/blocked/missing evidence therefore never hides a missing current-coverage relationship.

The JSON contract is `sdai.trace-missing/v1`.

Exit codes:

- `0` no missing links/current uncovered requirements
- `2` at least one gap exists
- `1` operational/validation failure

## Current coverage

```text
sdai trace coverage SIGN-123 --json
```

Coverage is intentionally conservative in 0.10: a requirement is covered only by an explicit `evidenced-by` relationship whose typed evidence freshness evaluates to `valid` against the current repository state.

The `sdai.trace-coverage/v1` contract reports:

- graph identity
- total/covered/uncovered requirements
- percentage
- graph-gap count
- valid/stale/missing/blocked proof counts
- per-requirement proof detail and provenance

Exit codes:

- `0` every declared requirement has current valid proof
- `2` one or more requirements are uncovered
- `1` operational/validation failure

## Exact canonical export

```text
sdai trace export SIGN-123 --format json
```

Writes the exact canonical `sdai.trace-graph/v1` JSON serialization to stdout. There is no wrapper/envelope around the graph, allowing CI to hash or persist exactly the same bytes SDAI uses for the graph contract.

## Read-only guarantee

Trace CLI commands build/query/export repository state only. They do not write graph artifacts, freshness records, evidence, or source changes. Tests snapshot repository bytes before/after invocation.

## Portability

Human and JSON output preserves UTF-8 repository provenance such as `src/café.py` while canonical code/test node identities remain platform-stable path SHA-256 IDs. Repository paths render with POSIX separators on Windows and Linux.

## Compatibility routing

The installed `sdai` executable continues to use the existing version-aware entrypoint. That entrypoint intercepts only the new top-level `trace` command and delegates every other command to the existing lifecycle CLI unchanged.
