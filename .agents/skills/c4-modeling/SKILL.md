---
name: c4-modeling
description: Create consistent C4 system-context, container, component, and deployment views with stable boundaries and identifiers.
---
# C4 Modeling

Use C4 views to communicate different abstraction levels without mixing concerns.

## Modeling rules

- Level 1 / System Context: people, the system of interest, and external systems only.
- Level 2 / Container: independently deployable/runnable applications and data stores; show technology only when decision-relevant.
- Level 3 / Component: major internal responsibilities and dependencies inside one selected container; do not turn every class into a component.
- Deployment view: runtime nodes/environments, replicated instances, managed services, network/trust boundaries, and placement relationships.
- Use stable IDs and the same names across all views and sequence diagrams.
- Label relationships with intent/protocol where helpful; avoid unlabeled arrows.
- Show trust/external boundaries when they materially affect security or ownership.
- Keep each diagram readable; split views rather than creating a single "everything" diagram.

## Source format

Prefer repository-supported PlantUML or Mermaid. Do not introduce remote `!includeurl` dependencies unless the repository explicitly allows them. If C4-PlantUML is already vendored/configured, follow the existing convention; otherwise generate self-contained PlantUML/Mermaid that preserves C4 semantics.

Suggested files: `architecture/diagrams/context.puml`, `container.puml`, `component-<name>.puml`, and a deployment diagram source.
