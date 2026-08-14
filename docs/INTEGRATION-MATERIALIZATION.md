# Integration Native Materialization

SDAI Integration manifests can declare native projections for skills, commands, and agent files. The materialization engine turns those declarative mappings into tool-native files while keeping ownership, provenance, and recovery deterministic.

## Safety model

Materialization is fail-closed. SDAI never silently overwrites or adopts an unmanaged destination, even when the existing bytes happen to match the desired content. A destination becomes managed only through a successful materialization operation or through recovery of an interrupted operation whose journal proves ownership of the exact planned bytes.

Managed destinations and their source paths must remain portable project-relative paths. Traversal, Windows-reserved path segments, non-portable separators, unsafe symlink ancestry, source/target overlap, and targets inside `.sdai/integrations` are rejected.

All managed content is written atomically in the destination directory. The operation journal is persisted before managed writes and removed only after install state commits. A retry may recover only when the existing journal matches the exact Integration identity, manifest hash, planned writes, and planned deletes.

## Versioned state contracts

Materialized state is stored under `.sdai/integrations/`:

- `install-state.json` uses `sdai.integration-install-state/v1`.
- `operation.json` uses `sdai.integration-operation-journal/v1` while an operation is in progress.
- status reports use `sdai.integration-status/v1`.

Each installed Integration records its Integration id, exact `id@version` identity, manifest SHA-256, selected registry provenance, managed destination path, source path, projection kind, managed content SHA-256, and preserved user-modified paths.

State JSON is canonical UTF-8 JSON. Duplicate JSON keys, malformed records, invalid hashes, or unsafe persisted paths fail closed instead of being normalized silently.

## Status model

Status is deterministic and explainable at file level:

| Status | Meaning |
|---|---|
| `exact` | Managed bytes and desired bytes match. |
| `missing` | A desired managed destination is absent. |
| `stale` | The existing managed file is still clean, but the desired source bytes or Integration metadata changed. |
| `modified` | A managed destination differs from the last bytes SDAI wrote. SDAI treats it as user/tool-owned modification and will not overwrite it. |
| `unmanaged-conflict` | A desired destination already exists but is not owned by the Integration. |
| `broken` | Source/destination/state is unsafe, unreadable, symlinked, malformed, or otherwise cannot be reasoned about safely. |

A missing or clean stale destination can be repaired. A modified, unmanaged-conflict, or broken destination requires explicit human resolution.

## Install and upgrade behavior

`materialize_integration(...)` installs a new Integration or upgrades an existing Integration id to the exact resolved manifest. Directory projections recursively map source files under the target directory; single-file projections map the source directly to the target path.

For upgrades:

- clean managed files may be replaced with new declared bytes;
- clean obsolete managed files may be removed;
- user-modified obsolete files are preserved and recorded;
- user-modified desired files are never overwritten;
- unmanaged destinations are never adopted.

Repeated exact materialization is byte/state idempotent.

## Repair behavior

`repair_integration(...)` repairs only state SDAI can prove it owns. It can restore missing managed files and replace clean stale files with the current desired bytes. It refuses to overwrite modified or unmanaged content.

If an earlier install/upgrade/repair crashed after writing files but before committing state, a retry may continue only when the operation journal matches and each interrupted output still has the exact planned SHA-256. Tampered interrupted output blocks recovery.

If state committed but journal cleanup was interrupted, a retry recognizes the already-committed exact record and clears the matching stale journal without rewriting native files.

## Remove behavior

`remove_integration(...)` deletes only files whose current bytes still match the managed SHA-256. Missing files are harmless. Modified or unsafe managed paths are preserved and returned to the caller; SDAI does not delete them merely because they were once managed.

## Cross-platform contract

Projection paths are canonical POSIX-style relative paths in manifests and state on every platform. Filesystem operations translate those paths through `pathlib` without persisting machine-specific absolute paths. UTF-8 and Windows/Linux path behavior are covered by the repository CI matrix on Python 3.11 and 3.12.
