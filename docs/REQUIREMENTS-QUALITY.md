# Engineering Constitution and Requirements Quality

SDAI 0.6 adds a deterministic quality layer before architecture and implementation. The purpose is not to let an AI decide whether its own requirements are good; it is to create stable evidence, stable finding IDs, and reviewer-owned artifacts that humans and semantic agents can refine safely.

## Engineering constitution

The repository constitution lives at:

```text
.sdai/constitution.md
```

Create it once:

```bash
sdai constitution init
```

Inspect and validate it:

```bash
sdai constitution show
sdai constitution validate
```

The Markdown file begins with machine-readable frontmatter. Every principle has a stable `CON-NNN` identifier, title, severity, and optional deterministic checks such as required specification sections or terms.

SDAI computes SHA-256 over the exact UTF-8 constitution content. Any change to governing text therefore changes the constitution identity. `sdai constitution check FEATURE` writes feature-scoped evidence to:

```text
specs/<feature>/quality/constitution-check.yaml
```

That evidence contains the constitution hash, concrete `CON-NNN` findings, reviewer owner, and pending approval state. Principles without a deterministic evidence rule are marked `review`, never automatically `pass`.

The default constitution separates engineering principles from provider/execution policy. It covers testable requirements, security/privacy, failure/observability, compatibility/rollout, and traceable architecture decisions.

## Clarification

Generate a reviewer-owned clarification artifact without modifying the canonical specification:

```bash
sdai clarify FEATURE
```

Output:

```text
specs/<feature>/quality/clarifications.md
```

The artifact contains 14 stable clarification categories:

- functional behavior
- actors and permissions
- inputs and outputs
- errors and failure modes
- edge cases and boundaries
- state and lifecycle
- performance and scale
- security and privacy
- compatibility and migration
- observability
- deployment and rollout
- rollback and recovery
- retention and deletion
- compliance and audit

A category may be marked `candidate-covered` when related text is found, but that status is intentionally not approval. A requirements analyst or authorized human reviewer must decide whether the evidence actually resolves the question.

Accepted clarification changes are applied through the requirements workflow. The clarification command never rewrites `specification.md` itself.

## Requirements quality checklist

Run deterministic requirements checks with:

```bash
sdai requirements check FEATURE
```

Output:

```text
specs/<feature>/quality/requirements-checklist.md
```

The initial stable finding set is `RQ-001` through `RQ-014`, covering:

- required specification structure
- unresolved placeholders
- unique requirement/acceptance IDs
- normative FR/NFR language
- identifiable acceptance criteria
- security/privacy
- failure behavior and observability
- measurable performance/scale targets
- compatibility/migration impact
- unresolved open questions
- actors/authorization
- inputs/outputs/contracts
- edge cases/state transitions
- rollout/rollback/retention/compliance applicability

Blocking failures cause `sdai requirements check` to return non-zero so CI/workflows can use the result without parsing prose. Warning findings do not make the command fail, but remain visible for reviewer disposition.

## Reviewer ownership

Clarification, constitution-check evidence, and requirements checklists record:

```yaml
review_owner: requirements-analyst
approval_status: pending
implementation_self_approval: forbidden
```

This is intentional. An implementation agent may supply evidence or propose a correction, but cannot silently mark requirements quality or constitution compliance approved. Identity-backed approval is a later enterprise release; this 0.6 layer establishes the ownership and evidence contract now.

## Hash binding

Requirements-quality artifacts store the SHA-256 of the specification they analyzed. Constitution evidence stores the SHA-256 of the constitution. These hashes are the foundation for later staleness and approval invalidation: when a governed input changes, previously generated evidence can be recognized as referring to an older state rather than treated as current truth.
