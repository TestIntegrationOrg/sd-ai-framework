# Hybrid Feature Verification

`sdai verify` is SDAI's read-only current-state verifier. It deliberately does **not** invoke an AI provider. Deterministic checks run first; semantic conclusions are consumed only from already-recorded, current `sdai.semantic-review/v1` evidence.

## Command

```text
sdai verify FEATURE [--risk trivial|standard|critical|regulated]
sdai verify FEATURE --risk critical --json
```

Exit codes:

- `0` — verification passed;
- `2` — one or more blocking findings exist;
- `3` — deterministic truth is not blocked, but current semantic review is still required/stale;
- `1` — repository, contract, evidence, or operational validation failed.

Machine output is the canonical `sdai.verify-report/v1` contract.

## Deterministic inputs

The verifier composes existing framework-owned truth rather than reimplementing it:

- 0.8 artifact freshness (`fresh`, `stale`, `missing`, `blocked`);
- 0.9 deterministic cross-artifact analysis findings;
- 0.9 typed execution evidence when present;
- 0.10 canonical trace graph and unresolved gaps;
- 0.10 risk-based trace policy and current evidence freshness.

Required artifact freshness failures, blocking analysis findings, unresolved trace gaps, trace-policy failures, and current failed execution evidence remain deterministic blockers.

## Semantic review requirements

Semantic reviews are stored as framework state under:

```text
.sdai/verification/<FEATURE>/reviews/*.json
```

The directory is read only by `sdai verify`; the verifier never creates or refreshes review evidence.

Required dimensions are intentionally risk-sensitive:

| Risk | Required semantic review |
|---|---|
| `trivial` | requirement satisfaction for each declared requirement |
| `standard` | requirement satisfaction for each requirement + feature failure behavior |
| `critical` | standard + architecture intent + undocumented behavior |
| `regulated` | standard + architecture intent + undocumented behavior |

Requirement satisfaction subjects use canonical trace node IDs such as `requirement:FR-001`. Whole-change review subjects use `feature:<FEATURE>`.

A missing or stale required review produces outcome `review`. A current `failed` or `blocked` semantic review is a blocking finding. A current passed semantic review is evidence-backed by its provider-independent semantic truth SHA-256.

## Provider boundary

`verify_feature(...)` contains no provider runtime call and does not choose a model. Producer/provider/model metadata is retained in semantic evidence storage but excluded from semantic truth and verification report identity. Re-recording the same semantic truth with another allowed provider/model therefore does not change the canonical verification report.

Provider execution, review generation, routing, retries, and isolated task agents belong to later 0.11 slices. This separation prevents a provider response from becoming framework truth merely because verification asked for it.

## Input identity

Each report includes `input_sha256`, a canonical hash of:

- current Git commit;
- selected risk;
- artifact-state report;
- deterministic analysis report;
- canonical trace graph hash and gaps;
- effective trace-policy report;
- semantic review freshness/truth states;
- typed evidence freshness states.

The report therefore changes when relevant current truth changes, while provider/model-only provenance changes do not change verification truth.

## Safety

Verification is byte-for-byte read only. Semantic review discovery rejects malformed JSON, symlinks, path escapes, stale bindings, and invalid contract records. Existing trace/artifact evidence validators retain their own fail-closed behavior.
