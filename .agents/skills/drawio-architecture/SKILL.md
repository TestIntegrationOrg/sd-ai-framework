---
name: drawio-architecture
description: Generate editable, version-control-friendly Draw.io architecture diagrams as valid mxGraph XML.
---
# Draw.io Architecture

Generate **editable Draw.io source**, not a flattened image.

## Output contract

- Return valid XML suitable for saving as a `.drawio` file.
- Prefer uncompressed XML for Git readability and reviewability.
- Use the standard `<mxfile><diagram><mxGraphModel>...` structure with editable `mxCell` vertices and edges.
- Escape XML special characters correctly and keep IDs unique/stable inside the diagram.
- Do not embed secrets, credentials, private URLs, or production-sensitive data.

## Diagram quality

- Use a clear left-to-right or top-to-bottom flow and consistent spacing/alignment.
- Use containers/boundaries to distinguish systems, services, trust zones, networks, accounts/subscriptions/projects, or deployment environments when relevant.
- Label every important connection with purpose and protocol/interaction style where useful.
- Distinguish synchronous calls, asynchronous messaging, data stores, users/external systems, and control-plane dependencies.
- Include failure/HA relationships only when they clarify the architecture; avoid decorative complexity.
- Use approved provider icon shapes only when the project already has a stable icon/library convention. Otherwise prefer portable labeled shapes over brittle external assets.
- Keep terminology identical to the RFC/ADR/C4/sequence artifacts.

## Recommended views

Create separate files for high-level/system context, component/integration, and deployment views rather than one overloaded canvas. Suggested names: `architecture/diagrams/high-level.drawio`, `component.drawio`, and `deployment.drawio`.
