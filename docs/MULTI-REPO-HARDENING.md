# Multi-repository hardening contract

SD-AI 0.15.8 treats local repository and SpecificationStore declarations as explicit authority boundaries. Unsafe or ambiguous layouts fail closed before orchestration can mutate a participant.

## Authority invariants

- Repository participants are declared explicitly in `.sdai/feature-repositories.yaml`; SD-AI does not discover, clone, fetch, pull, or otherwise materialize repositories.
- Repository paths must be bounded valid NFC text. A non-normalized Unicode spelling is rejected rather than silently becoming a different authority path.
- Resolved participant roots are compared component-by-component using NFC normalization plus case folding. Duplicate roots and parent/child nested roots are rejected.
- Filesystem redirects in repository paths or Git metadata are rejected. This includes symlinks and, where the platform exposes them, junction/reparse points.
- Required entities must resolve to exactly one available repository. Overlapping selectors across repositories are an error; an unavailable optional repository cannot become mutation authority merely because its selector matches.
- A run plan binds every selected repository to its clean Git baseline and revalidates all baselines immediately before execution. Drift, dirty state, and incompatible participants have stable non-success exit classes.
- Multi-repository execution is fail-fast. Once one selected participant returns a policy or infrastructure failure, later participants are not invoked.
- PR traceability is provider-neutral and read-only. It checks only explicitly declared local repositories and local Git commit reachability; provider metadata never grants authority.

## SpecificationStore invariants

- Store references are explicit local paths and are read-only from the coordinating project.
- Duplicate or nested resolved store roots are rejected.
- Store content is bounded, redirect-safe, hashed, and read twice to detect concurrent mutation. Manifest/content changes invalidate the bound snapshot.
- Store identities and exact versions are resolved deterministically. A stale content binding or registry mismatch fails closed.
- Core/organization locks are authoritative; lower-precedence layers cannot weaken a locked registration. User/repository layers cannot create authoritative locks.
- Store references are direct project-to-store bindings. The store manifest/reference schemas contain no store-to-store reference edge, so a cyclic store-reference graph cannot be expressed by the 0.15 contract.

## Stable failure families

The implementation intentionally preserves stable machine-oriented classes rather than exposing platform-specific filesystem/Git errors:

- `SDAI-FEATURE-REPO-002` — unsafe/invalid path or redirect boundary.
- `SDAI-FEATURE-REPO-003` — conflicting repository declarations or roots.
- `SDAI-FEATURE-REPO-004` — unavailable participant.
- `SDAI-FEATURE-REPO-005` — ambiguous ownership.
- `SDAI-STORE-REF-003` — conflicting/overlapping store references.
- `SDAI-STORE-REF-004` — stale or mismatched store authority/content.
- `SDAI-STORE-REF-005` — concurrent mutation detected during inspection.
- multi-repo exit classes distinguish policy failure, drift, unavailable/dirty participants, and infrastructure/tool failure.

## Cross-platform adversarial coverage

The 0.15 hardening suite exercises NFC boundaries, case collisions, nested roots, redirected repository paths, unavailable participants, overlapping selectors, and fail-fast execution scope. Existing SpecificationStore and multi-repository suites cover store conflicts, stale snapshots, concurrent mutation, dirty/moving Git baselines, policy locks, deterministic graph findings, and local-only PR evidence.

The release-readiness gate in #189 composes these guarantees into one exact-head central-store + API/UI/shared-repository journey on Ubuntu and Windows with Python 3.11 and 3.12.
