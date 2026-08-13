# Debugger Semantic Role and Durable Root-Cause Evidence

Issue #94 adds a provider-neutral `debugger` semantic agent and a deterministic evidence contract for root-cause debugging.

## Semantic role

Canonical agent:

```text
.sdai/agents/debugger.agent.md
```

The role is always `debugger`. It composes existing lifecycle capabilities (`coding`, `testing`, `review`) with the `systematic-debugging` skill. Provider/model choice may change how the role executes, but it does not change the semantic identity, responsibilities, or evidence schema.

## Systematic-debugging skill

The skill requires the sequence:

```text
reproduce
  -> observe
  -> formulate falsifiable hypothesis
  -> run discriminating experiment
  -> establish root cause
  -> implement causal fix
  -> prove regression coverage
```

A symptom disappearing is not root-cause evidence. If evidence is insufficient, the debugger must record the missing signal and continue investigation rather than claim completion.

## `sdai.debug-record/v1`

The deterministic record contains:

```text
feature/run/task identity
semantic_role = debugger
status
reproduction
observations
hypotheses
experiments
root_cause
fix
regression_evidence
producer metadata
```

Producer metadata may record provider/model, but `semantic_role` and `producer.agent` must remain `debugger`.

### Cross-reference rules

- hypothesis observation IDs must resolve to recorded observations;
- experiment hypothesis IDs must resolve to recorded hypotheses;
- root-cause evidence IDs must resolve to an observation or experiment;
- fix file paths must be repository-relative POSIX paths without traversal;
- IDs are unique within each record family.

## Completion-ready record

An investigation record may be persisted while status is still `investigating`. A completion-ready debugger record additionally requires:

1. `status: fixed`;
2. a root cause with `confidence: confirmed`;
3. at least one supported hypothesis;
4. at least one experiment whose conclusion supports a hypothesis;
5. a recorded fix and affected file(s);
6. at least one regression-evidence item; and
7. all regression evidence marked `passed`.

The deterministic validator, not provider prose, decides whether these conditions are satisfied.

## Durable execution integration

`register_debugger_task()` registers the task with:

```text
semantic_role: debugger
required_completion_evidence:
  - sdai.debug-record/v1
```

This uses a generic ledger mechanism introduced by #94. Any task may declare `required_completion_evidence`; the core ledger does not hardcode debugger-specific behavior.

For a required evidence contract to authorize `task.completed`, the **current task attempt** must contain a `task.evidence` event where:

- `evidence_contract` matches a declared requirement;
- `completion_ready` is `true`;
- the evidence event contains a durable hash binding; and
- the same exact binding is carried into the `task.completed` event.

Evidence from a prior attempt does not satisfy a reopened task.

The existing #92 completion-binding check then verifies those bound files still exist as current regular non-symlink repository files with the exact declared SHA-256 at completion time. #93 can revalidate the same binding later when deciding whether a completed debugger task is still safe to skip on resume.

## Evidence persistence

`persist_debug_record()` stores the normalized record through the #92 task evidence file:

```text
specs/<FEATURE>/.sdai/execution/<RUN>/tasks/<TASK>/evidence.json
```

and appends `task.evidence` containing the evidence contract, record SHA-256, semantic role, completion-readiness flag, and evidence-file binding.

`complete_debugger_task()` requires a completion-ready record, persists it, then appends `task.completed` with the debug-evidence binding plus any implementation artifact bindings.

## Behavioral evals

#94 includes deterministic pressure evals for both:

- `systematic-debugging` skill; and
- `debugger` semantic agent.

The candidate behavior must improve over a patch-first baseline by requiring reproduction, hypothesis/experiment evidence, root cause, regression coverage, and the durable `sdai.debug-record/v1` contract.

## Trust boundary

AI/provider responsibilities:

- propose observations/hypotheses;
- run or recommend experiments;
- explain root cause;
- implement a fix;
- produce structured evidence.

Deterministic SDAI responsibilities:

- validate the record schema and cross references;
- decide completion readiness;
- bind the record to durable execution evidence;
- enforce declared completion-evidence contracts;
- reject previous-attempt or non-completion-ready evidence;
- preserve provider-neutral semantic role identity.
