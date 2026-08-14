# Workflow Engine 2 — Durable bounded execution

SDAI 0.14 executes a resolved `sdai.workflow-graph/v2` through `execute_workflow_graph(...)`. The engine uses the existing 0.9 execution ledger as its only durable authority: graph execution does not introduce a second state store.

## Execution contract

The executor accepts a `WorkflowGraphResolution`, an `ExecutionLedger`, an optional leaf callback, and an optional cancellation callback. It returns `sdai.workflow-execution-result/v2` with a canonical result hash, normalized run status, and deterministic node records.

Supported control nodes are:

| Node | Runtime behavior |
|---|---|
| `sequence` | Executes children in declared order. |
| `if` | Evaluates the safe expression and executes one named branch. |
| `switch` | Selects the first matching case or the default branch. |
| `parallel` | Uses deterministic bounded scheduling and rejects writable subtrees. |
| `fan-out` | Expands a finite list into stable item identities and preserves input order. |
| `fan-in` | Collects completed source outputs in declared source order. |
| `foreach` | Executes a finite list with stable item identities. |
| `bounded-while` | Re-evaluates its condition and fails if it remains true at `max_iterations`. |

Parallel and fan-out nodes currently use deterministic single-process scheduling within their declared maximum. This preserves partial-branch recovery and stable evidence while honoring the bound. A branch containing an agent in `workspace-write` mode, a write-enabled safe command, or a plugin is rejected under a concurrent node until SDAI has an explicit governed write-permit strategy.

## Durable identities and evidence

The execution plan hash binds the workflow name, graph SHA-256, and resolved input SHA-256. Each executable leaf task ID additionally binds its structural execution identity and canonical context hash. Fan-out/foreach identities contain the input index and item hash; loop identities contain a zero-padded iteration. Repeated equal items therefore remain distinct without losing deterministic ordering.

Before invoking a leaf, the ledger records `task.registered` and `task.started`. The dispatch token is derived from the task ID and attempt number. A process crash before outcome persistence reuses that token on resume, allowing the leaf adapter to apply idempotency. Successful, failed, and cancelled terminal leaf outcomes are stored as `sdai.workflow-leaf-evidence/v2`, SHA-256-bound through the ledger, and completed with the run baseline commit.

Completed leaves are never invoked again while their evidence binding remains current. Resume verifies the evidence file and ledger binding before loading its outcome. Intermediate retry failures are append-only ledger evidence events; a later attempt receives a new deterministic dispatch token.

## Checkpoints and resume

After every node, the executor writes `sdai.workflow-execution-checkpoint/v2` into the existing ledger checkpoint's `extra.workflowEngine2` field. The checkpoint includes the graph/input/plan hashes and canonical node records. Reopening an already completed run reconstructs the same result from this verified checkpoint without executing a leaf.

Paused approval or quality-gate outcomes append `run.paused`. A subsequent call appends `run.resumed` and invokes the unresolved decision leaf again with the same dispatch identity. If graph or resolved input changes while a run is paused, incomplete tasks from the previous plan are completed with cancellation evidence before the new plan proceeds. This prevents stale work from authorizing downstream execution and keeps ledger completion rules intact.

Cancelled, failed, blocked, paused, and succeeded results are normalized. Cancellation and failure are terminal ledger states. A raw exception from a leaf adapter represents process loss and is intentionally allowed to escape: the started task remains durable and can be resumed safely.

## Bounds and failure behavior

Graph validation and execution both fail closed:

- item counts must not exceed `max_items`;
- loop evaluation must terminate within `max_iterations`;
- parallel/fan-out concurrency must be between 1 and 32;
- fan-in sources must resolve uniquely and have completed output;
- all values persisted or hashed by the engine must be finite JSON;
- terminal runs cannot be resumed, except that an already completed matching plan can be read idempotently.

The default leaf adapter executes only deterministic and validation leaves. Approval and quality-gate leaves pause; agent, plugin, and safe-command leaves block until a governed adapter is supplied. Operational adapters remain responsible for enforcing the normalized leaf contract and the shared execution policy boundary.
