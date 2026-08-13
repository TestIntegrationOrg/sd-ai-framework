# SDAI 0.10 Traceability Release Readiness

This document is the evidence checklist for the 0.10 **Traceability Graph + Typed Evidence Model** milestone. Package/tag publication remains a separate intentional release action.

## Exact-head release gate

0.10 is implementation-complete only when the full repository suite passes on the exact candidate head across:

- Ubuntu, Python 3.11
- Ubuntu, Python 3.12
- Windows, Python 3.11
- Windows, Python 3.12

The full suite must keep the previous compatibility gates enabled:

- `tests/test_v06_release_compatibility.py`
- `tests/test_v07_release_compatibility.py`
- `tests/test_v08_release_compatibility.py`
- `tests/test_v09_release_compatibility.py`
- `tests/test_v010_release_compatibility.py`

## 0.10 acceptance evidence

`tests/test_v010_release_compatibility.py` demonstrates the milestone as one integrated current-state journey rather than relying only on isolated unit tests.

| Requirement | Release-gate evidence |
|---|---|
| Canonical graph spans requirements through design, implementation, verification, security and approval | Complete workspace contains requirement/scenario/RFC/ADR/component/contract/threat/task/code/test/approval/evidence nodes and no unresolved gaps |
| Critical requirements can require 100% coverage | Critical policy journey requires and achieves 100% across requirements, tasks, code, tests, security and approvals |
| Stale proof never satisfies current coverage | Bound source mutation makes requirement/security/approval proof non-current and blocks critical policy |
| Missing links remain visible | Brownfield requirement referencing a missing ADR produces both unresolved-link and uncovered-requirement output |
| Provider/model identity does not define trace truth | Rewriting only evidence producer/provider/model leaves canonical graph JSON and SHA-256 unchanged |
| Organization minimums cannot be weakened | Organization/framework 100% critical requirement minimum remains effective when repo/user layers request lower values |
| Trace inspection/export is read-only | Summary, requirement, missing, coverage, policy and export commands preserve repository bytes; export equals canonical graph serialization exactly |
| Windows/Linux UTF-8 portability | Release workspace uses spaces, `Ω`, `Δ`, and `café` paths/content and runs in the complete OS/Python CI matrix |

## Compatibility expectations

0.10 does not replace earlier truth systems. It composes with them:

- 0.6 extension/governance compatibility remains intact.
- 0.7 current-specification and technology/skill compatibility remains intact.
- 0.8 artifact freshness and workflow/isolation compatibility remains intact.
- 0.9 cross-artifact analysis and durable execution compatibility remains intact.
- 0.10 trace evidence may bind 0.8 artifact state and 0.9 durable evidence while freshness is still decided deterministically from current Git/content state.

## Review rule

The #109 integration PR is evidence-only unless these end-to-end gates reveal an actual underlying product defect. Any such defect must be corrected at the narrowest responsible boundary and the complete matrix rerun on the new exact head.
