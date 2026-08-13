# Durable Execution Ledger

Issue #92 establishes provider-independent execution truth for long-running SDAI work. The ledger is designed to survive process restart, context compaction, and chat loss without treating model memory as task state.

## Location

Each run is stored beneath the feature workspace:

```text
specs/<FEATURE>/.sdai/execution/<RUN-ID>/
├── run.json
├── events.jsonl
├── checkpoint.json        # optional
├── ledger.lock            # persistent anchor; OS lock ownership is transient
└── tasks/
    └── <TASK-ID>/
        ├── brief.md
        ├── implementation.json
        ├── review.json
        └── evidence.json
```

Run/task IDs are independent of provider, model, and chat identity.

## Versioned contracts

The first implementation defines:

- `sdai.execution-run/v1`
- `sdai.execution-event/v1`
- `sdai.execution-state/v1`
- `sdai.execution-checkpoint/v1`
- `sdai.execution-implementation/v1`
- `sdai.execution-review/v1`
- `sdai.execution-evidence/v1`

The run manifest binds:

```text
run id
feature id
workflow
baseline Git commit
creation time
```

`run.created` additionally binds the exact `run.json` bytes and the semantic manifest SHA-256. Loading a run rechecks both, so rewriting `run.json` after creation invalidates the run.

## Append-only event chain

`events.jsonl` contains one canonical JSON event per line. Every event records:

```text
monotonic sequence
stable event id: <run-id>:<8-digit sequence>
run + feature identity
event kind
task id when applicable
UTC timestamp
optional Git commit
artifact/evidence SHA-256 bindings
structured JSON payload
previous event SHA-256
current event SHA-256
```

The first event uses an all-zero previous hash. Every later event must reference the exact SHA-256 of the previous event body.

Before any append SDAI reads and validates the entire existing chain. A sequence gap, duplicate sequence, identity mismatch, previous-hash mismatch, event-content hash mismatch, malformed JSON, invalid UTF-8, blank record, or truncated final record fails closed.

The append path uses literal filesystem operations, `O_APPEND`, complete-write loops, and `fsync`. It does not rewrite prior lines.

## Cross-process serialization

A per-run `ledger.lock` is a persistent lock anchor. SDAI acquires an OS advisory lock on that anchor before reads that must be consistent with a following mutation and before append/checkpoint operations. The current v1 lock is fail-closed: another live lock owner blocks mutation rather than allowing two processes to assign the same sequence. File existence alone never means the ledger is locked.

OS lock ownership is released automatically when a process exits, including hard process termination, so the persistent anchor does not brick restart. If a process is terminated in the middle of an append, restart validation checks the JSONL boundary/hash chain before any state is reconstructed. A partial final record therefore cannot silently become completion truth.

## Run state machine

Supported run events:

```text
run.created
run.paused
run.resumed
run.completed
run.failed
run.cancelled
```

`run.created` must be first. A paused run must explicitly emit `run.resumed` before additional task activity. Completed/failed/cancelled runs are terminal.

A run cannot emit `run.completed` while any registered task is not completed.

## Task state machine

Supported task events:

```text
task.registered
task.started
task.implementation
task.review
task.evidence
task.completed
task.failed
```

A task must be registered before it can start. Implementation/review/evidence/completion/failure events require a started task. Completed and failed are terminal; conflicting or duplicate terminal events are rejected.

`task.completed` requires:

1. a concrete Git commit identity; and
2. at least one persistent `artifact` or `evidence` SHA-256 binding whose current regular non-symlink file exists and matches the declared digest at completion time.

A chat message, provider response, forged digest, missing file, or plain `"done"` flag cannot create durable completion truth. Historical reconstruction does not re-read those files; #93 performs current-evidence freshness checks again before resume may skip work.

## Task records

Task records are deterministic files outside the model/chat context:

- `brief.md` — UTF-8 text normalized to LF.
- `implementation.json` — structured implementation report.
- `review.json` — structured review report.
- `evidence.json` — structured verification/evidence report.

Writes are atomic. On POSIX, the parent directory is also fsynced after replacement so the new directory entry is durable across power loss; Windows keeps the atomic replacement without unsupported directory fsync. Record writes are rejected after the task becomes terminal.

`binding_for_file()` creates a repository-relative POSIX source + SHA-256 binding for a regular non-symlink file inside the SDAI project.

## Checkpoints

A checkpoint is an atomic snapshot containing:

```text
run + feature identity
last ledger sequence
last event SHA-256
fully reconstructed execution state
optional structured cursor/context
checkpoint SHA-256
```

Loading a checkpoint validates its own SHA-256 and then reconstructs the event ledger. The checkpoint is accepted only if sequence, last hash, and full state exactly match the current ledger. A stale or modified checkpoint never overrides the event chain.

## Restart behavior in #92

After process restart:

```python
ledger = load_execution_run(root, feature, run_id)
state = ledger.reconstruct()
```

SDAI validates `run.json`, the full event hash chain, event transitions, and then reconstructs run/task terminal state deterministically. No chat history is required.

## Important boundary: current-evidence freshness belongs to #93

#92 persists the Git commit and artifact/evidence hashes that justified `task.completed`. It does **not** yet decide that those bindings are still current enough to skip a task during resume.

#93 will add resume planning that revalidates:

- current Git identity against the recorded completion commit policy;
- every bound artifact/evidence file against its recorded SHA-256;
- checkpoint/ledger freshness;
- exact task registration order.

If those current bindings no longer match, #93 must invalidate the skip decision and resume/re-run the affected task. This separation keeps #92 focused on durable historical truth while #93 owns current resume eligibility.

## Corruption and truncation philosophy

A valid prefix may reconstruct an earlier non-complete state if later complete lines were wholly removed. It must never reconstruct completion from a corrupt/truncated line. Hash chaining also detects edits or reordering inside the retained chain.

This v1 ledger is integrity-checked but not cryptographically signed against a malicious repository administrator; signed/immutable enterprise provenance is a later roadmap milestone.

## Security and portability

- paths remain inside the project root;
- existing symlink components are rejected for ledger/task evidence paths;
- binding paths use repository-relative POSIX notation;
- JSON rejects non-finite values and unsupported types;
- event/run IDs use restricted portable characters;
- no shell execution or provider dependency is introduced;
- tests cover Windows/Linux, partial writes, locks, truncation, tampering, checkpoint staleness, and UTF-8 content.
