---
name: architecture-review
description: Review architecture for requirement alignment, quality attributes, failure behavior, security, operability, and decision quality.
---
# Architecture Review

Review the architecture as an independent senior architecture reviewer.

## Review checks

- Trace every material design choice to an approved requirement, NFR, constraint, or ADR.
- Identify contradictions between RFCs, architecture prose, C4 views, sequence diagrams, Draw.io diagrams, contracts, deployment topology, and ADRs.
- Verify that meaningful alternatives and trade-offs were evaluated instead of presenting one predetermined solution.
- Challenge hidden assumptions about scale, latency, ordering, consistency, retries, idempotency, capacity, data growth, dependencies, and operational ownership.
- Review failure modes: dependency outage, timeout, partial failure, duplicate delivery, poison messages, retry storms, backpressure, degraded mode, rollback, and disaster recovery where relevant.
- Review security: trust boundaries, identity, authentication, authorization, secrets, encryption, data exposure, tenant isolation, auditability, and least privilege.
- Review operability: observability, SLOs, alerting, runbooks, deployability, migration, rollback, and support burden.
- Review cost and vendor coupling where they materially affect the decision.
- Flag architecture drift between approved decisions and the proposed implementation.

## Output

Prioritize findings by severity and decision impact. For each finding include evidence, why it matters, and a concrete remediation or decision that must be made. Distinguish blockers from improvements. Do not rewrite approved requirements to make the architecture pass review.
