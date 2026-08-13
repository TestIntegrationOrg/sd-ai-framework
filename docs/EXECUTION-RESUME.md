# Exact Execution Resume Semantics

Issue #93 turns the durable ledger from #92 into a deterministic restart mechanism. Resume decisions come from repository evidence and the append-only ledger, never prior chat/model output.

## Commands

```text
sdai execution status <FEATURE> --run <RUN-ID> [--json] [--path PATH]
sdai execution resume <FEATURE> --run <RUN-ID> [--json] [--path PATH]
```

`status` is read-only. `resume` may append deterministic resume events and atomically refresh the checkpoint. JSON mode emits only the versioned result document on stdout.

## Exact task order

Task order is the order of the original `task.registered` events in `events.jsonl`. It is not alphabetical order and is not reconstructed from chat history.

SDAI examines tasks in that registration order. The first task whose durable state is not a currently verified completion is the resume point.

## When a completed task may be skipped

A completed task is skippable only when all of the following remain true:

1. its recorded completion commit still exists in the current repository history and is an ancestor of current `HEAD`;
2. every persisted artifact/evidence binding still resolves to a regular non-symlink file inside the repository; and
3. every current file SHA-256 exactly matches the digest persisted on `task.completed`.

The completion commit does not need to equal current `HEAD`; later tasks normally add later commits. Rewritten history, a missing commit, a missing/symlinked evidence file, or a changed digest invalidates the skip decision.

## Dirty workspace rule

Resume requires a clean Git working tree except for this feature's own durable execution state under:

```text
specs/<FEATURE>/.sdai/execution/**
```

Uncommitted engineering/specification changes outside that path block resume. SDAI does not reinterpret a dirty workspace as a new baseline and does not invalidate completed tasks merely to make progress.

## Interrupted, failed, and stale tasks

- `registered` → resume action `dispatch`.
- `started` → resume action `resume`; the same persisted dispatch token is reused when one already exists.
- `failed` → `task.reopened` creates a new attempt, then a new dispatch reservation is created.
- `completed` with stale Git/evidence identity → `task.reopened` creates a new attempt, then a new dispatch reservation is created.
- verified `completed` → skipped.

`task.reopened` preserves the old terminal event in history but makes the current reconstructed task state `registered` again.

## Duplicate-dispatch prevention

Before execution is dispatched, SDAI appends:

```text
task.dispatch_reserved
```

with a durable `dispatch_id` and attempt number. A resumed process reuses the existing dispatch ID for the same task attempt instead of inventing another one. This gives provider/orchestrator integration a stable idempotency key.

Resume mutations use compare-and-append semantics: each append can require the exact last ledger SHA-256 observed by the planner. If another process advances the ledger first, the stale writer fails its compare operation, rebuilds the plan, and reuses the winning reservation rather than creating an independent duplicate reservation.

## Paused runs

If an interrupted run is `paused`, `sdai execution resume` appends `run.resumed` before reopening/reserving a task. Active runs do not receive a redundant `run.resumed` event.

Completed, failed, or cancelled runs are terminal and are not reopened by #93.

## Checkpoints

#92 already provides the self-hashed atomic `sdai.execution-checkpoint/v1` format. #93 stores resume cursor data in its `extra.resume` section, including:

```text
plan SHA-256
resume task/action
dispatch ID + reuse flag
current Git HEAD
exact task registration order
```

A current checkpoint may be trusted only when it exactly matches the current ledger sequence/hash/state. A checkpoint that is merely behind the valid ledger is treated as stale after a crash and replaced from reconstructed ledger truth. A corrupt or tampered checkpoint still fails closed.

## Process-crash behavior

After a process interruption:

1. validate the full #92 event hash chain;
2. reconstruct task states;
3. read original task registration order;
4. verify current Git and bound evidence for completed tasks;
5. identify the exact first non-skippable task;
6. resume/reopen it when necessary;
7. reuse or reserve its dispatch idempotency token; and
8. atomically write a checkpoint matching the new ledger head.

No previous model message is consulted to decide what completed.

## Versioned interfaces

#93 adds:

- `sdai.execution-resume-plan/v1`
- `sdai.execution-resume-result/v1`
- ledger event `task.reopened`
- ledger event `task.dispatch_reserved`
- compare-and-append via expected last event SHA-256.

The resume planner is provider-independent. Provider execution/heartbeat integration can consume the durable dispatch token in subsequent 0.9 work without changing the resume truth model.
