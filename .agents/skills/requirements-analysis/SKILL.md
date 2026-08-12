---
name: requirements-analysis
description: Analyze requirements for ambiguity, completeness, testability, dependencies, assumptions, NFRs, and implementation readiness.
---
# Requirements Analysis

Produce a requirements baseline that is rigorous enough for enterprise development and useful enough for an engineer to continue the lifecycle.

## Method

1. Restate the business problem, desired outcome, actors, scope, and explicit constraints from available evidence.
2. Separate **Known**, **Proposed**, **Assumption**, **Open question**, and **Blocker** statements. Use the engineering-judgment skill definitions consistently.
3. Convert explicit intent into stable functional requirement identifiers (`FR-*`) and observable acceptance criteria (`AC-*`) when the repository has not already assigned identifiers.
4. Add **Proposed** requirements when they are conventional, directly support the stated intent, and do not silently choose a business policy. Explain the trace or engineering rationale.
5. Identify missing inputs, outputs, boundaries, failure behavior, compatibility, lifecycle behavior, authentication/authorization, data handling, and operational behavior when material.
6. Capture NFRs that materially affect architecture: scale, latency, availability, resilience, security, privacy, compliance, operability, data lifecycle, compatibility, and cost. Do not demand arbitrary numeric targets when none are supported; mark them as open only when they are required for the next decision.
7. Keep the open-question list short and decision-oriented. Name the decision owner or decision type when it can be inferred safely.
8. Mark a gap as a **Blocker** only when the next lifecycle action cannot proceed safely or would likely create invalid behavior, security exposure, contract breakage, or expensive rework.
9. Distinguish implementation blockers from items that may be resolved during architecture, planning, hardening, rollout, or operations.
10. Keep SD-AI execution/runtime/policy diagnostics out of the feature requirements artifact unless they directly constrain feature behavior.

## Output contract

Prefer this structure:

1. **Disposition** — Ready for architecture / Ready with assumptions / Blocked, with a one-paragraph rationale.
2. **Problem and outcome** — concise restatement of user/business intent.
3. **Known facts and constraints** — evidence-based only.
4. **Proposed functional requirements** — stable IDs, concise shall-statements, and trace/rationale.
5. **Acceptance criteria** — observable, testable behaviors mapped to requirements.
6. **Material NFRs and security requirements** — only those relevant to the feature.
7. **Assumptions** — reversible working assumptions with validation/revisit trigger.
8. **Open questions** — only decisions that materially affect design or behavior.
9. **Blockers** — explicit next action that cannot proceed, if any.
10. **Recommended next step** — what architecture/planning should do next.

Do not stop at “requirements are incomplete” when a useful proposed baseline can be produced safely. Do not invent business intent, compliance requirements, ownership, or externally visible policy.
