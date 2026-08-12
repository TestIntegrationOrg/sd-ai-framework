# SDAI 0.6 Foundation — Release Readiness

This document is the acceptance map for issue #35 and the 0.6 foundation parent (#13). It records what must remain true before the implementation slice is considered complete. It is not a statement that a package/tag has already been published.

## Release posture

- Package version remains whatever `sdai.__version__` declares until an intentional release cut.
- `pyproject.toml`, README release metadata, console version output, and generated `.sdai/framework-version.yaml` are guarded by the deterministic version-sync tests added in #34.
- A future `0.6.0` release changes the authoritative version once and updates the guarded README release metadata as documented in `docs/RELEASING.md`.

## Compatibility acceptance matrix

| Area | Required evidence |
|---|---|
| Full regression | Entire `pytest -q` suite succeeds; CI runs Ubuntu/Windows × Python 3.11/3.12. |
| Installation/package metadata | Each CI job successfully installs `pip install -e '.[dev]'` before tests. |
| Windows/Linux paths | Extension scaffolding emits POSIX-style repository-relative paths; release journey runs in a workspace containing spaces and non-ASCII characters. |
| UTF-8 | Feature intake, skill content, and upgrade compatibility include `Ω`, `Δ`, and `café` content and round-trip with UTF-8. |
| Extension authoring | `sdai create` + `sdai extensions validate` + runtime resolution succeed for a skill, semantic agent, and workflow. |
| Skill/agent compatibility | Existing plural `sdai skills` and `sdai agents` namespaces remain functional alongside singular eval commands. |
| Behavioral evaluation | A generated skill executes a deterministic baseline/candidate eval and proves measurable improvement. |
| Engineering constitution | Constitution initialization remains available on a freshly initialized v0.5-compatible project. |
| Requirements quality | Complete requirements pass deterministic `requirements check`; clarification artifacts are generated without rewriting canonical specs. |
| Enterprise workflow | Existing `enterprise` workflow lists nested design-review steps and manual `architecture-review --dry-run` still resolves the semantic agent/capability. |
| Organization lock | An authoritative locked organization extension rejects weaker repository override even when source input order is reversed. |
| Upgrade compatibility | `sdai upgrade` preserves customized canonical agent content and legacy `.sdai/skills` content. |
| Version metadata | Init/upgrade writes the authoritative framework version to `.sdai/framework-version.yaml`. |

Primary end-to-end evidence: `tests/test_v06_release_compatibility.py`.

## 0.6 foundation capabilities covered

The release gate covers the capabilities delivered through the 0.6 implementation sequence:

1. Versioned extension manifest and registry core.
2. Existing semantic-agent/shared-skill registry migration with backward compatibility.
3. Safe extension scaffolding and validation CLI.
4. Engineering constitution, clarification, and requirements-quality checks.
5. Behavioral skill/agent eval runner.
6. Deterministic version/status synchronization.
7. Cross-platform compatibility validation and release evidence.

## Merge criteria

The final #35 PR must not merge unless:

- its exact latest head is mergeable;
- no unresolved actionable review finding remains;
- all four CI matrix jobs pass on the exact latest head;
- no existing v0.5 test is disabled, deleted, or weakened to obtain a green result;
- failures discovered by the release journey are fixed in runtime code or accurately scoped as a release blocker.

## After this gate

Once #35 merges cleanly, parent #13 can be closed as implementation-complete. The next roadmap slice is 0.7: current-state/delta specifications plus language/technology skill foundations. Publishing a package/tag is a separate intentional release action.
