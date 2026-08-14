# SDAI Pack Dependency Resolution and Lockfile

SDAI 0.12 resolves `sdai.pack-manifest/v1` dependency constraints into an exact, reproducible lock contract:

```text
sdai.pack-lock/v1
```

The resolver is deterministic and framework-owned. Providers may discover or propose Pack candidates later, but provider output does not decide canonical dependency truth.

## Candidate truth

A resolver candidate binds:

- an exact `PackManifest`,
- a stable source/catalog identity, and
- the SHA-256 of the exact Pack content represented by that candidate.

The manifest already carries `publisher/id@version` identity and its canonical manifest SHA-256. A lock entry therefore records both manifest and content digests.

Two candidates with the same exact `publisher/id@version` are accepted as duplicates only when source, content SHA-256, and manifest SHA-256 are identical. Conflicting truth for the same exact identity is ambiguous and fails closed.

## Deterministic resolution

Root Packs are exact selections. Their transitive dependencies use the SemVer constraints defined by `sdai.pack-manifest/v1`.

Resolution:

1. accumulates every constraint by Pack coordinate,
2. visits unresolved coordinates in sorted order,
3. considers eligible versions by highest SemVer precedence first,
4. uses exact version/source/content/manifest truth as the deterministic tie-break for equal precedence,
5. backtracks when a candidate introduces a transitive conflict, missing dependency, or dependency cycle, and
6. emits one exact selected version per coordinate.

This is intentionally a backtracking resolver rather than a greedy sorter. A higher version can be locally valid while making the complete graph unsatisfiable.

If no candidate graph satisfies all constraints, resolution fails explicitly. Missing dependencies, incompatible constraints, ambiguous exact identities, and cycles have separate stable error classes/codes.

## Lock contract

A lock contains:

- `apiVersion: sdai.pack-lock/v1`,
- exact root identities, and
- one package entry per resolved coordinate.

Each package entry records:

- `publisher`, `id`, and exact `version`,
- stable `source`,
- canonical `manifestSha256`,
- exact `contentSha256`, and
- exact dependency identities (`publisher/id@version`).

Roots, packages, and dependency identities are canonically sorted. Unknown fields, duplicate JSON object names, malformed IDs/versions/hashes, missing referenced entries, noncanonical ordering, and dependency cycles fail closed when a lock is loaded.

`PackLock.sha256` hashes canonical JSON without the trailing file newline. It identifies semantic lock truth.

## Re-resolution and outdated detection

Re-resolve from the current candidate universe to produce an expected `PackLock`, then compare it with the stored lock using `compare_pack_lock()`.

Comparison is read-only. It reports stable differences such as:

```text
missing:publisher/id
extra:publisher/id
changed:publisher/id
changed:roots
```

A parent entry is also `changed` when one of its exact dependency identities changes. Comparison never silently updates the existing lock.

## Atomic and coordinated writes

`write_pack_lock()` writes canonical UTF-8 JSON plus one trailing newline through a same-directory temporary file, flushes/fsyncs the temporary file, and replaces the target atomically with `os.replace`.

All SDAI lock writers coordinate the complete **read → expected-hash check → temporary write → replace** critical section with an operating-system exclusive lock on a stable sibling guard file:

```text
.<lock-file-name>.write-lock
```

POSIX uses `flock`; Windows uses the corresponding byte-range file lock. The guard file intentionally remains present so concurrent and later processes continue locking the same stable filesystem object. A symlinked guard fails closed.

This coordination prevents two SDAI writers that start from the same old hash from both committing updates: one writer succeeds; the next writer reacquires the guard, observes the new exact bytes, and fails its stale-hash check. Non-cooperating external processes that overwrite the file outside SDAI are outside this lock protocol, but the next SDAI operation will observe their bytes/hash rather than treating them as the expected state.

Writing identical bytes is an idempotent no-op once the guard is held.

Replacing a different existing lock requires `expected_current_sha256`. This value is the SHA-256 of the **exact current file bytes**, including the trailing newline, not `PackLock.sha256`. Use:

```python
expected = pack_lock_file_sha256(path)
write_pack_lock(path, new_lock, expected_current_sha256=expected)
```

This stale-write guard is evaluated while holding the exclusive write guard. It prevents an ordinary re-resolution call from silently mutating an existing lock and prevents cooperating concurrent SDAI writers from losing an intervening update.

Lock paths that are symlinks are rejected. Directories and unreadable/non-file targets also fail with controlled Pack-lock errors.

## Scope boundary

This slice resolves deterministic dependency truth and persists exact source/integrity metadata. It does **not** claim that a recorded content digest or publisher is trusted.

Signature verification and publisher provenance are added by #136. Trusted catalog and enterprise publisher policy are added by #137. Installation consumes the trusted locked result later in #138.
