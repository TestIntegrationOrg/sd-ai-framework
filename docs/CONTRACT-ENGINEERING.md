# Contract Engineering (SD-AI 0.16)

SD-AI 0.16 makes API, event, schema, and Protobuf compatibility a deterministic engineering concern owned by the SDAI engine. Providers may help author contracts or evidence, but providers do not define compatibility truth, policy precedence, evidence freshness, trace identity, or approval authority.

## Supported contract formats

The explicit project contract manifest is `.sdai/contracts.yaml` using `sdai.contract-sources/v1`. A source declaration has a stable `id`, one supported `kind`, and a repository-relative local `path`.

Supported kinds are:

- `openapi` — OpenAPI 3.0.x and 3.1.x validation and compatibility diff;
- `asyncapi` — AsyncAPI 2.x and 3.x validation and compatibility diff;
- `json-schema` — draft-07, 2019-09, and 2020-12 validation and compatibility diff; and
- `protobuf` — deterministic native `.proto` parsing, declared-local import resolution, and wire/API compatibility diff.

Contract discovery is explicit and local. SDAI does not crawl the repository for undeclared contracts and does not fetch remote references, invoke `protoc`, execute plugins, interpolate shell commands, or use network fallback to determine contract truth.

## CLI surfaces

The 0.16 command family is:

```text
sdai contract inspect [--json] [--path PATH]
sdai contract check SOURCE [--json] [--path PATH]
sdai contract diff SOURCE --against PATH --direction backward|forward|full [--json] [--path PATH]
sdai contract gate SOURCE --against PATH --direction backward|forward|full --criticality light|standard|critical [--evidence PATH ...] [--json] [--path PATH]
```

`inspect` hashes the manifest and normalized source snapshots. `check` invokes the format adapter. `diff` produces deterministic directional compatibility findings. `gate` classifies findings, resolves effective contract policy, validates the engineering constitution, evaluates current typed evidence freshness, and returns a policy decision.

Stable gate exit classes are:

- `0` — the governed change is allowed;
- `2` — inputs are valid but policy blocks the change; and
- `1` — project, contract, policy, evidence, or safety input is malformed/unsafe.

Contract trace uses the existing trace commands:

```text
sdai trace FEATURE --json
sdai trace coverage FEATURE --json
sdai trace missing FEATURE --json
sdai trace export FEATURE --format json
```

`trace coverage` retains `sdai.trace-coverage/v1` and adds deterministic `contract_trace` coverage for declared sources, addressable symbols, explicit links, source/symbol hashes, and stale/dangling gaps.

## Stable JSON/API contracts

The principal 0.16 machine contracts are:

- `sdai.contract-sources/v1`
- `sdai.contract-snapshot/v1`
- `sdai.contract-result/v1`
- `sdai.contract-diff/v1`
- `sdai.contract-policy/v1`
- `sdai.contract-policy-decision/v1`
- `sdai.contract-trace/v1`
- `sdai.trace-coverage/v1` (extended with contract trace coverage)

Canonical objects use sorted, finite JSON and SHA-256 identities. Equivalent inputs produce stable ordering and hashes across supported platforms.

## Compatibility and classification

Adapters emit stable format-specific findings. The contract policy engine maps known compatibility findings to `non-breaking`, `breaking`, or `unknown` classes. Unknown error-level changes fail closed unless the effective policy explicitly permits unknown changes.

Directional semantics are explicit:

- `backward` evaluates baseline consumers against the candidate;
- `forward` evaluates the reverse compatibility direction; and
- `full` evaluates both directions deterministically.

The engine, not an AI provider, owns the final classification and decision.

## Policy precedence

Contract policy is monotonic. Effective rules are resolved from immutable SDAI core defaults plus optional organization, repository, and user layers. Lower-precedence layers can strengthen requirements but cannot weaken authoritative restrictions.

The core 0.16 critical rule permits a known breaking change only when all required fresh evidence is present. For `critical`, that includes:

- architecture approval; and
- migration plan evidence.

Organization policy can make this stricter. Repository or user policy cannot remove organization/core requirements.

Policy decisions bind these identities:

- baseline contract SHA-256;
- candidate contract SHA-256;
- compatibility diff SHA-256;
- effective contract policy SHA-256; and
- engineering constitution SHA-256.

A stale or mismatched binding is rejected.

## Evidence requirements and approval authority

Contract governance reuses canonical `sdai.trace-evidence/v1` records and SDAI trace freshness evaluation. Evidence must bind current repository content and a reachable Git commit according to the configured commit policy.

