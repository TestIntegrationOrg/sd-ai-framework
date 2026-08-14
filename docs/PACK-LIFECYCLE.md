# SDAI Pack Lifecycle Contract

SDAI 0.12.5 adds deterministic lifecycle operations on top of the canonical Pack manifest, exact resolver lock, integrity, catalog, and trust contracts.

The lifecycle surface is:

```text
sdai pack install <publisher/id> --lock FILE --source DIR [--local-link] [--json]
sdai pack update  <publisher/id> --lock FILE --source DIR [--local-link] [--json]
sdai pack remove  <publisher/id> [--json]
sdai pack outdated --lock FILE [--json]
sdai pack info <publisher/id> --catalog FILE [--catalog FILE ...] [--json]
sdai pack search [QUERY] --catalog FILE [--catalog FILE ...] [--json]
```

## Exact lockfile authority

`install` and `update` do not select a version from a directory name, provider response, or catalog at materialization time. The requested `publisher/id` must occur exactly once in the supplied `sdai.pack-lock/v1` file.

The local artifact is accepted only when all of the following match that exact lock entry:

- exact `publisher/id@version`,
- canonical manifest SHA-256, and
- canonical Pack content SHA-256.

The lifecycle state records the lock SHA-256 used for the operation. `outdated` reports a Pack when its identity, manifest hash, content hash, or recorded lock hash no longer matches the supplied exact lock.

Catalog and enterprise trust evaluation remain separate earlier-stage contracts. A production caller should materialize only an artifact whose resolved catalog entry and trust decision were already accepted. The lifecycle layer never invents trust from a local path.

## Managed materialization boundary

Pack bytes are materialized under a framework-owned namespace:

```text
.sdai/installed-packs/<publisher>/<id>/<version>/...
```

This intentionally avoids writing into arbitrary repository paths. Every file is recorded in `.sdai/packs/install-state.json` with:

- exact Pack identity and coordinate,
- install mode,
- source provenance,
- manifest/content/lock hashes,
- Pack source-relative path,
- materialized path and exact file SHA-256, and
- preserved user-modified paths from older managed content.

All lifecycle state uses canonical UTF-8 JSON. State and managed output paths reject symlink ancestry and path traversal.

## User edits and unmanaged content

SDAI never silently overwrites or deletes bytes it cannot prove it owns in the expected state.

On install/update:

- an existing destination with no matching managed-file provenance is rejected,
- a previously managed file whose bytes differ from its recorded SHA-256 is treated as user-modified and is not overwritten, and
- obsolete clean files may be removed, while obsolete modified files are preserved and recorded as preserved paths.

On remove:

- clean managed files are removed,
- missing files are already idempotently absent, and
- modified managed files are preserved and returned to the caller.

After preservation, those edited bytes are no longer considered removable Pack content.

## Crash recovery and idempotency

Lifecycle mutations use atomic file replacement for both managed files and state. Before materialization starts, SDAI writes `.sdai/packs/operation-journal.json` containing the exact coordinate, identity, operation, destination paths, and expected hashes.

If a process terminates after writing some bytes but before committing install state, the journal remains. A retry for the same exact operation may adopt only leftover files whose bytes still match the journal hash. A mismatch fails closed. An unrelated Pack operation is blocked until the interrupted operation is recovered.

Removal is also resumable: already-removed clean files are treated as absent and the remaining recorded managed files are processed again safely.

The journal is cleared only after the new install state is committed.

## Local-link development mode

`--local-link` is explicit development provenance. It does **not** weaken exact-lock verification: the local Pack must still match the selected lock identity, manifest hash, and content hash.

The install record is distinguishable from production materialization:

```json
{
  "apiVersion": "sdai.pack-local-link/v1",
  "mode": "local-link",
  "localPath": "/absolute/development/path",
  "source": "local-link:/absolute/development/path"
}
```

For portability and path safety, SDAI does not create operating-system symlinks for local-link mode. It materializes a verified snapshot and records the development source path. Re-running `pack update ... --local-link` refreshes that snapshot after the lock has been regenerated for the changed local Pack.

This makes local development explicit without making runtime behavior depend on platform-specific symlink semantics or permitting a mutable local directory to bypass integrity checks.

## Discovery commands

`pack search` and `pack info` consume deterministic local `sdai.pack-catalog/v1` files. CLI-supplied catalogs are treated as repository-scoped discovery inputs. Organization/repository/user scope composition remains available through the framework API where provenance can be supplied explicitly.

Network retrieval remains outside the deterministic lifecycle contract.

## JSON and exit semantics

With `--json`, successful command payloads are one canonical JSON object on stdout; diagnostics are emitted on stderr by the top-level CLI.

Stable command result codes are:

| Code | Meaning |
|---:|---|
| `0` | operation succeeded / installed state is exact |
| `1` | validation, I/O, safety, integrity, or lifecycle error |
| `2` | `pack outdated` found one or more non-exact installed Packs |
| `3` | `pack info` found no matching catalog entry |

`pack remove` is idempotent: removing a coordinate that is not installed succeeds with an empty preserved-path set.

## Security boundary

The lifecycle contract is deliberately fail-closed:

- no arbitrary destination paths,
- no symlink/reparse-style ancestry through managed paths,
- no overwrite of unmanaged files,
- no deletion of modified files,
- no update from bytes that disagree with the exact lock,
- no silent recovery of crash leftovers whose hashes changed, and
- no treatment of `local-link` as production trust evidence.

These invariants are provider-neutral and apply on Windows, Linux, and macOS.