# Trace Evidence Freshness and Invalidation

SDAI 0.10 treats evidence validity as a current-repository-state question. A historically valid evidence record is not automatically valid proof for the current checkout.

## Proof states

Every evaluated evidence record resolves to one of four states:

- `valid` — Git policy is satisfied, all bound bytes still match, relevant artifact state is fresh, and the evidence result is not failed/blocked.
- `stale` — the evidence commit is not acceptable or at least one bound file/artifact changed.
- `missing` — a required bound file or the durable evidence record no longer exists.
- `blocked` — the evidence record explicitly reports blocked/failed proof, or a bound 0.8 artifact is blocked.

Only `valid` evidence can satisfy current trace coverage.

## Git commit policy

The default policy is `ancestor`:

1. Evidence recorded at the current `HEAD` is eligible.
2. Evidence from an ancestor commit remains eligible only if every content binding still matches current repository bytes and all other freshness checks pass.
3. A commit that exists but is no longer an ancestor of current `HEAD` is stale. This covers rewritten/disconnected history.

`exact-head` is available for stricter environments. Under that policy, evidence must be recorded at the current `HEAD` even if all bound bytes are unchanged.

Git is executed with argv, `shell=False`, and the repository root as the working directory. Unexpected Git failures fail closed.

## Content revalidation

Each `sdai.trace-evidence/v1` binding is re-read from its repository-relative path and SHA-256 is recomputed from current file bytes.

- changed source => stale
- changed test => stale
- deleted test/source/artifact => missing
- changed contract/artifact => stale
- symlink/unsafe path => fail closed

This same mechanism applies to durable 0.9 execution-ledger files when they are referenced through an `evidence` binding: the durable ledger bytes are the bound truth and changing them invalidates the trace proof.

## 0.8 artifact-state integration

For `artifact` bindings, an optional `ArtifactStateReport` adds lifecycle semantics beyond raw bytes. If the matching artifact path is reported `stale`, `missing`, or `blocked`, the trace evidence inherits that invalid state even when the artifact bytes themselves have not changed.

This captures definition/dependency/evidence changes already modeled by SDAI 0.8 instead of duplicating its artifact DAG logic.

## Coverage propagation

`evaluate_trace_coverage(...)` projects freshness reports onto canonical `evidenced-by` edges without mutating the trace graph.

A graph edge with no current freshness report is treated as missing proof. A stale, missing, or blocked report remains visible but cannot satisfy coverage. This prevents historical graph links from being mistaken for current proof.

## APIs

```python
from sdai.trace_freshness import (
    CommitPolicy,
    evaluate_trace_coverage,
    evaluate_trace_evidence_file,
    evaluate_trace_evidence_freshness,
)

report = evaluate_trace_evidence_file(
    project_root,
    "specs/changes/SIGN-123/evidence/tests.json",
)

strict = evaluate_trace_evidence_freshness(
    project_root,
    validated_record,
    commit_policy=CommitPolicy.EXACT_HEAD,
)

coverage = evaluate_trace_coverage(graph, {report.evidence_id: report})
```

## Missing vs corrupt records

A requested evidence path that does not exist produces an explicit `missing` report. A record that exists but is malformed, tampered, invalid UTF-8, or unsafe fails closed rather than being downgraded to ordinary missing proof.

## Error families

- `SDAI-TRACE-FRESH-001`: Git state/policy evaluation failures.
- `SDAI-TRACE-FRESH-002`: unsafe/unreadable content-binding failures.
- `SDAI-TRACE-FRESH-003`: invalid evidence input/path/record failures.
- `SDAI-TRACE-FRESH-004`: invalid graph-to-evidence coverage projection.
