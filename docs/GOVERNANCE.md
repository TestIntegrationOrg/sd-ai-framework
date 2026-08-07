# Governance Model

SD-AI separates **policy**, **source-of-truth artifacts**, and **agent execution**.

- `.sdai/constitution.yaml` — durable engineering principles
- `.sdai/policies.yaml` — change classification and approval expectations
- `.sdai/workflows/*.yaml` — deterministic lifecycle steps
- `.sdai/agents.yaml` — external agent profiles
- `.sdai/routing.yaml` — capability-to-profile defaults
- `.sdai/prompts/` — version-controlled prompt templates
- `.sdai/skills/` — reusable provider-neutral skills
- `specs/<feature>/` — feature source-of-truth artifacts

External agents do not bypass lifecycle approvals. A provider may propose architecture or modify code when explicitly permitted, but a provider response is not itself an approved specification or ADR.
