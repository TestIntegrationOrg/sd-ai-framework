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

The plan contains before/after SHA-256 evidence and a deterministic `planSha256`. Repeating the plan against an unchanged project returns the same JSON and hash. If an earlier apply was interrupted after its durable prepare point, planning fails closed until `sdai migrate apply` or `sdai upgrade` performs recovery.

## Apply a migration

```text
sdai migrate apply --path .
sdai migrate apply --path . --json
```

Before the first project write, SDAI revalidates every planned path and current hash. If any target appeared, disappeared, changed, or became unsafe after planning, the apply fails and asks the operator to re-plan.

Apply uses a durable prepare/commit transaction at the managed file-set boundary:

1. exact pre-upgrade bytes for stock replacements are copied into the migration evidence directory;
2. an integrity-bound canonical prepared manifest is durably written **before the first managed target write**;
3. every managed target is written atomically and its resulting hash is verified;
4. an integrity-bound commit receipt is written only after every target has reached its expected post-migration hash;
5. an ordinary apply error automatically restores recorded pre-migration state before the command fails.

The prepared manifest closes the process-interruption gap that cannot be handled by an in-process exception handler. If the process stops after preparation but before commit, the next apply/upgrade detects the incomplete transaction before computing a new plan. SDAI accepts only target bytes that exactly match the transaction's recorded pre-migration or post-migration hashes, restores the pre-migration state, records an integrity-bound recovery receipt, and then plans the requested upgrade again.

Recovery is restart-safe. If recovery itself is interrupted, another retry can continue only while every target remains in one of the same two recorded states. If a target contains unknown/operator-owned bytes, recovery fails closed instead of overwriting them.

A record containing only backups and no prepared manifest is safe to discard because this protocol never writes a managed target before the manifest is durable. Multiple simultaneous incomplete transaction records, non-canonical evidence paths, invalid receipts, unsafe symlinks, missing backups, or unsupported transaction protocols fail closed for operator review.

The stable public result contract remains `sdai.migration-result/v1`. A successful apply returns a `migrationId`, `planSha256`, and manifest path. A project already at the current scaffold returns `status=current` and creates no migration record. Commit/recovery receipts are internal integrity evidence and do not change the established result JSON contract.

## Roll back

```text
sdai migrate rollback MIGRATION_ID --path .
sdai migrate rollback MIGRATION_ID --path . --json
```

Rollback is intentionally conservative. Only a committed migration is eligible for rollback. An interrupted prepared transaction must first be recovered by `sdai migrate apply` or `sdai upgrade`.

Before changing **any** committed target, SDAI verifies the entire recorded migration:

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

Repeating the rollback returns `already-rolled-back` and performs no project mutation. The stable rollback contract is `sdai.migration-rollback/v1`. Migration manifests produced before the prepare/commit protocol remain backward-compatible and continue to use their historical manifest-as-commit semantics.

## Existing `sdai upgrade`

`sdai upgrade --path .` uses the same crash-recoverable apply engine. Its human output remains compatible with the pre-1.0 command: scaffold additions/replacements are shown as `+` paths and the framework-version footer is emitted once. Migration manifests and transaction receipts are intentionally not injected into that legacy display, but they are retained on disk for rollback/recovery evidence.

For automation that needs migration IDs, hashes, or rollback, prefer the explicit `sdai migrate ... --json` surface.

## Package-install release evidence

The 1.0 release matrix does not rely only on editable imports. After the full pytest suite, every supported OS/Python leg builds an SDAI wheel, installs it and its dependencies into a fresh virtual environment, invokes the installed `sdai` console entrypoint outside the repository tree, and proves brownfield initialization, user-content preservation, idempotent upgrade, committed migration, and rollback. See `docs/releases/1.0-install-upgrade-rollback.md`.

## Security boundaries

Migration plan/apply targets are restricted to SDAI-managed `.sdai` and `.agents` paths. The evidence namespace `.sdai/migrations` is excluded from scaffold planning and has a separate internal resolver. Managed symlinks are never followed for writes; if a current scaffold change would require traversing one, migration fails closed.

Migration manifests and transaction receipts are integrity evidence, not identity signatures. They detect accidental or local byte drift but do not implement identity-backed authorization, signing, SSO/OIDC identity, or distinct-approver guarantees. Those held 0.18/#25 capabilities remain explicitly outside this work.
