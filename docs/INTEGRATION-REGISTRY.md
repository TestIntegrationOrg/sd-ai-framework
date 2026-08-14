# SDAI Integration Registry v1

The SDAI 0.13 Integration registry resolves `sdai.integration-manifest/v1` definitions across framework, signed Pack, organization, repository, and user sources without depending on filesystem enumeration or caller registration order.

Canonical registry snapshots use:

```text
sdai.integration-registry/v1
```

Exact resolved records use:

```text
sdai.integration-resolution/v1
```

## Authority layers

The registry reuses SDAI's established extension layers, from lower to higher precedence:

1. `builtin`
2. `pack`
3. `org`
4. `repo`
5. `user`

Precedence chooses provenance only when the exact `id@version` manifest content is identical. It never turns a higher layer into permission to mutate an existing exact identity.

## Exact identity immutability

An Integration's exact identity is `id@version`.

If the same exact identity appears in multiple layers, every canonical manifest SHA-256 must match. Identical content is accepted, the highest-precedence provenance is selected, and all provenance remains visible. Different canonical content for the same exact identity fails closed with `SDAI-INTEGRATION-REG-003`.

Two definitions of the same exact identity in the same layer are ambiguous even when their bytes are identical and fail with `SDAI-INTEGRATION-REG-002`. Authors must remove the duplicate instead of relying on source enumeration order.

## Version resolution

Exact version requests resolve that exact SemVer identity. An unqualified request resolves the greatest SemVer precedence.

SemVer build metadata does not affect precedence. Therefore two available versions such as `2.0.0+a` and `2.0.0+b` are both valid exact identities but make unqualified `latest` ambiguous. SDAI fails with `SDAI-INTEGRATION-REG-004` and requires the caller to choose an exact version rather than inventing a build-metadata ordering.

## Authoritative locks

Only `builtin` and `org` Integration sources may be locked.

A lock is by Integration **id**, not merely one version. Once an authoritative layer locks an id, every higher-precedence definition of that id is rejected, including a different version. This prevents repository or user configuration from bypassing enterprise policy by publishing a new version number.

Lower-precedence definitions may remain visible beneath an organization lock. The lock controls higher-layer override, matching SDAI's non-weakening configuration model.

## Source discovery

`IntegrationSource` identifies:

- a filesystem discovery root,
- an authority layer,
- a stable provenance/source label, and
- whether the source is authoritatively locked.

Discovery recursively recognizes:

- `*.integration.yaml`
- `*.integration.yml`
- `*.integration.json`

Files and directories are sorted with stable portable keys. Symlinked directories are not traversed. A symlinked source root fails closed, and a matching symlinked manifest is rejected by the Integration manifest loader.

Absolute discovery-root paths are intentionally excluded from canonical resolution/provenance JSON. Provenance records only the stable source label and the manifest path relative to that root, so equivalent catalogs can produce the same canonical registry bytes and hash on different machines.

## Provenance

Every resolved exact version includes:

- selected authority layer,
- stable source label,
- relative manifest path,
- manifest SHA-256,
- lock state, and
- the complete ordered provenance chain for identical exact content across layers.

This is the information later lifecycle/status commands use for explainability and audit without relying on process-local paths.

## Discovery APIs

The provider-neutral registry exposes deterministic programmatic operations for later CLI work:

- `resolve(id, version=None)` / `info(...)`
- `list_versions(id)`
- `list_resolved()` for one current version per Integration id
- `search(query)` over id/display name/description/capabilities
- `list_all_exact()` for canonical registry snapshots

`search("")` and `list_resolved()` are ordered by Integration id. Version lists use SemVer precedence with an exact-string tie break only for display/enumeration; unqualified resolution still rejects equal-precedence build variants.

## Stable error families

- `SDAI-INTEGRATION-REG-001` — malformed registry input, id/version/source/provenance data.
- `SDAI-INTEGRATION-REG-002` — duplicate exact identity in one layer.
- `SDAI-INTEGRATION-REG-003` — conflicting canonical content for one exact identity.
- `SDAI-INTEGRATION-REG-004` — ambiguous unqualified version resolution.
- `SDAI-INTEGRATION-REG-005` — invalid authoritative lock or blocked higher-layer definition.
- `SDAI-INTEGRATION-REG-006` — unsafe or missing discovery root.

These contracts are shared SDK behavior. They contain no `if integration == ...` provider branches; ecosystem expansion in later 0.13 slices is expected to add manifests and catalog data rather than new registry logic.
