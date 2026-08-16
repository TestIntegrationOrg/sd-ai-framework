# Multi-Repository Run Planning and Verification

SD-AI 0.15 executes multi-repository feature work only through explicit local participant selection and a deterministic pre-mutation plan.

The run plan API is:

```text
sdai.multi-repo-run-plan/v1
```

## Planning

Inspect one repository without executing:

```bash
sdai run FEATURE-123 --repo api --plan --json --path .
```

Inspect all feature participants:

```bash
sdai run FEATURE-123 --all --plan --json --path .
```

A plan is bound to:

- the canonical multi-repository feature graph SHA-256;
- SpecificationStore resolution SHA-256 when stores are registered;
- feature-repository resolution SHA-256;
- each selected repository's Git repository identity, branch, commit, tree, and clean-status SHA-256;
- workflow and isolation mode;
- the deterministic repository execution order and branch/worktree policy.

Local absolute repository paths are retained only in the in-memory execution object and are excluded from canonical plan JSON.

## Execution

Execute one explicit participant:

```bash
sdai run FEATURE-123 --repo api --workflow standard --isolation worktree --path .
```

Execute every participant in deterministic repository order:

```bash
sdai run FEATURE-123 --all --workflow standard --isolation worktree --path .
```

The immutable plan is printed before any participant mutation. Immediately before execution, SD-AI revalidates every planned Git baseline. If a repository became dirty, changed commit/tree/branch, disappeared, or became incompatible, execution is refused before the first workflow mutation.

Actual repository execution delegates to the existing repository-local `Orchestrator` and worktree isolation implementation. Provider profiles, workflow state, approvals, quality gates, guarded workspace mutation, worktree evidence, and repository-local durable execution artifacts therefore remain local to that repository.

`--all` is sequential and fail-fast. If a participant fails, later repositories are not executed. This prevents an API failure, for example, from silently mutating UI/shared repositories that have not yet been reached.

The legacy single-repository command remains unchanged:

```bash
sdai run FEATURE-123 --workflow standard --path .
```

Multi-repository behavior is selected only with `--repo`, `--all`, or `--plan`.

## Git authority boundary

The multi-repository runner never performs hidden Git network or integration operations. It does **not**:

- clone repositories;
- discover undeclared repositories;
- fetch or pull;
- push;
- merge;
- rebase;
- auto-create a PR.

When `--isolation worktree` is used, the existing local worktree guard creates only the isolated local branch/worktree required for the selected repository. The source repository must be clean before that happens.

## Stable exit classes

Multi-repository automation uses these stable process classes:

| Code | Class | Meaning |
|---:|---|---|
| `0` | `success` | Plan/verification/execution succeeded. |
| `2` | `policy-failure` | A repository workflow or verification policy failed. |
| `4` | `drift` | Bound graph/store/ownership input is stale or ambiguous, or a planned baseline changed. |
| `5` | `participant-unavailable-or-dirty` | A required participant is missing, dirty, or incompatible. |
| `6` | `infrastructure-or-tool-failure` | Local execution/verification tooling could not complete reliably. |

The ordinary single-repository command keeps its pre-0.15 exit behavior.

## All-repository verification

Aggregate repository-local verification with:

```bash
sdai verify --all-repos --feature FEATURE-123 --risk medium --path .
```

Canonical JSON:

```bash
sdai verify --all-repos --feature FEATURE-123 --risk medium --json --path .
```

The aggregate report is bound to the same feature graph and run-plan hashes used to determine participants. Each repository is then verified locally with the existing verification engine. Reports remain separate per repository so partial states are visible rather than flattened into an invented global result.

Verification is read-only with respect to unrelated repositories. Missing/dirty participants, graph/store drift, local verification policy failures, and infrastructure failures map to the stable exit classes above.

## Resume and durable state

0.15 does not centralize or replace repository-local execution state. Each selected repository continues using the framework's existing workflow state, durable execution ledgers, resume semantics, approvals, evidence, and worktree records. Running `api`, then `ui`, then `shared` explicitly therefore resumes/supersedes work in each repository independently.

The multi-repository plan is orchestration evidence; it is not a second source of truth for repository-local workflow state.