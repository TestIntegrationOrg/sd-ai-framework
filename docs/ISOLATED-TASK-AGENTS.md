# Fresh Isolated Task Agents and Independent Review

SDAI 0.11 executes convergence remediation through **durable minimal task contracts**, not inherited chat/session history. Each task attempt has a fresh deterministic invocation identity and a bounded context assembled by the deterministic framework.

## Contracts

The execution chain uses three versioned contracts:

- `sdai.isolated-task/v1` — immutable task/stage/context contract;
- `sdai.isolated-invocation/v1` — resolved semantic agent/profile/provider invocation identity;
- `sdai.isolated-result/v1` — recorded stage output/status bound to that invocation.

Framework state lives under:

```text
.sdai/isolated/<FEATURE>/<TASK>/attempt-<N>/<STAGE>/
```

The implementation task contract binds:

- convergence remediation task SHA-256;
- verification report/input SHA-256 indirectly through that remediation task;
- round ID, task ID, attempt, current Git commit, and durable dispatch ID;
- semantic agent/capability/execution mode;
- explicit allowed and forbidden roots;
- exact source/line context windows with full-file SHA-256 bindings.

The task prompt states that this bounded contract is the **complete task context**. `AgentRuntime.build_explicit_context_invocation(...)` does not call normal feature-context collection, so a fresh task worker does not inherit unrelated feature artifacts or conversation history.

## Stage chain

Task stages are ordered:

```text
implement (developer)
  ↓
spec-compliance-review (code-reviewer)
  ↓
code-quality-review (code-reviewer)
```

After every remediation task passes those stages, SDAI may create:

```text
final-change-review (code-reviewer)
```

Each stage receives a different deterministic invocation ID. Review stages carry the worker invocation ID explicitly and cannot reuse the worker semantic role or invocation identity.

The official independent review preparation path adds only bounded durable evidence:

- original remediation provenance windows;
- persisted worker output;
- current Git diff relative to the implementation contract;
- for code-quality review, the prior independent spec-review output.

Generated review context is materialized under `.sdai/isolated/**` with exact SHA-256 bytes so freshness checks can reconstruct and revalidate it. Final whole-change review receives an aggregate of individually accepted task result hashes plus a bounded baseline-to-current Git diff.

## Execution and resume

Implementation dispatch reuses the existing 0.9 execution ledger and `resume_execution(...)` reservation semantics. The remediation task is registered into the ledger with its exact convergence hashes and its canonical task brief.

If a task is interrupted after dispatch/start, the same current-attempt dispatch and already-persisted task contract are reused. The framework does not rescan feature context and create a different prompt on retry.

Stage results are persisted separately and mirrored into existing ledger events:

- `task.implementation`
- `task.review`

The event payload records stage, attempt, contract SHA, invocation ID, semantic agent, status, result SHA, worker invocation ID, and predecessor review invocation IDs. Re-recording the exact same invocation/result is idempotent; a conflicting result for the same invocation fails closed.

## Write security

Implementation execution uses two nested boundaries:

1. the existing `AgentRuntime` / `WorkspaceMutationGuard`, which enforces framework and organization protected paths;
2. `AllowedRootsMutationGuard`, which enforces the **stricter per-task allowlist** from the remediation contract.

The second guard cannot weaken the first. If a worker modifies any path outside its task allowlist, or touches an explicitly forbidden root, SDAI restores the entire invocation's file transaction and raises a boundary violation.

Every convergence-derived task forbids at minimum:

```text
specs/changes/<FEATURE>/requirements.md
specs/current
```

Therefore an implementation agent cannot rewrite requirements/current specification truth to make verification match its implementation.

## Context freshness

Immediately before provider execution, file-backed context SHA-256 values are revalidated. If a bound requirement/source artifact changed after task dispatch, execution fails as stale rather than silently using an outdated task contract.

Generated `.sdai/isolated/**` review context is itself durable and hash-bound. This distinguishes intentional resume of the exact interrupted contract from silently rebuilding context after source truth changed.

## Result status is not completion truth

`IsolatedStageStatus` records the execution/review-chain result (`recorded`, `passed`, `failed`, `blocked`). It is not by itself authorization to complete a task or feature. The next 0.11 slice (#122) binds required review/test/security/verification evidence to the current attempt/Git/content state and makes those evidence checks mandatory at ledger completion transitions.

This separation prevents a worker or provider from converting its own stage output into framework completion truth merely by returning “passed.”
