---
name: architecture-design
description: Design enterprise architecture from explicit drivers, alternatives, trade-offs, and evolvable boundaries.
---
# Architecture Design

Design from evidence and quality attributes rather than from favorite technologies.

## Method

1. Restate the problem, scope, assumptions, constraints, and unresolved questions without inventing business requirements.
2. Extract measurable architecture drivers from functional requirements and NFRs: scale, latency, availability, consistency, security, privacy, compliance, operability, cost, delivery constraints, and team boundaries.
3. Describe the relevant current-state architecture and dependencies before proposing a change.
4. Generate at least two viable options for every material or expensive-to-reverse decision. Include a simpler option when credible.
5. Compare options explicitly across scalability, reliability, latency, security, operability, cost, complexity, reversibility, migration risk, failure recovery, and vendor coupling.
6. Recommend an option only after the comparison. State why rejected options remain inferior under the current drivers.
7. Define system/container/component boundaries, synchronous and asynchronous interactions, data ownership, consistency model, trust boundaries, deployment topology, and operational responsibilities.
8. Model normal flow plus important failure, timeout, retry, duplicate, partial-success, rollback, and recovery paths.
9. Specify observability signals and SLO-relevant behavior where the requirements justify them.
10. Turn material decisions into ADR proposals and contract changes into version-controlled OpenAPI/AsyncAPI/JSON Schema artifacts.
11. Read `.sdai/architecture-validation.yaml` and ensure the proposed artifact set can satisfy the lifecycle mode. Surface missing/not-applicable evidence explicitly rather than relying on the validator to discover it later.

## Artifact discipline

- Keep written architecture, diagrams, ADRs, contracts, and deployment views mutually consistent.
- Use stable component/actor names across C4, sequence, deployment, and threat-model diagrams.
- Prefer source formats that diff cleanly in Git: Markdown, PlantUML, Mermaid, YAML/JSON, and uncompressed Draw.io XML.
- When returning an artifact in advisory mode, label it with the intended repository-relative filename and put the exact file content in a fenced block.
- Do not claim an RFC or ADR is approved. AI-generated decisions start as Draft/Proposed.
