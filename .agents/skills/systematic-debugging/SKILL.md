---
name: systematic-debugging
description: Diagnose failures through reproducible evidence and controlled hypotheses before changing code.
---

# Systematic Debugging

Debug from evidence, not from a sequence of speculative edits.

1. Reproduce the failure with the smallest stable command/request/test and capture the exact symptom, inputs, environment, and relevant logs/exit status.
2. Establish what changed and classify the failure boundary before proposing a fix: configuration, dependency, data, contract, concurrency, environment, or code path.
3. Narrow the search using observations and instrumentation; formulate one falsifiable hypothesis at a time.
4. Test the hypothesis with the least invasive experiment and record whether the evidence supports or rejects it.
5. Identify the root cause and explain the causal chain, including why nearby alternatives are not the cause when that matters.
6. Implement the smallest root-cause fix, add a regression test/reproduction, and verify both the original failure and relevant surrounding behavior.
7. If evidence is insufficient, say what is unknown and collect the next discriminating signal instead of stacking random changes or disabling safeguards.

For SDAI debugger tasks, persist the investigation as `sdai.debug-record/v1`. The record must keep observed facts separate from interpretation and reference the observations/experiments that support the confirmed root cause. A debugger completion claim is valid only when the deterministic record validator sees a confirmed root cause, a recorded fix, and passing regression evidence. Provider/model prose cannot substitute for that evidence.

If three materially different fix attempts fail, stop proposing another speculative patch. Re-evaluate the reproduction, architecture, assumptions, component boundaries, or missing observability before continuing.
