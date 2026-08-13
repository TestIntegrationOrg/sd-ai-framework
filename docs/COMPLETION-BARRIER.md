# SDAI 0.11 completion barrier

SDAI 0.11 treats completion as a framework decision backed by current evidence, not as an agent-provided status string.

## Contracts

- `sdai.completion-policy/v1` defines additional required evidence dimensions by risk and by `task` / `change` stage.
- `sdai.completion-barrier/v1` is the deterministic machine report for one attempted terminal transition.
- Existing `sdai.trace-evidence/v1` records remain the source for test, quality, security, and approval proof.
- Existing 0.9 execution-ledger evidence events and hash bindings remain the durable transition substrate; debugger/custom completion evidence is not replaced.

A barrier report records the feature, task/change subject, risk, exact Git commit, task attempt when applicable, required dimensions, and one explicit finding per dimension. Finding states are `valid`, `missing`, `stale`, `failed`, `blocked`, `wrong-attempt`, or `wrong-subject`.

## Built-in requirements

Task completion always requires the isolated spec-compliance and code-quality review chain. Standard risk additionally requires current test and quality proof. Critical adds security proof; regulated also adds approval proof.

Change completion always requires a current final whole-change review plus a passing `sdai verify` result. Critical adds security proof; regulated also adds approval proof.

Policy layers are combined by set union:

1. framework built-ins
2. organization policy from `SDAI_ORG_COMPLETION_POLICY_PATH`
3. repository `.sdai/completion-policy.yaml`
4. user policy from `SDAI_USER_COMPLETION_POLICY_PATH`

Because requirements are only added, repository/user configuration cannot weaken organization or framework minimums.

Example:

```yaml
apiVersion: sdai.completion-policy/v1
risks:
  critical:
    task: [approval]
    change: [approval]
```

Unknown fields, risks, stages, dimensions, malformed YAML, and symlinked policy files fail closed.

## Current-proof rules

Isolated review proof must match the current ledger attempt, persisted contract, exact Git HEAD, current context snapshot, implementation worker identity, and review predecessor chain. The implementing worker cannot satisfy an independent review requirement.

Typed evidence must have the expected kind and subject, `passed` status, exact-HEAD commit binding, and current SHA-256 content bindings. Missing, stale, blocked, failed, previous-attempt, or wrong-subject evidence never satisfies the barrier.

`complete_isolated_task(...)` first evaluates the barrier, persists the canonical report, emits completion-ready ledger evidence bound to the report/current proof, and only then appends `task.completed`.

`complete_isolated_run(...)` requires all ledger tasks complete and evaluates final review plus current verification before appending `run.completed`.

Both evaluators are deterministic/provider-neutral. Provider/model identity cannot convert stale or failing machine proof into valid completion proof.
