---
name: implementation-planning
description: Create dependency-aware, verifiable implementation plans with explicit rollout and risk handling.
---

# Implementation Planning

Produce a plan that another engineer can execute without rediscovering the design.

- Anchor every material task to approved requirements, architecture/contracts, or an explicit discovery gap; never invent missing decisions.
- Name the affected component and, when known from repository evidence, the concrete files/symbols or artifact paths involved.
- Order tasks by dependency and identify genuinely parallel work separately from sequential prerequisites.
- For each task state the intended change, key implementation constraints, expected tests, and the command/evidence that will prove completion.
- Include API/data/schema migration, security, observability, rollout, rollback, documentation, and compatibility work when applicable.
- Separate discovery/decision tasks from implementation tasks when uncertainty remains; use an explicit TBD/blocker rather than guessing.
- Keep tasks independently verifiable and small enough that failures can be localized without turning the plan into artificial micro-steps.
- Do not treat the plan as approval to change canonical requirements or architecture; route those changes through their governed lifecycle.
