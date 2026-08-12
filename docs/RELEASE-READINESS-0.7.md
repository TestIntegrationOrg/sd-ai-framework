# SDAI 0.7 — Current Truth + Technology Skills Release Readiness

This document is the acceptance map for issue #62 and the 0.7 parent (#14). It records what must remain true before the 0.7 implementation slice is considered complete. It is **not** a statement that a package, tag, or `0.7.0` release has already been published.

## Release posture

- Package version remains whatever `src/sdai/__init__.py::__version__` declares until an intentional release cut.
- Roadmap completion does not implicitly change package metadata or create a GitHub/PyPI release.
- The deterministic version synchronization guard from 0.6 remains authoritative for a future deliberate version change.
- All 0.6 backward-compatibility tests remain part of the 0.7 release gate and must stay green unchanged.

## 0.7 capability chain

The release gate covers the dependency-ordered capabilities delivered through the 0.7 implementation sequence:

1. **#55 — Current/change/delta model**
   - `specs/current/<domain>/specification.md`
   - typed `ADDED`, `MODIFIED`, `REMOVED`, and `RENAMED` delta operations
   - strict portable parsing, baseline identities, and path containment
2. **#56 — Deterministic validation and parallel conflict detection**
   - whole-spec and per-requirement SHA-256 baselines
   - stale baseline findings
   - overlapping parallel change footprints
   - read-only structured validation evidence
3. **#57 — Governed promotion**
   - semantic diff and dry-run
   - change/current-hash-bound approval
   - current-policy approver revalidation
   - immediate pre-image verification
   - multi-domain rollback
   - promotion evidence and archive outside active changes
4. **#58 — Repository technology detection**
   - deterministic language/framework/build/platform/library/test evidence
   - explicit `.sdai/technology.yaml` declarations/pins
   - conservative ambiguous-version handling
   - stable portable JSON evidence
5. **#59 — Minimal compatible skill resolver**
   - semantic-agent + policy + explicit skill union
   - dependency graph and deterministic ordering
   - role/capability/task/domain filters
   - conservative technology/version compatibility
   - explainable selected/skipped decisions
6. **#60 — Tier-1 language/framework assets**
   - Java/Spring Boot
   - C#/.NET/ASP.NET Core
   - Python/FastAPI/Django
   - JavaScript/TypeScript/Node.js/React/Angular
   - Go
   - PowerShell
   - deterministic pack integrity and behavioral evals
7. **#61 — Execution excellence**
   - precise implementation planning
   - test-driven development
   - systematic debugging
   - verification before completion
   - current-v5 provider-neutral workflow and additive policy examples
8. **#70 — Resolver keyword-boundary hardening**
   - case-insensitive token/phrase boundary matching
   - no false positives such as `bug` inside `debug`
   - Unicode/cross-platform matching evidence

## Compatibility acceptance matrix

| Area | Required evidence |
|---|---|
| Full regression | Entire `pytest -q` suite succeeds; CI runs Ubuntu/Windows × Python 3.11/3.12. |
| Existing 0.6 compatibility | `tests/test_v06_release_compatibility.py` remains enabled and unchanged in intent; v0.5/v0.6 init/upgrade, extension, workflow, policy, UTF-8, and manual-step behavior remains green. |
| Current truth | Current Markdown and structured requirement identities remain deterministic; unrelated UTF-8 Markdown survives promotion. |
| Parallel changes | Two valid changes based on the same current truth produce a deterministic footprint conflict; after the first promotion the competing change becomes stale rather than silently rebasing. |
| Dry-run safety | Promotion preview is byte-for-byte read-only and creates no promotion evidence. |
| Approval safety | Real promotion requires fresh change/current-hash-bound approval under the current approval policy. |
| Atomic promotion | A simulated second-domain replacement failure restores all already-written current specs from exact pre-images and leaves the active change available for operator recovery. |
| Archive semantics | Successful changes move to `specs/archive/changes/<FEATURE>/<PROMOTION-ID>/` with `promotion.yaml`; archive history cannot be rediscovered as an active change. |
| Technology detection | Tier-1 repository technology evidence is deterministic, provider-independent, UTF-8/path portable, and does not conflate language/runtime/framework versions. |
| Version compatibility | A skill requiring Java 21 is rejected against deterministic Java 17 evidence with `SDAI-SKILL-003`; weak/ambiguous version evidence is not coerced into compatibility. |
| Skill minimality | Java/Spring Boot repository evidence resolves Java + Spring skills plus only task-relevant execution discipline; no `java-developer` or provider-specific semantic role is introduced. |
| Tier-1 packs | All six built-in Tier-1 language pack manifests pass deterministic asset/skill/eval integrity validation. |
| Execution excellence | Execution pack/evals/workflow/policy examples pass their deterministic validator; provider pins and malformed/weakened examples remain rejected. |
| Provider/role separation | Existing manual-step dry-run can independently select semantic agent `architect` and provider profile `codex`. |
| Upgrade preservation | `sdai upgrade` preserves customized canonical agents, legacy `.sdai/skills`, `.sdai/technology.yaml`, and `specs/current/**` byte-for-byte. |
| Windows/Linux and UTF-8 | Integrated journeys use workspace/content values containing spaces, `Ω`, `Δ`, and `café`; emitted evidence paths remain portable. |

Primary integrated evidence: `tests/test_v07_release_compatibility.py`.

## Trust boundaries validated

### Canonical specification truth

```text
human/AI-authored proposed delta
          ↓
strict parser + deterministic baseline validator
          ↓
semantic diff / read-only preview
          ↓
hash-bound human approval
          ↓
pre-image revalidation
          ↓
deterministic transaction
          ↓
current truth + archived evidence
```

No AI/provider call decides promotion eligibility, approval satisfaction, rollback success, or canonical current-state hashes.

### Technology and skills

```text
repository evidence + explicit technology pins
                    ↓
        deterministic technology model
                    ↓
 semantic role + capability + task/domain + policy
                    ↓
          minimal compatible skill resolver
                    ↓
       provider-neutral skill instructions
                    ↓
              Provider Router
```

Language/framework/platform/library context does not redefine semantic role identity and does not select an AI provider.

### Execution discipline

Execution-excellence skills can improve planning, TDD, debugging, and verification behavior, but they cannot:

- approve their own work;
- weaken organization policy;
- rewrite protected canonical truth to match code;
- mark a failed or unrun deterministic gate as success;
- override provider/capability/workspace-write/protected-path controls.

## Merge criteria

The final #62 PR must not merge unless:

- its exact latest head is mergeable;
- no unresolved actionable review finding remains;
- all four CI matrix jobs pass on the exact latest head;
- the full existing repository test suite runs—no older test may be disabled, deleted, skipped, or weakened merely to obtain green CI;
- `tests/test_v06_release_compatibility.py` and the new `tests/test_v07_release_compatibility.py` both pass on Windows and Linux;
- failures discovered by the integrated journey are fixed in runtime code or explicitly recorded as a release blocker;
- no runtime/provider/workflow behavior is added to the release-gate PR merely to bypass an acceptance failure.

## After this gate

Once #62 merges cleanly and the parent checklist is verified, issue #14 can close as **0.7 implementation-complete**. Publishing/tagging a package remains a separate intentional release operation.

The next planned foundation slice is 0.8: extensible artifact schemas and reusable workflow components/graph dependencies, followed by cross-artifact analysis, traceability, verification, and convergence.
