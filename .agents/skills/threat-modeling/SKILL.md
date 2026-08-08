---
name: threat-modeling
description: Create architecture-linked threat models covering assets, actors, trust boundaries, abuse cases, controls, and residual risk.
---
# Threat Modeling

Threat-model the proposed architecture using repository evidence and explicit trust boundaries.

## Method

1. Identify protected assets, sensitive data, identities, credentials, critical operations, and availability targets.
2. Identify actors, external systems, administrative/control-plane access, and attacker-relevant entry points.
3. Map data flows and trust-boundary crossings using the same component names as architecture diagrams.
4. Enumerate relevant threats/abuse cases. STRIDE or another organization-approved taxonomy may be used as a checklist, not as a substitute for system-specific reasoning.
5. For each material threat record affected asset/flow, preconditions, impact, existing control, required mitigation, detection/observability, residual risk, and owner/status when known.
6. Convert required mitigations into explicit security requirements, ADR implications, implementation tasks, and tests where appropriate.
7. Re-evaluate threats introduced by retries, queues, caches, multi-tenancy, privileged operations, deployment pipelines, third-party integrations, and operational tooling.

## Rules

- Distinguish confirmed architecture facts from assumptions and hypothetical abuse cases.
- Do not claim compliance or risk acceptance without repository evidence/human approval.
- Keep the threat model synchronized with C4/deployment/sequence diagrams and API/event contracts.

Suggested artifact: `security/threat-model.md` or the repository's established security-design location.
