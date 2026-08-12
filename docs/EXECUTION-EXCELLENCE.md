# Execution Excellence Skill Pack

SDAI 0.7 adds a provider-neutral execution-discipline pack that complements specification, architecture, security, policy, approval, and quality-gate controls.

The behavioral ideas are familiar from disciplined engineering workflows—including test-first implementation, evidence-led debugging, and proof before completion—but the implementation is native to SDAI's semantic-role, skill resolver, policy, eval, and workflow contracts.

```text
approved requirements / architecture / policy
                    ↓
          semantic lifecycle role
                    +
       execution-excellence skills
                    ↓
       provider-neutral instructions
                    ↓
      governed provider execution
                    ↓
 deterministic tests / gates / evidence
```

The skills do not create new provider-specific or language-specific agents.

## Pack

```yaml
apiVersion: sdai/v1
kind: Pack
metadata:
  id: sdai-execution-excellence
  version: 0.1.0
spec:
  type: execution
  skills:
    - implementation-planning
    - test-driven-development
    - systematic-debugging
    - verification-before-completion
```

Full remote pack installation, publisher trust, signing, catalogs, and lockfiles remain later roadmap work. In this slice the pack is a validated built-in composition asset.

## 1. Precise implementation planning

SDAI strengthens the existing `implementation-planning` skill rather than creating a duplicate planning concept.

A useful implementation plan must:

- trace material tasks to approved requirements, architecture/contracts, or an explicit discovery gap;
- name affected components and, when repository evidence makes them known, files/symbols or artifact paths;
- make dependency order and real parallelism explicit;
- state implementation constraints and expected tests;
- include the verification command/evidence that will prove each material task;
- include migration, security, observability, rollout, rollback, compatibility, and documentation work when applicable;
- use explicit blockers/TBDs instead of inventing missing decisions.

The plan does not become authority to modify canonical requirements or architecture. Those changes remain governed lifecycle operations.

## 2. Test-driven development

`test-driven-development` uses a practical red-green-refactor loop:

1. derive the smallest observable behavior from an approved requirement or reproduced defect;
2. create or identify a focused failing test;
3. **run it and confirm it fails for the intended reason**;
4. make the smallest production change that satisfies that behavior;
5. run the focused test and relevant surrounding suite/gates;
6. refactor only while keeping the evidence green.

TDD is not treated as ritual. When legacy code has no deterministic test seam, the skill first establishes the cheapest characterization/reproduction boundary. It must not fabricate a test result or claim test-first evidence that was not actually produced.

Changing a test to weaken assertions or coverage is itself a behavior change requiring review; it is not a shortcut to green CI.

## 3. Systematic debugging

`systematic-debugging` prevents random-edit debugging loops.

The expected sequence is:

```text
reproduce
   ↓
capture exact evidence
   ↓
classify/narrow failure boundary
   ↓
one falsifiable hypothesis
   ↓
least-invasive experiment
   ↓
root cause / causal chain
   ↓
smallest root-cause fix
   ↓
regression test + surrounding verification
```

When evidence is insufficient, the correct next step is to collect the next discriminating signal—not to stack speculative changes, increase arbitrary timeouts, disable security checks, or retry until CI happens to turn green.

## 4. Verification before completion

`verification-before-completion` encodes a simple rule:

> A completion claim is an evidence claim.

Before saying work is done, an agent should determine and execute the checks relevant to the changed surface and policy, such as:

- focused and surrounding tests;
- build/compile checks;
- static analysis and linting;
- security/supply-chain gates;
- contract/schema validation;
- generated artifact validation;
- required SDAI quality gates.

The agent must inspect exit status and material output. Running a command is not the same as proving it passed.

Status must remain precise:

```text
passed
not run
not available
blocked
failed
```

An unrun check must never be converted into “should pass” or “looks correct.” AI confidence is not deterministic evidence.

## Resolver behavior

These skills are technology-neutral:

```yaml
compatibility: {}
compatible_agents: []
```

They compose with language/framework/platform skills from the same #59 resolver instead of replacing them.

Automatic selection is task/capability-aware. Examples:

- planning/migration work → `implementation-planning`;
- implementing/fixing behavior → `test-driven-development`;
- debugging a failure/regression → `systematic-debugging`;
- review/completion/readiness verification → `verification-before-completion`.

Organizations that want stronger guarantees can make skills mandatory through effective SDAI policy.

## Policy example

`examples/policies/execution-excellence.yaml` demonstrates additive mandatory skill policy:

```yaml
skills:
  required:
    planning:
      - implementation-planning
    coding:
      - test-driven-development
      - verification-before-completion
    review:
      - verification-before-completion
    testing:
      - verification-before-completion
```

This is an example, not an automatic modification of organization or repository policy. Existing effective-policy precedence still applies, including mandatory-skill union semantics where stronger organization requirements cannot be removed by lower layers.

`systematic-debugging` remains task-selected in the initial example rather than mandatory on every coding prompt.

## Workflow example

`examples/workflows/execution-excellence.yaml` is valid against the **current v5 workflow engine** and uses only existing semantic agent steps:

```text
planner/planning advisory
        ↓
developer/coding workspace-write
        ↓
code-reviewer/review advisory
        ↓
tester/testing advisory
        ↓
deterministic validate
```

The example intentionally contains:

- no provider `profile` pins;
- no language-specific agents;
- no invented per-step `skills:` field;
- no workflow-component syntax that the current engine does not yet support.

Skill requirements are expressed through semantic-agent definitions, resolver context, explicit resolution, or policy. Reusable workflow components/overlays are a later workflow-engine milestone and can reuse the same skill assets when they land.

## Behavioral evals

Every execution discipline has at least one deterministic SDAI behavioral eval:

- implementation planning → traceability/files/verification/rollback;
- TDD → failing test first, run failure, minimal change, surrounding suite;
- systematic debugging → reproduce/hypothesis/root cause/regression test;
- verification → run checks, inspect exit code, report evidence, no “should work.”

The existing mock executor makes the schema, assertions, scoring, regressions, and CI behavior deterministic. Provider-backed eval execution can later use the same provider-neutral scenario/result contract.

## Deterministic pack validation

`sdai.execution_excellence` validates the pack as a unit:

- exact Pack identity/type and skill membership;
- every referenced skill has valid resolver metadata and eval scenarios;
- execution skills remain technology-neutral and semantic-role neutral;
- every skill opts into resolver automatic selection and declares capabilities;
- workflow examples contain no provider profile or unsupported per-step skill syntax;
- workflow agent names stay semantic/provider-neutral;
- workflow ends in deterministic validation;
- policy examples do not constrain providers;
- policy skill lists are well formed, stay inside the pack, and preserve the intended mandatory planning/coding/review/testing disciplines.

## Trust and governance boundary

Execution excellence strengthens **how** work is performed; it does not redefine **who is authorized** or **what is canonical truth**.

These skills cannot:

- approve their own requirements, architecture, security, or promotion decisions;
- weaken organization policy;
- directly mutate protected canonical specification state through external agent execution;
- turn a failed/not-run quality gate into success;
- bypass approval, protected-path, capability, environment, or workspace-write restrictions;
- replace deterministic traceability or promotion evidence with prose.

The ownership rule remains:

```text
AI proposes / analyzes / implements / reviews / recommends
SDAI validates / enforces / records / gates / promotes
humans or identity-backed systems approve where policy requires it
```
