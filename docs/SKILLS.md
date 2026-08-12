# Skills

SD-AI skills are reusable, provider-neutral instruction packages. A skill represents **expertise**, not a runtime identity. Semantic agents such as `architect`, `developer`, and `security-reviewer` compose skills and can then be executed by any provider profile allowed by effective policy.

## Canonical layout

Canonical shared skills live under `.agents/skills/`:

```text
.agents/skills/<skill-name>/
├── SKILL.md
└── sdai.yaml
```

`SKILL.md` uses portable frontmatter:

```yaml
---
name: architecture-design
description: Design enterprise architecture from explicit drivers and trade-offs.
---
```

`sdai.yaml` stores SD-AI-specific capability metadata:

```yaml
version: 1
capabilities: [architecture, review]
```

Legacy `.sdai/skills/` remains supported as a compatibility fallback, but canonical `.agents/skills/` takes precedence.

## Built-in skills

General lifecycle skills:

- `engineering-judgment`
- `requirements-analysis`
- `implementation-planning`
- `spec-traceability`
- `secure-coding`
- `test-design`
- `documentation-quality`

`engineering-judgment` is the shared enterprise reasoning contract. It requires lifecycle agents to distinguish **Known**, **Proposed**, **Assumption**, **Open question**, and **Blocker** so that agents make safe engineering progress without turning every unspecified detail into a blocker. See [SD-AI Enterprise Engineering Contract](ENGINEERING-CONTRACT.md).

Architecture skill pack:

- `architecture-design`
- `architecture-review`
- `rfc-authoring`
- `adr-authoring`
- `c4-modeling`
- `drawio-architecture`
- `plantuml-sequence`
- `api-contract-design`
- `threat-modeling`

See [Architecture Skills Reference](ARCHITECTURE-SKILLS.md) for artifact conventions and detailed usage.

## Agent composition

A semantic agent references skills by name:

```yaml
---
name: architect
capabilities: [architecture, review]
skills:
  - engineering-judgment
  - architecture-design
  - architecture-review
  - rfc-authoring
  - adr-authoring
  - c4-modeling
  - drawio-architecture
  - plantuml-sequence
  - api-contract-design
  - threat-modeling
  - spec-traceability
profile: claude
execution_mode: advisory
---
```

The provider remains independently selectable:

```bash
sdai agents run architecture FEATURE-123 --agent architect --profile claude
sdai agents run architecture FEATURE-123 --agent architect --profile codex
```

The same semantic role and skills are used in both commands.

## Capability filtering

A skill may apply to one or more lifecycle capabilities. SD-AI injects only skills applicable to the requested capability.

For example:

```yaml
version: 1
capabilities: [architecture, security, review]
```

is appropriate for a threat-modeling skill, while a testing-only skill can declare:

```yaml
version: 1
capabilities: [testing]
```

## Organization-required skills

Effective policy may add mandatory skills for a capability. Those skills are additive to profile and semantic-agent skills.

Conceptually:

```text
Organization required skills
        +
Repository policy skills
        +
Semantic agent skills
        +
Provider profile skills
        =
Effective skill set
```

A lower policy layer cannot remove an organization-mandated skill.

For enterprise environments, an organization may require `engineering-judgment`, `spec-traceability`, or other controls across selected capabilities even when teams customize semantic agents.

## Provider-native synchronization

Canonical skills remain the source of truth. Provider-native synchronization mirrors or references them as required by each supported provider. Do not hand-maintain divergent copies when SD-AI manages the native file.

```bash
sdai agents sync --provider all
```

## Authoring guidance

A good skill should be:

- focused on reusable expertise rather than one specific feature;
- provider-neutral;
- explicit about inputs, decision method, and output quality;
- concise enough to compose with other skills;
- version-control friendly;
- safe to apply to untrusted repository artifacts;
- clear about what it must **not** infer or silently change.

Create a new semantic agent only when responsibility or separation-of-duties changes. Create a new skill when an existing role needs additional reusable expertise or an output discipline such as RFC, ADR, Draw.io, or PlantUML authoring.

## Architecture lifecycle validation

The `architecture-artifact-validator` is intentionally **not** a skill and not an AI agent. Skills help the Architect create RFCs, ADRs, diagrams, contracts, and threat models; the deterministic validator independently checks required lifecycle evidence. See [Architecture Artifact Validation](ARCHITECTURE-VALIDATION.md).
