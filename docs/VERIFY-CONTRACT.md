# SDAI Hybrid Verification Contract

SDAI 0.11 separates **machine-owned verification truth** from **semantic review evidence**. The deterministic framework owns whether current artifacts, trace links, tests, security evidence, approvals, hashes, and execution state are valid. Semantic reviewers contribute bounded evidence about questions that require judgment, but their provider output cannot override deterministic failures.

This slice defines contracts only. `sdai verify` orchestration is added in the next 0.11 slice.

## Contracts

### `sdai.semantic-review/v1`

A semantic review is a typed wrapper around existing `sdai.trace-evidence/v1` evidence with `kind: review`.

Supported review dimensions are:

- `requirement-satisfaction`
- `architecture-intent`
- `failure-behavior`
- `undocumented-behavior`

The nested evidence must:

- use `kind=review`;
- use status `passed`, `failed`, or `blocked`;
- have the same `review_id` / `evidence_id` and subject as the wrapper;
- contain at least one SHA-256 content binding;
- bind a Git commit and repository-relative source/test/artifact/evidence bytes;
- carry source/line provenance;
- keep semantic role/provider/model producer metadata separate from evidence truth.

The semantic wrapper has two hashes:

- `truth_sha256` commits the dimension, subject, conclusion, summary, and nested **evidence truth hash**;
- `sha256` commits the full stored record including nested producer metadata.

Changing only provider/model therefore changes the storage record hash but does **not** change semantic review truth.

### `sdai.verify-report/v1`

A verification report is a deterministic canonical snapshot containing:

- feature ID;
- current Git commit identity;
- caller-provided aggregate `input_sha256` binding the verified input state;
- typed verification findings;
- current semantic-review states;
- derived outcome and canonical report SHA-256.

Findings distinguish:

- source: `deterministic` or `semantic`;
- category: artifact freshness, analysis, trace coverage, task/execution state, test/quality/security/approval/contract/current state, or one of the semantic review dimensions;
- severity: `blocking`, `review`, `warning`, `info`;
- status: `pass`, `fail`, `blocked`, `missing`, `stale`, `review-required`;
- source/line provenance;
- optional subject and metadata.

A semantic finding is invalid unless it references a canonical semantic evidence `truth_sha256`.

## Outcome rule

Verification outcomes are monotonic over the complete finding set:

1. any unresolved `blocking` finding → `blocked`;
2. otherwise any unresolved `review` finding → `review`;
3. otherwise → `passed`.

There is no score, vote, provider confidence, or last-writer-wins mechanism that can cancel a blocking finding. A semantic pass can coexist with a deterministic failure, but the report remains blocked.

## Freshness

`evaluate_semantic_review_freshness(...)` delegates to the 0.10 typed-evidence freshness engine. A semantic review satisfies current verification only when:

- its review status is `passed`;
- its evidence commit is valid under the configured commit policy;
- every bound artifact/source/test/evidence SHA-256 still matches current bytes;
- any bound 0.8 artifact state is current rather than stale/missing/blocked.

Changed source/test/artifact bytes, rewritten/disconnected Git history, failed review evidence, or blocked evidence therefore cannot satisfy current verification.

## Trust boundary

The deterministic engine does not execute or trust an AI provider merely because a semantic record exists. Provider/model metadata is provenance, not truth authority. Later `sdai verify` orchestration consumes these contracts and decides which semantic dimensions are required for a given feature/risk.

Implementation/convergence agents may not rewrite requirements merely to make verification agree with implementation. That invariant is enforced in the later convergence slice, while this contract ensures semantic evidence cannot disguise machine-detectable disagreement.

## Safety and portability

The contracts use:

- canonical finite JSON;
- repository-relative POSIX evidence paths;
- source/line provenance;
- strict UTF-8;
- SHA-256 content identities;
- safe argv evidence from the underlying typed-evidence record;
- symlink/path-boundary rejection when loading semantic-review files.

The verification truth contract contains no provider-specific role identity and is designed for the existing Ubuntu/Windows × Python 3.11/3.12 compatibility matrix.
