# SDAI 1.0 upgrade and migration safety

SDAI 1.0 treats project upgrades as an explicit, reversible migration boundary rather than an opaque scaffold rewrite.

## Compatibility model

The public `sdai upgrade --path PATH` command keeps its historical grammar and user-facing behavior, but now executes through the safe migration engine. The engine preserves the established scaffold rules:

- missing SDAI-managed files may be added;
- team-customized files are preserved;
- a small set of older stock scaffold files may be replaced only when their bytes still match the exact prior SDAI stock definition;
- `.sdai/framework-version.yaml` remains framework-managed metadata;
- migration planning never mutates the project;
- migration evidence lives under `.sdai/migrations/` and is excluded from future scaffold plans.

The migration engine does **not** rewrite arbitrary repository files, application source, current specifications, team-owned policy, custom workflows, or data stores.

## Preview an upgrade

```text
sdai migrate plan --path .
sdai migrate plan --path . --json
```

`plan` runs the existing current-scaffold installers against a safe temporary mirror of only the SDAI-managed roots (`.sdai` and `.agents`). It then computes the exact byte delta without writing to the real workspace.

The stable JSON contract is `sdai.migration-plan/v1`. Each change is one of:

- `create` — the managed path does not currently exist;
- `replace-stock` — the current bytes exactly match an older known SDAI stock file and the current scaffold has a newer stock version.

The plan contains before/after SHA-256 evidence and a deterministic `planSha256`. Repeating the plan against an unchanged project returns the same JSON and hash.

## Apply a migration

```text
sdai migrate apply --path .
sdai migrate apply --path . --json
```

Before the first project write, SDAI revalidates every planned path and current hash. If any target appeared, disappeared, changed, or became unsafe after planning, the apply fails and asks the operator to re-plan.

Apply behavior is transactional at the file set boundary:

1. exact pre-upgrade bytes for stock replacements are copied into the migration evidence directory;
2. every managed target is written atomically;
3. every resulting file hash is verified;
4. an integrity-bound canonical manifest is written only after all target writes succeed;
5. if an apply error occurs, already-written targets are restored before the command fails.

The stable result contract is `sdai.migration-result/v1`. A successful apply returns a `migrationId`, `planSha256`, and manifest path. A project already at the current scaffold returns `status=current` and creates no migration record.

## Roll back

```text
sdai migrate rollback MIGRATION_ID --path .
sdai migrate rollback MIGRATION_ID --path . --json
```

Rollback is intentionally conservative. Before changing **any** target, SDAI verifies the entire recorded migration:

- the canonical manifest hash is valid;
- every migrated file still exists;
- every migrated file still has the recorded post-migration hash;
- every stock-replacement backup exists and has its recorded pre-migration hash;
- every target and backup remains inside its allowed project namespace and does not traverse a symlink.

If any migrated file was edited after the migration, rollback fails before mutating any other file. SDAI never deletes or overwrites post-migration user changes merely to force a rollback.

For an unchanged migration:

- `create` targets are removed;
- `replace-stock` targets are restored to their exact pre-upgrade bytes;
- a canonical rollback receipt is written under the original migration record.

Repeating the rollback returns `already-rolled-back` and performs no project mutation. The stable rollback contract is `sdai.migration-rollback/v1`.

## Existing `sdai upgrade`

`sdai upgrade --path .` uses the same safe apply engine. Its human output remains compatible with the pre-1.0 command: scaffold additions/replacements are shown as `+` paths and the framework-version footer is emitted once. Migration manifests are intentionally not injected into that legacy display, but they are retained on disk for rollback and audit.

For automation that needs migration IDs, hashes, or rollback, prefer the explicit `sdai migrate ... --json` surface.

## Security boundaries

Migration plan/apply targets are restricted to SDAI-managed `.sdai` and `.agents` paths. The evidence namespace `.sdai/migrations` is excluded from scaffold planning and has a separate internal resolver. Managed symlinks are never followed for writes; if a current scaffold change would require traversing one, migration fails closed.

Migration manifests are integrity evidence, not identity signatures. They detect accidental or local byte drift but do not implement identity-backed authorization, signing, SSO/OIDC identity, or distinct-approver guarantees. Those held 0.18/#25 capabilities remain explicitly outside this work.
