# Architecture Skills Reference

SD-AI uses one semantic `architect` agent and composes specialized, provider-neutral skills underneath it. Diagram types, RFCs, ADRs, contracts, and threat models are **skills**, not separate agents.

This keeps the role/provider model stable:

```text
Architecture workflow step
        ↓
Semantic agent: architect
        ↓
Architecture skill set
        ↓
Provider profile selected by effective policy/user choice
        ↓
Claude / Codex / Copilot / Gemini / custom
```

## Built-in architecture skill pack

v0.5.2 adds or strengthens the following canonical skills under `.agents/skills/`:

| Skill | Purpose |
|---|---|
| `architecture-design` | Architecture drivers, alternatives, trade-offs, boundaries, failure modes, deployment and operations |
| `architecture-review` | Independent architecture review, drift detection, failure/security/operability review |
| `rfc-authoring` | Decision-ready RFCs with goals, NFRs, alternatives, migration, risks and open questions |
| `adr-authoring` | Focused ADR proposals with drivers, considered options, consequences and revisit triggers |
| `c4-modeling` | C4 system-context, container, component and deployment views |
| `drawio-architecture` | Editable `.drawio` mxGraph XML for high-level, component/integration and deployment views |
| `plantuml-sequence` | Self-contained `.puml` sequence diagrams including failure/retry/timeout/asynchronous paths |
| `api-contract-design` | OpenAPI, AsyncAPI and JSON Schema contract design and compatibility rules |
| `threat-modeling` | Architecture-linked assets, actors, trust boundaries, abuse cases, controls and residual risk |
| `spec-traceability` | Trace requirements → architecture/ADR → tasks/code/tests |

The canonical Architect definition attaches these skills while keeping `execution_mode: advisory` as its default posture.

## Why skills instead of more agents

Use an agent when the **semantic responsibility** changes. Use a skill when the same role needs reusable expertise.

Do:

```text
architect.agent.md
   ├── architecture-design
   ├── rfc-authoring
   ├── adr-authoring
   ├── c4-modeling
   ├── drawio-architecture
   ├── plantuml-sequence
   ├── api-contract-design
   └── threat-modeling
```

Avoid creating separate `drawio-agent`, `plantuml-agent`, `rfc-agent`, or `adr-agent` roles merely for output formats.

## Suggested feature artifact layout

Existing SD-AI baseline artifacts remain valid. Specialized architecture artifacts may be added alongside them:

```text
specs/<feature>/
├── specification.md
├── rfc/
│   └── RFC-001-<topic>.md
├── architecture/
│   ├── architecture.md
│   ├── decision-matrix.md
│   ├── context.mmd
│   ├── container.mmd
│   └── diagrams/
│       ├── context.puml
│       ├── container.puml
│       ├── component-<name>.puml
│       ├── <scenario>-sequence.puml
│       ├── high-level.drawio
│       ├── component.drawio
│       └── deployment.drawio
├── adr/
│   └── ADR-001-<decision>.md
├── contracts/
│   ├── openapi.yaml
│   ├── asyncapi.yaml
│   └── schemas/
└── security/
    └── threat-model.md
```

The exact artifact set should be proportional to the decision and risk. A small feature should not be forced to produce every diagram.

## Advisory execution and source-of-truth protection

The Architect agent is advisory by default. External agents are not allowed to silently rewrite protected `specs/**` source-of-truth artifacts.

When an architecture skill is asked to produce a file, it should return:

1. the intended repository-relative filename; and
2. the exact version-control-ready file content in a fenced block.

SD-AI can persist the agent response as the workflow's AI evidence while framework-owned lifecycle commands or an explicitly governed human step promotes proposed material into approved architecture artifacts.

AI-generated RFC/ADR content must remain `Draft` / `Proposed` until the workflow or human approval establishes a stronger status.

## Draw.io conventions

`drawio-architecture` produces editable source rather than screenshots/images.

Expected characteristics:

- valid `.drawio` XML;
- standard `<mxfile><diagram><mxGraphModel>...` structure;
- editable `mxCell` vertices/edges;
- uncompressed XML where practical for Git diffs;
- unique/stable IDs;
- explicit boundaries and labeled relationships;
- no embedded secrets or environment-specific credentials;
- approved vendor icons only when the repository has a stable icon-library convention.

A Draw.io diagram is treated primarily as a presentation artifact. SD-AI does not inject `.drawio` XML into every downstream AI prompt by default because those files can be large and noisy. Keep corresponding C4/PlantUML/Mermaid source for machine-readable architecture semantics.

## PlantUML sequence conventions

`plantuml-sequence` produces self-contained `.puml` source with:

- `@startuml` / `@enduml`;
- architecture-level participants rather than class/method noise;
- `alt`, `opt`, `loop`, and `par` blocks when appropriate;
- timeout/retry/backoff/idempotency/duplicate/failure paths where relevant;
- explicit asynchronous/event flows;
- consistent participant names with C4/RFC/ADR artifacts;
- no remote includes unless project policy explicitly allows them.

## RFC and ADR conventions

RFCs explain the broader technical proposal and its alternatives. ADRs isolate material decisions that are expensive to reverse.

```text
Requirement / NFR
       ↓
      RFC
       ↓
Architecture alternatives
       ↓
Decision(s)
       ↓
     ADR(s)
       ↓
Contracts + diagrams
       ↓
Implementation plan
```

Do not use an RFC as a substitute for ADRs, and do not hide multiple unrelated decisions inside one ADR.

## Downstream context

SD-AI includes committed text-based architecture artifacts in downstream agent context. The context collector scans:

- `rfc/`
- `architecture/`
- `adr/`
- `contracts/`
- `security/`

for version-control-friendly formats such as Markdown, Mermaid, PlantUML, YAML, JSON, Proto, and related architecture source. Existing fixed lifecycle artifacts and AI/quality-gate evidence remain included.

This means a later Developer, Tester, Security Reviewer, or Code Reviewer can reason from the RFC, sequence diagrams, contracts, ADRs, and threat model rather than only from `architecture.md`.

## Example usage

Use the same Architect role with different allowed providers:

```bash
sdai agents run architecture FEATURE-123 \
  --agent architect \
  --profile claude \
  --dry-run

sdai agents run architecture FEATURE-123 \
  --agent architect \
  --profile codex \
  --dry-run
```

The provider changes; the semantic role and architecture skill set do not.

A focused architecture task can request specific artifacts, for example:

```text
Review the proposed retry architecture. Produce:
- RFC draft
- C4 container view
- editable Draw.io deployment view
- PlantUML retry sequence including timeout and duplicate delivery
- ADR proposal for the queue choice
- threat-model findings for the new trust boundaries
```

The attached skills define the expected quality and source format for each artifact.

## Lifecycle validation

Architecture skills generate/propose the evidence; they do not self-certify it. SD-AI v0.5.3 adds a deterministic `architecture-artifact-validator` that runs through normal validation and can require RFCs, alternatives, decision matrices, ADRs, C4/component/sequence diagrams, security models, contracts, and traceability for critical features. See [Architecture Artifact Validation](ARCHITECTURE-VALIDATION.md).

For enterprise use, organization policy may add mandatory architecture artifacts and may disable artifact waivers. Repository/user configuration may become stricter but cannot weaken those organization requirements.
