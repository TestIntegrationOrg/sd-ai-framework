# Governance Model

SD-AI separates **policy** from **execution**.

- `.sdai/constitution.yaml` contains durable engineering principles.
- `.sdai/policies.yaml` classifies changes and approval expectations.
- `.sdai/workflows/*.yaml` define executable lifecycle steps.
- `specs/<feature>/` contains feature-specific source-of-truth artifacts.

## Approval strategy

Light changes should remain lightweight. Standard and critical changes add stronger gates based on impact and risk.

Future versions will persist approval evidence and signatures rather than merely describing the gate.
