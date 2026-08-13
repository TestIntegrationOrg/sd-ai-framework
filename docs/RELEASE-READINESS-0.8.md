# SDAI 0.8 — Artifact Graph + Workflow Composition + Isolation Release Readiness

This document is the acceptance map for issue #79 and the 0.8 parent (#15). It records the evidence required before the 0.8 implementation slice is considered complete. It is **not** a statement that a package, tag, or `0.8.0` release has already been published.

## Release posture

- Package version remains whatever `src/sdai/__init__.py::__version__` declares until an intentional release cut.
- Roadmap completion does not implicitly modify package metadata or create a GitHub/PyPI release.
- The version synchronization guard introduced in 0.6 remains authoritative for a future deliberate version change.
- The full existing 0.6 and 0.7 compatibility suites remain enabled and are part of the 0.8 gate.

## 0.8 capability chain

1. **#73 — Artifact schema registry + DAG**
   - file-driven `sdai/v1` `ArtifactSchema` definitions
   - built-in → organization → repository → user layering with provenance
   - organization required/locked/dependency mandates cannot be weakened
   - deterministic topological validation, cycle/missing-edge detection, and portable paths
2. **#74 — Hash-bound artifact state + evidence invalidation**
   - `fresh`, `stale`, `missing`, and `blocked` lifecycle states
   - content SHA-256 and dependency SHA bindings
   - transitive downstream staleness
   - approval/validation/verification evidence bindings
   - domain-safe, collision-resistant state record identities
3. **#75 — Reusable workflow components + typed inputs**
   - `WorkflowComponent` manifests
   - typed/default/enum/sensitive inputs
   - deterministic interpolation and redacted provenance
   - component dependencies and cycle checks
   - v5 compatibility preserved
4. **#76 — Workflow inheritance, overlays + lifecycle hooks**
   - inheritance before composition
   - organization → repository → user overlay order
   - organization-mandated step/hook non-weakening
   - safe additive lower-layer operations around required controls
   - provider/shell execution fields forbidden from overlays
   - lifecycle hooks limited to advisory/gate/validation behavior
5. **#77 — PluginStep permission + execution boundary**
   - strict `sdai/v1` PluginStep manifest and permission policy
   - trusted installed executor registry; YAML cannot import arbitrary code
   - organization policy narrowing/deny precedence
   - protected-path/symlink/case-insensitive path hardening
   - explicit trusted executable search path; no ambient workspace `PATH`
   - literal argv + `shell=False`; network fails closed in v1
   - workflow `type: plugin` version 8 integration only through the reviewed SDK
   - reusable components may contain strict plugin steps; overlays/hooks remain plugin-free
6. **#78 — Verified Git worktree isolation**
   - `sdai run --isolation worktree`
   - exact repository-root, named-branch, clean-baseline gate
   - commit/tree/repository identity evidence under Git common metadata
   - hostile Git environment sanitization
   - detached worktree verification before dedicated branch creation
   - dirty implementation work is never automatically discarded
   - failed/cancelled clean worktrees may be safely removed

## Compatibility acceptance matrix

| Area | Required evidence |
|---|---|
| Full regression | Entire `pytest -q` suite succeeds on Ubuntu/Windows × Python 3.11/3.12. |
| Existing release gates | `tests/test_v06_release_compatibility.py` and `tests/test_v07_release_compatibility.py` remain enabled and green without weakened intent. |
| Artifact graph | Built-in and layered artifact definitions produce a deterministic DAG with stable provenance. |
| Organization artifact authority | Repo/user schema overlays cannot remove an organization-required artifact or mandated dependency. |
| Transitive freshness | Editing an upstream requirements artifact marks requirements and every dependent downstream artifact stale. |
| Evidence freshness | Editing/deleting a hash-bound validation/approval/evidence file invalidates the bound artifact and downstream dependents. |
| Workflow composition | Typed input resolves through a reusable component and the expanded step is parsed by the normal workflow parser. |
| YAML overlays | A repository overlay may add behavior around an organization-mandated step without copying core workflow code. |
| Workflow non-weakening | Repo/user overlays cannot disable/replace organization-mandated or protected validation/approval/security controls. |
| Plugin deny precedence | A repository allow cannot bypass an organization PluginStep deny. |
| Plugin execution boundary | Workflow plugins execute only through the reviewed SDK/registered executor; no direct shell/module/provider escape path exists. |
| Worktree baseline | Worktree mode records source branch/commit/tree/cleanliness before execution and creates the isolated root outside source. |
| Worktree preservation | Dirty isolated work is preserved even when cleanup is requested; source workspace remains clean. |
| Semantic role/provider separation | Existing manual-step dry-run still independently selects semantic agent `architect` and provider profile `codex`. |
| Windows/Linux + UTF-8 | Integrated journeys use spaces, `Ω`, `Δ`, and `café`; portable evidence remains cross-platform. |

Primary integrated evidence: `tests/test_v08_release_compatibility.py`.

## Trust boundaries validated

### Artifact state

```text
schema DAG + artifact bytes + evidence bytes
                  ↓
         deterministic SHA bindings
                  ↓
     fresh / stale / missing / blocked
                  ↓
        transitive downstream state
```

No provider or model decides artifact freshness, dependency identity, or evidence validity.

### Workflow composition

```text
base workflow / inherited workflow
             ↓
org → repo → user overlays + safe hooks
             ↓
typed WorkflowComponent expansion
             ↓
existing strict workflow step parser
             ↓
governance + orchestrator
```

Organization controls cannot be removed by a lower layer. Components and overlays do not redefine semantic agents or provider policy.

### Plugin execution

```text
strict PluginStep manifest
      + layered permission policy
                  ↓
          deterministic prepare
                  ↓
      trusted registered executor
                  ↓
 restricted filesystem/env/argv services
                  ↓
       structured result evidence
```

YAML cannot import executor code, introduce a shell primitive, or bypass organization denial.

### Worktree execution

```text
clean named Git baseline
          ↓
commit/tree/repository evidence
          ↓
detached verified worktree
          ↓
dedicated isolated branch
          ↓
existing SDAI Orchestrator/policy/provider controls
          ↓
preserve dirty work or safely remove clean work
```

Worktree isolation supplements rather than replaces provider sandboxing, protected paths, approvals, or enterprise policy.

## Merge criteria

The final #79 PR must not merge unless:

- its exact latest head is mergeable;
- no unresolved actionable review finding remains;
- all four CI matrix jobs pass on the exact latest head;
- the entire repository test suite runs with no older release/compatibility test disabled or weakened;
- the new `tests/test_v08_release_compatibility.py` passes on Windows and Linux;
- any defect found by the integrated gate is fixed in the underlying capability rather than hidden by weakening the release test;
- the final release-gate PR remains evidence/docs/tests only unless a real blocker requires a separately reviewed runtime correction.

## After this gate

Once #79 merges cleanly and the parent checklist is verified, issue #15 can close as **0.8 implementation-complete**. Publishing/tagging a package remains a separate intentional release operation.

The next roadmap slice is 0.9 cross-artifact analysis: deterministic relationship analysis and structured findings over requirements, architecture, contracts, tasks, tests, threats, approvals, and artifact freshness.
