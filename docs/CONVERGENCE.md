# Bounded Deterministic Convergence

`sdai converge` turns current `sdai verify` gaps into durable remediation briefs. It does not execute an agent in this slice. The framework owns the loop boundary, task identity, source-truth protections, resume semantics, and escalation decisions.

## Command

```text
sdai converge FEATURE [--risk trivial|standard|critical|regulated] [--max-rounds 3]
sdai converge FEATURE --json
```

Exit codes:

- `0` — current verification passed (`verified`);
- `3` — deterministic remediation work exists (`action-required`);
- `2` — the framework escalated (`max-rounds`, `no-progress`, or `non-remediable`);
- `1` — state/verification/operational validation failed.

## State and task contracts

Convergence stores framework state under:

```text
.sdai/convergence/<FEATURE>/state.json
.sdai/convergence/<FEATURE>/tasks/REMEDIATE-<id>.json
```

Contracts are versioned as:

- `sdai.convergence-state/v1`
- `sdai.remediation-task/v1`

Every round binds the current Git commit, verification report SHA-256, verification input SHA-256, actionable-finding signature, task identities, and any non-remediable blocker. Every task binds the exact verification report/input and canonical finding SHA-256 that produced it.

No timestamps or random IDs are used. Round/task identities are deterministic from current verification truth.

## Idempotency and resume

If the current verification `input_sha256` matches the latest convergence state, `sdai converge` returns the existing state without rewriting it or creating duplicate tasks.

If the Git/input state changes, verification is rerun:

- if verification passes, convergence records `verified`;
- if the same actionable finding signature remains after the input changed, convergence records `no-progress` and escalates instead of creating duplicate work;
- if the configured round bound has already been consumed, convergence records `max-rounds`;
- if any blocker is policy-classified as non-remediable, convergence records `non-remediable`.

Corrupt/tampered state or task files fail closed; the framework never silently resets a damaged convergence ledger.

## Requirements cannot be rewritten to match implementation

Every remediation task explicitly forbids:

```text
specs/changes/<FEATURE>/requirements.md
specs/current
```

Allowed roots depend on remediation kind:

- `implementation` — source/tests plus plan/task/test implementation artifacts;
- `test` — source/tests and test artifacts;
- `architecture` — source plus architecture/ADR/contract artifacts;
- `security` — source/tests plus security artifacts;
- `contract` — source/tests plus contract artifacts;
- `review` — only `.sdai/verification/<FEATURE>/reviews`.

Stale/changed requirement source truth, approval-policy blockers, missing NFR truth, and similar authority-bound findings are not converted into implementation tasks. They escalate for human/governed resolution.

## Relationship to isolated task agents

0.11.3 produces durable minimal remediation contracts only. 0.11.4 consumes those contracts to launch fresh isolated task-agent invocations and independent reviews. Keeping the convergence planner provider-free means the same current verification state always produces the same remediation plan regardless of which model will later execute it.
