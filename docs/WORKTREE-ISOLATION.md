# Git Worktree Isolation

Issue #78 adds a Git worktree execution mode for `sdai run`.

```bash
sdai run SIGN-123 --workflow enterprise --isolation worktree
```

Normal execution remains unchanged:

```bash
sdai run SIGN-123 --workflow enterprise
```

## Purpose

Worktree mode isolates workflow changes from the engineer's source working tree. SDAI verifies an exact clean Git baseline, creates a dedicated branch/worktree from that commit, executes the workflow with the isolated path as the SDAI project root, and records deterministic Git evidence outside tracked source files.

The worktree contains the same committed `.sdai/**`, `.agents/**`, provider profiles, policies, workflows, skills, and protected-path configuration as the verified baseline. Existing provider/workspace-write controls therefore continue to operate relative to the isolated root.

## Baseline gate

Before a worktree is created, SDAI requires all of the following:

- `--path` is exactly the Git repository root.
- HEAD is attached to a named branch.
- tracked and untracked working-tree status is empty.
- the exact HEAD commit and tree can be resolved.
- the Git common directory can be resolved.
- tracked `.sdai/config.yaml` exists in the resulting worktree.

Dirty or detached baselines fail closed. Version 1 intentionally has no `--force-dirty` escape hatch; a future enterprise policy may explicitly govern such behavior.

SDAI strips Git environment variables such as `GIT_DIR`, `GIT_WORK_TREE`, `GIT_INDEX_FILE`, object-directory overrides, and command-line Git configuration injection before running its verification commands.

## Worktree allocation

By default, worktrees are created in a sibling directory:

```text
<source-parent>/.<repo-name>.sdai-worktrees/<FEATURE>/<RUN-ID>/
```

An administrator/operator may set:

```text
SDAI_WORKTREE_ROOT=/absolute/path/to/worktrees
```

The configured root must be absolute and must not be inside the source workspace.

Each run receives a unique branch:

```text
sdai/<FEATURE>/<RUN-ID>
```

The worktree HEAD commit/tree is verified against the source baseline immediately after creation.

## Evidence

Worktree evidence is written under the repository Git metadata rather than into the source working tree:

```text
<git-common-dir>/sdai/worktree-evidence/<FEATURE>/<RUN-ID>.json
```

Evidence includes:

- repository identity hash
- source repository root and optional origin URL
- source branch
- baseline commit and tree
- source cleanliness hash
- isolated worktree path and branch
- worktree final commit/tree
- worktree status hash/dirty state
- outcome (`success`, `failed`, `paused`, `cancelled`)
- cleanup disposition
- failure/cleanup details when applicable

The evidence record itself does not make the source Git working tree dirty.

## Preservation and cleanup

The default is intentionally conservative:

| Outcome | Worktree clean | Worktree dirty |
|---|---|---|
| success | preserve | preserve |
| paused | preserve | preserve |
| failed | remove worktree + branch | preserve |
| cancelled | remove worktree + branch | preserve |

SDAI never automatically discards dirty isolated work. If execution fails after producing implementation changes, the worktree and branch remain available for investigation/recovery.

For a successful workflow that produced no changes, explicit cleanup is available:

```bash
sdai run SIGN-123 --isolation worktree --cleanup-worktree
```

`--cleanup-worktree` still refuses to remove a dirty worktree.

## Security boundary

Worktree isolation is not a replacement for provider sandboxing, organization policy, approvals, or protected-path enforcement. It adds a Git/filesystem isolation layer around those existing controls.

The isolated `Orchestrator` receives the worktree path as its project root. External providers therefore continue to receive the same protected-path snapshots, policy restrictions, profile controls, and workspace-write enforcement, but any allowed implementation changes occur in the isolated worktree instead of the source workspace.

No shell interpolation primitive is introduced. All Git operations are literal argv calls with `shell=False`.

## Current v1 limitations

- Worktree mode is exposed for full `sdai run` workflow execution in this slice; manual-step isolation can be added later using the same service.
- A successful dirty worktree is intentionally preserved; SDAI does not automatically commit or push it.
- The baseline must already contain the feature/specification/configuration required by the workflow. Uncommitted feature artifacts are considered a dirty baseline and are rejected.
- Evidence is local Git metadata; signed/immutable enterprise provenance is a later roadmap milestone.
