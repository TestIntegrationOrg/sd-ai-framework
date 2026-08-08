---
name: adr-authoring
description: Author focused Architecture Decision Records with drivers, options, consequences, and revisit conditions.
---
# ADR Authoring

Use one ADR for one material architectural decision. AI-generated ADRs start with status **Proposed** unless an approved artifact proves otherwise.

## ADR structure

- ADR ID and concise title.
- Status: Proposed / Accepted / Superseded / Deprecated, using only evidence from the repository or workflow.
- Context and problem.
- Decision drivers.
- Considered options, including the credible status-quo/simple option when applicable.
- Decision matrix or explicit trade-off comparison for material choices.
- Proposed decision and rationale.
- Positive consequences.
- Negative consequences / accepted trade-offs.
- Risks and mitigations.
- Migration/rollback implications when relevant.
- Revisit triggers: what future condition would invalidate the decision.
- Links to requirements, RFC, diagrams, contracts, and superseded ADRs.

## Rules

- Do not combine unrelated decisions merely to reduce ADR count.
- Do not mark an ADR Accepted based solely on AI recommendation.
- Preserve ADR numbers; supersede rather than renumber historical decisions.
- Suggested path: `adr/ADR-NNN-kebab-title.md`.
