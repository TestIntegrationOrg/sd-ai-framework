# Architecture Artifact Validation

SD-AI treats architecture artifacts as lifecycle evidence rather than optional AI documentation.
The deterministic `architecture-artifact-validator` runs as part of `sdai validate` and therefore also runs when a workflow reaches its normal validation step.

It is **not an AI agent**. The Architect agent and architecture skills propose artifacts; the validator independently checks that required artifacts exist and meet basic machine-verifiable quality rules.

## Default lifecycle requirements

The repository profile is stored at `.sdai/architecture-validation.yaml`.

### Light

No additional architecture-artifact requirements by default.

### Standard

```text
Specification             required
Architecture alternatives required
Decision matrix           required
ADR                       required
Traceability              required
```

### Critical

```text
Specification             required
RFC                       required
Architecture alternatives required
Decision matrix           required
ADR                       required
C4 context                required
Component diagram         required
Sequence diagram          required
Security model            required
API/event contracts       required
Traceability              required
```

Run the normal validator:

```bash
sdai validate FEATURE-123 --workflow critical
```

The validator also exposes a stable checklist formatter for CLI/CI surfaces. A report is shaped like:

```text
Architecture artifact validation — FEATURE-123 (critical)
PASS  specification                Approved feature specification: satisfied
PASS  rfc                          Decision-ready engineering RFC: satisfied
PASS  architecture-alternatives    Architecture alternatives and explicit trade-offs: satisfied
PASS  decision-matrix              Architecture decision matrix: satisfied
PASS  adr                          Architecture Decision Record: satisfied
PASS  c4-context                   C4 system-context diagram: satisfied
PASS  component-diagram            Component-level architecture diagram: satisfied
PASS  sequence-diagram             Runtime interaction/sequence diagram: satisfied
PASS  security-model               Threat/security model: satisfied
PASS  api-event-contracts           Version-controlled API/event/schema contract: satisfied
PASS  traceability                 Requirement-to-task traceability: satisfied
Result: PASS
```

A critical feature cannot pass while a required architecture artifact is missing or invalid unless an allowed waiver satisfies the configured waiver policy.

## Artifact conventions

The stock profile expects:

```text
specs/FEATURE-123/
├── specification.md
├── rfc/
│   └── RFC-*.md
├── architecture/
│   ├── architecture.md
│   ├── decision-matrix.md
│   ├── validation-waivers.yaml        # optional
│   └── diagrams/
│       ├── context.puml|mmd|drawio
│       ├── component*.puml|mmd|drawio
│       └── *sequence*.puml|mmd|drawio
├── adr/
│   └── ADR-*.md
├── contracts/
│   ├── openapi*.yaml|json
│   ├── asyncapi*.yaml|json
│   ├── **/*.proto
│   └── schemas/*.{json,yaml,yml}
├── security/
│   └── threat-model.md
└── tasks.yaml
```

The profile is configurable. Teams may use different paths by changing the repository-controlled requirement definitions.

## Validation checks

The validator does more than `Path.exists()`.

### Specification / RFC / decision matrix / ADR / security model

Markdown must contain meaningful non-heading content and cannot be a heading-only or `TODO`/`TBD` placeholder.

### Architecture alternatives

The stock `architecture-alternatives` check requires at least two explicit Markdown sections whose headings start with `Option` or `Alternative`. This prevents a critical architecture from presenting a single predetermined design while claiming alternatives were reviewed.

Example:

```markdown
## Option A - synchronous retry
...

## Option B - asynchronous queue
...
```

### PlantUML

`.puml` diagrams must start with `@startuml` and end with `@enduml`.

### Draw.io

`.drawio` files must be editable Draw.io XML with:

```text
mxfile
  └── diagram
      └── mxGraphModel
```

and at least one editable vertex or edge `mxCell`. A flattened screenshot or placeholder XML does not satisfy the check.

### Mermaid

`.mmd` files must start with a supported Mermaid/C4 declaration such as `flowchart`, `graph`, `sequenceDiagram`, `C4Context`, `C4Container`, or `C4Component`.

