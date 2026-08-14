# Integration Lifecycle CLI

SDAI 0.13 exposes the declarative Integration SDK through `sdai integration ...`. The CLI delegates discovery to the Integration registry and native-file changes to the managed materialization lifecycle; it does not implement a parallel provider-specific install path.

## Commands

```text
sdai integration search [QUERY]
sdai integration info <id> [--version VERSION]
sdai integration install <id> [--version VERSION]
sdai integration use <id> [--version VERSION]
sdai integration status <id> [--version VERSION]
sdai integration repair <id> [--version VERSION]
sdai integration upgrade <id> [--version VERSION]
sdai integration remove <id>
```

Every command accepts `--path PATH`. Registry-backed commands also accept source overrides for `builtin`, `pack`, `org`, `repo`, and `user` layers.

## Registry sources

The CLI composes the same deterministic authority order as the Integration registry:

```text
builtin < pack < org < repo < user
```

Default discovery locations are:

- framework: packaged `sdai/builtin_integrations` when present;
- installed Packs: `.sdai/installed-packs`;
- organization: `SDAI_ORG_INTEGRATIONS_PATH` when configured;
- repository: `.sdai/integrations/manifests`;
- user: `SDAI_USER_INTEGRATIONS_PATH`, otherwise `~/.sdai/integrations`.

For explicit automation/test inputs, use:

```text
--builtin-source DIR
--pack-source DIR
--org-source DIR
--repo-source DIR
--user-source DIR
```

An explicit source must exist and be a directory; it is never silently ignored. Organization sources are treated as authoritative locked sources. Built-in manifests use the registry's normal built-in authority and may opt into locking through framework-owned registry construction rather than the CLI inventing per-manifest overrides.

## Version semantics

`search` and unqualified `info` use normal registry latest resolution. Equal-precedence SemVer build variants remain ambiguous and fail closed exactly as the registry API does.

`install` is intentionally conservative. If a different version of the same Integration id is already installed, `install` returns actionable state instead of silently changing versions. Use `upgrade` for an intentional version transition.

`repair` always targets the installed exact version. Omitting `--version` does **not** mean latest for repair. This prevents a repair operation from becoming an accidental upgrade. If `--version` is supplied, it must match the installed version.

`upgrade` requires an existing installed Integration. Without `--version`, it resolves the registry's current unambiguous latest version; with `--version`, it targets that exact version.

## Selection (`use`)

`sdai integration use` selects an already installed exact Integration and writes only:

```text
.sdai/integrations/selection.json
```

The versioned selection contract is:

```text
sdai.integration-selection/v1
```

It records exact Integration identity, version, manifest SHA-256, and registry provenance. `use` does not rewrite `.sdai/agents.yaml`, `.sdai/policy.yaml`, provider profiles, or unrelated agent policy. Removing the selected Integration clears its matching selection record.

## Status and repair

`status` preserves both installed state and desired registry resolution in its JSON response. Its underlying file report uses the materialization statuses:

- `exact`
- `missing`
- `stale`
- `modified`
- `unmanaged-conflict`
- `broken`

Only `exact` exits successfully. Other resolved statuses are actionable and return exit code `2`.

`repair` delegates to `repair_integration(...)`; it may restore missing or clean-stale managed files, but refuses modified, unmanaged, broken, or otherwise unsafe content. User-modified bytes are never overwritten merely to make status green.

## Remove

`remove` delegates to the same managed-file ownership state as materialization. Clean managed files may be removed; modified or unsafe files are preserved and returned in `preservedPaths`. Repeating an already-completed remove is idempotent.

## Stable JSON contracts

`--json` writes exactly one compact UTF-8 JSON object plus one trailing newline to stdout for command-level success, actionable, not-found, and handled error results. Diagnostics for those handled JSON operations do not leak to stderr.

Current contracts are:

| Command/result | `apiVersion` |
|---|---|
| search | `sdai.integration-search/v1` |
| info | `sdai.integration-info/v1` |
| lifecycle result | `sdai.integration-lifecycle-result/v1` |
| status command | `sdai.integration-status-command/v1` |
| selection state | `sdai.integration-selection/v1` |
| handled CLI error | `sdai.integration-cli-error/v1` |

Resolution payloads embed the existing `sdai.integration-resolution/v1` contract, and installed records embed the existing `sdai.integration-install-state/v1` record shape. This keeps manifest hashes and provenance visible to CI without duplicating those schemas.

## Exit codes

The Integration CLI reserves deterministic scripting meanings:

| Exit | Meaning |
|---:|---|
| `0` | command succeeded; status is exact where applicable |
| `2` | actionable lifecycle/status state exists (for example stale, different version installed, or orphaned install) |
| `3` | requested Integration/version was not found |
| `4` | handled registry/materialization/manifest/CLI safety or validation error |

Human mode prints ordinary success/status text to stdout and handled errors to stderr. JSON mode keeps stdout machine-clean and returns the structured error object on stdout.

## Safety inheritance

The CLI does not weaken Integration SDK invariants:

- exact registry identity and provenance rules still apply;
- authoritative registry locks still apply;
- unmanaged destinations are never silently adopted;
- modified managed files are not overwritten or deleted;
- path traversal and symlink ancestry checks still apply;
- crash/retry recovery still comes from the materialization journal/state contracts;
- `use` does not grant execution permissions or mutate provider policy.

Adding another Integration remains manifest/catalog work rather than a new `if tool == ...` CLI branch.
