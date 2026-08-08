---
name: rfc-authoring
description: Create decision-ready engineering RFCs with alternatives, NFRs, risks, migration, operations, and explicit open questions.
---
# RFC Authoring

Create RFCs for significant technical changes. Default AI-generated RFC status to **Draft**.

## Required structure

1. Title, RFC ID, status, owners/reviewers when known, and last-updated date when supplied by the task.
2. Executive summary.
3. Problem statement and current-state evidence.
4. Goals and non-goals.
5. Functional requirements and acceptance references.
6. Non-functional requirements and measurable architecture drivers.
7. Constraints, assumptions, dependencies, and unresolved questions.
8. Proposed architecture.
9. Viable alternatives and trade-off/decision matrix.
10. Data ownership, storage, consistency, and lifecycle implications.
11. APIs, events, schemas, and compatibility/versioning implications.
12. Security and privacy considerations including trust boundaries.
13. Scalability, availability, resilience, and failure-recovery behavior.
14. Observability and operations.
15. Deployment, migration, rollback, and backward-compatibility plan.
16. Cost implications when material.
17. Risks and mitigations.
18. ADRs required or referenced.
19. Diagram inventory and links.
20. Open questions and decision deadline/owner when known.

## Quality rules

- Separate facts, assumptions, decisions, proposals, and open questions.
- Do not hide a major decision inside prose; create/link an ADR proposal.
- Avoid generic architecture claims such as "highly scalable" without a driver or mechanism.
- Keep the RFC internally consistent with diagrams and contracts.
- Suggested path: `rfc/RFC-NNN-kebab-title.md` under the feature workspace.