### API / event contracts

The stock contract validator recognizes version-controlled:

- OpenAPI (`openapi` or legacy `swagger` root key)
- AsyncAPI (`asyncapi` root key)
- JSON Schema (`$schema` root key)
- Protocol Buffers with a syntax declaration and at least one message/service

This is intentionally structural validation. Full OpenAPI/AsyncAPI/JSON Schema semantic validation belongs to a later contract-validation layer.

### Traceability

Traceability requires:

1. `specification.md` contains stable `FR-*`, `NFR-*`, or `AC-*` identifiers.
2. `tasks.yaml` contains tasks.
3. Every task has `traces_to`.
4. Every task references at least one identifier present in the specification.

## Repository configuration

Example `.sdai/architecture-validation.yaml`:

```yaml
version: 1
modes:
  standard:
    required:
      - specification
      - architecture-alternatives
      - decision-matrix
      - adr
      - traceability
  critical:
    required:
      - specification
      - rfc
      - architecture-alternatives
      - decision-matrix
      - adr
      - c4-context
      - component-diagram
      - sequence-diagram
      - security-model
      - api-event-contracts
      - traceability

settings:
  allow_waivers: true
  waiver_file: architecture/validation-waivers.yaml
  critical_waiver_requires_approval: true

requirements:
  rfc:
    description: Decision-ready engineering RFC
    any_of: [rfc/RFC-*.md]
    check: markdown
```

Supported checks are:

```text
presence
markdown
alternatives
adr
diagram
contract
traceability
```

Requirement paths must be relative to the feature workspace and may not contain `..`.

## Not-applicable / waiver evidence

A requirement may be genuinely not applicable. For example, an internal algorithm change may not introduce an API or event contract.

The stock configuration supports explicit waiver evidence:

```yaml
# specs/FEATURE-123/architecture/validation-waivers.yaml
version: 1
waivers:
  api-event-contracts:
    reason: Internal algorithm only; no API or event contract changes.
    approved_by: architecture-review
```

For a critical feature, the stock profile requires both `reason` and `approved_by`.

`approved_by` is currently policy evidence, not cryptographic identity proof. Enterprise identity-backed approvals remain a separate governance control.

## Enterprise policy

Individual engineers may customize the repository validation profile.

Enterprise policy can add non-removable requirements and can disable waivers:

```yaml
version: 1
providers: {}
capabilities: {}
execution: {}
skills: {}

architecture_validation:
  required:
    critical:
      - rfc
      - c4-context
      - component-diagram
      - sequence-diagram
      - security-model
      - api-event-contracts
  allow_waivers: false
```

Policy merge rules are restrictive:

```text
Required architecture artifacts -> additive union
allow_waivers=false             -> deny wins
```

Therefore a repository or user policy can add stricter requirements but cannot remove an organization-required artifact or re-enable waivers that organization policy disabled.

## Relationship to architecture skills

```text
Requirement / Feature
        │
        ▼
Architect agent
        │
        ├── architecture-design
        ├── rfc-authoring
        ├── adr-authoring
        ├── c4-modeling
        ├── drawio-architecture
        ├── plantuml-sequence
        ├── api-contract-design
        └── threat-modeling
        │
        ▼
Proposed architecture artifacts
        │
        ▼
Human / governed lifecycle writes approved artifacts
        │
        ▼
architecture-artifact-validator
        │
        ├── PASS
        ├── WAIVED (when policy permits)
        └── BLOCK
```

The important separation is that the same AI that generated an artifact is not trusted to certify that the lifecycle evidence is complete.

## Future extensions

The deterministic artifact validator is the foundation for later validation layers:

- full OpenAPI / AsyncAPI / JSON Schema semantic validation
- PlantUML compilation checks
- Draw.io schema/render checks
- architecture-to-contract consistency
- architecture drift detection
- requirement -> RFC -> ADR -> task -> code -> test graph
- policy-backed architecture evidence reports