Architecture approval is deliberately stronger than generic review evidence:

- evidence kind must be `approval`;
- producer semantic role must be `architecture-approver`; and
- provider/model identity must be null.

This prevents an AI provider/model from self-approving the architecture decision that enables its own breaking change. Migration-plan evidence must include an artifact content binding. Evidence that is stale, missing, blocked, failed, mismatched to the governed hashes, or produced under invalid approval authority cannot satisfy the gate.

## Contract trace integration

Each declared source becomes a stable `CONTRACT` trace node. Addressable symbols also become stable `CONTRACT` nodes:

- OpenAPI operations;
- AsyncAPI channels, operations, and messages;
- JSON Schema schema nodes; and
- Protobuf services, RPCs, messages, and fields.

Source and symbol identities are independent of provider choice. Symbol node identity is derived from the declared source id plus a stable address; semantic symbol content has its own SHA-256. Explicit feature links are declared in `specs/changes/<FEATURE>/contract-trace.yaml` using `sdai.contract-trace/v1`.

Links can target existing requirements, tasks, tests, ADRs, approvals, and typed evidence. SDAI never creates an approval node merely because a contract-trace declaration asks for one; the approval must already exist independently in canonical feature truth.

A trace declaration binds the current source hash and, for symbol-level links, the current symbol hash. Optional policy-decision provenance binds the decision, diff, and policy hashes. Changes that invalidate declared source/symbol/decision bindings produce deterministic trace gaps and therefore surface through both `sdai trace` and `sdai verify`.

## Error classes and fail-closed boundaries

0.16 errors use stable prefixes so CI and integrations can distinguish failure domains:

- `SDAI-CONTRACT-SOURCE-*` — manifest/source discovery, path, encoding, size, duplicate, and workspace-safety errors;
- `SDAI-CONTRACT-OPENAPI-*`, `SDAI-CONTRACT-ASYNCAPI-*`, `SDAI-CONTRACT-JSONSCHEMA-*`, `SDAI-CONTRACT-PROTOBUF-*` — adapter validation and compatibility findings;
- `SDAI-CONTRACT-ADAPTER-*` — missing/invalid adapter registration;
- `SDAI-CONTRACT-POLICY-*` — policy precedence, classification, evidence, constitution, and governance failures; and
- `SDAI-CONTRACT-TRACE-*` / `SDAI-TRACE-BUILD-005` — contract trace extraction, binding, decision, stale, dangling, and integration failures.

Unsafe absolute/traversal paths, symbolic-link escapes, unsupported kinds/dialects, duplicate YAML keys, invalid UTF-8, bounded-input violations, unsafe imports/references, malformed policy/evidence, unknown error-level compatibility, and stale governed evidence fail closed.

## Limitations

The 0.16 engine intentionally favors deterministic safety over broad parser/toolchain substitution:

- remote `$ref`/schema/import fetching is not part of contract truth;
- Protobuf imports resolve only from explicitly declared local Protobuf snapshots;
- adapter compatibility is conservative for semantics that cannot be proved safely;
- contract trace line provenance is source-file level for normalized parser-derived symbols rather than a source-language AST line map; and
- symbol links bind both source and symbol hashes, so a declared symbol link is conservatively stale when its source snapshot changes even if that symbol's semantic hash is unchanged.

These constraints are deliberate and can be evolved behind versioned JSON contracts without weakening current fail-closed behavior.

## Migration and backward compatibility

Existing projects without `.sdai/contracts.yaml` remain valid. No contract source/symbol nodes are introduced and existing trace/verify behavior continues unchanged. Contract Engineering is opt-in until a contract manifest is added.

A safe adoption sequence is:

1. add `.sdai/contracts.yaml` with explicit local source ids/kinds/paths;
2. run `sdai contract inspect` and `sdai contract check` until all declared sources are valid;
3. compare a proposed candidate with `sdai contract diff` using the required direction;
4. initialize/maintain `.sdai/constitution.md` and configure organization/repository contract policy where applicable;
5. for governed breaking changes, produce fresh canonical evidence and run `sdai contract gate`;
6. after an allowed candidate becomes current contract truth, add `contract-trace.yaml` links with the current source/symbol hashes and optional decision binding; and
7. run `sdai trace coverage`, `sdai trace missing`, and `sdai verify` in CI.

New manifest/configuration files are additive. Historical 0.6–0.15 compatibility/evidence gates remain enabled in the same full CI matrix.
