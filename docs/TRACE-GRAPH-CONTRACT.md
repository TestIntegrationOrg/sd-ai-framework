# Canonical Traceability Graph Contract

Issue #103 defines the provider-independent data contract used by later SDAI 0.10 traceability slices. This slice defines canonical graph truth; it does **not** yet discover repository facts or expose the `sdai trace` CLI.

## Versioned contract

```text
sdai.trace-graph/v1
```

A feature graph contains:

```text
feature_id
nodes[]
edges[]
sha256
```

The SHA-256 is calculated from canonical JSON containing the API version, feature ID, canonical node list, and canonical edge list. Input ordering therefore does not affect graph identity.

## Node types

The v1 canonical node vocabulary is:

```text
requirement
scenario
rfc
adr
component
contract
threat
task
code
test
approval
evidence
```

Logical node identity is:

```text
<type>:<entity-id>
```

Examples:

```text
requirement:FR-001
scenario:AC-001
task:TASK-004
code:src/signing/service.py#SigningService.sign
test:tests/test_signing.py#test_signs
```

Each node carries a label, finite structured metadata, and one or more source/line provenance declarations.

## Edge relations

The v1 directed relation vocabulary is:

```text
has-scenario
designed-by
implemented-by
verified-by
threatened-by
mitigated-by
approved-by
evidenced-by
contains
depends-on
references
```

Relations have deterministic endpoint-type rules. For example:

- `requirement --has-scenario--> scenario`
- `requirement/scenario --designed-by--> rfc|adr|component|contract`
- `requirement/scenario/task --implemented-by--> task|code`
- `requirement/scenario/task/code/contract --verified-by--> test|evidence`
- `requirement/component/contract/code --threatened-by--> threat`
- `threat --mitigated-by--> task|code|evidence`
- `requirement/rfc/adr/contract/threat --approved-by--> approval`
- non-evidence nodes may be `evidenced-by` an evidence node
- `component --contains--> code|test`

`references` and `depends-on` remain explicit typed directed relations that can connect canonical node types without pretending a stronger lifecycle meaning than the source artifact declared.

## Provenance

Every node and edge requires at least one provenance record:

```text
source: repository-relative POSIX path
line: positive 1-based line
detail: optional text
declaration_sha256: optional declaration/source hash
```

`trace_provenance_for_path()` creates provenance from an actual repository file while enforcing:

- containment inside the project root;
- regular-file requirement;
- no symlink component;
- repository-relative POSIX rendering; and
- current SHA-256 binding.

This helper supports spaces and UTF-8 names such as `café`, `Ω`, and `Δ`.

## Duplicate declarations

SDAI must never silently hide duplicate trace facts.

For the same logical node or edge:

- if semantic fields are identical, SDAI merges the declaration while retaining **all unique source/line provenance**;
- if label, metadata, relation, source endpoint, or target endpoint semantics conflict, graph construction fails closed;
- two different declarations at the exact same source/line with conflicting declaration hashes also fail closed.

This lets later repository discovery (#105) retain duplicate declaration evidence without creating unstable logical IDs.

## Determinism

Canonicalization sorts:

- nodes by node type + logical entity ID;
- edges by relation + source + target;
- provenance by source + line + hash/detail.

Metadata is defensively deep-copied and validated as finite JSON data before it enters graph truth. Mutating a caller-owned input dictionary after graph construction cannot alter the graph hash.

## Strict loading

`TraceGraph.from_mapping()` / `from_json()` validate:

- API version;
- exact node/edge field sets;
- node IDs derived from type + entity ID;
- edge IDs derived from relation + source + target;
- source/target endpoint existence;
- relation endpoint-type rules;
- provenance field types/paths;
- optional supplied canonical SHA-256.

A mismatched serialized SHA-256, missing endpoint, invalid relation pairing, unknown field, invalid type, or ambiguous duplicate fails closed with an `SDAI-TRACE-*` error.

## Trust boundary

Providers and future extractors may propose trace facts. Deterministic SDAI owns:

- allowed canonical node/relation vocabulary;
- logical identity;
- provenance validity;
- duplicate/conflict handling;
- endpoint semantics;
- canonical ordering; and
- graph SHA-256.

#103 intentionally does not infer missing relationships. #105 will populate the contract using explicit repository facts, and missing relationships will remain visible graph gaps rather than AI guesses.
