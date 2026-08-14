# Workflow Engine 2 PluginStep registry and execution

SDAI 0.14 extends the existing PluginStep permission sandbox with a deterministic manifest registry. It does not turn manifests into executable code: YAML selects a trusted executor ID, while the implementation must still be registered explicitly in `PluginExecutorRegistry` by trusted application code.

## Layered discovery

`build_plugin_step_registry(...)` accepts explicit `PluginManifestSource` values in these authority layers:

1. `builtin`;
2. verified installed `pack`;
3. `org`;
4. `repo`;
5. `user`.

Source ordering does not affect the result. Each source is project-shaped and may contain `.sdai/plugin-steps`, `.sdai/extensions/plugin-steps`, `plugin-steps`, or `extensions/plugin-steps`. Manifests and source directories must be regular, non-symlink paths contained by the declared root.

Default repository discovery preserves both historical repository locations. Organization and user roots may be supplied as path-separator-delimited absolute directories through `SDAI_ORG_PLUGIN_STEP_ROOTS` and `SDAI_USER_PLUGIN_STEP_ROOTS`. Organization plugin IDs listed in `SDAI_ORG_PLUGIN_STEP_LOCKS` are authoritative. Only builtin and organization sources may lock an ID.

Installed Pack discovery reads `.sdai/packs/install-state.json`. A Pack plugin manifest is considered only when it is listed as a managed file beneath the exact installed Pack identity and its current bytes still match the recorded SHA-256. Dropping or modifying an untracked file under `.sdai/installed-packs` cannot create a trusted Pack registration.

## Resolution and conflicts

Plugin identity is `id@SemVer`. An unqualified ID selects the highest unambiguous semantic version. The v2 registry fails closed for:

- more than one definition of an exact ID/version in the same layer;
- different canonical manifests claiming the same exact ID/version across layers;
- ambiguous latest build variants;
- a higher-layer definition of a core/organization-locked ID;
- locks declared by Pack, repository, or user sources.

`sdai.plugin-step-resolution/v2` separates selected manifest data from ordered source provenance. Provenance includes layer, source label, portable manifest path, lock state, and canonical manifest SHA-256.

The built-in `evidence-summary@1.0.0` manifest is an extension-first example. It requests no filesystem, network, environment, command, or workspace-write permission and is core-locked. Its `evidence-summary` executor is intentionally not dynamically imported or auto-registered.

## Exact execution plans

`prepare_plugin_step(...)` emits `sdai.plugin-execution-plan/v2`. A plan binds:

- canonical plugin manifest SHA-256;
- selected executor ID and publisher;
- step ID;
- private input keys and SHA-256, never input values;
- effective permission intersection;
- ordered policy sources;
- canonical plan SHA-256.

Organization, repository, and user plugin policies are intersected. A lower layer may narrow an allowlist or permission but cannot add network, environment, filesystem, command, or workspace-write authority denied above it. Publisher trust and plugin allow/deny checks still apply after manifest resolution.

## Workflow Engine 2 adapter

`WorkflowPluginLeafExecutor` adapts a plugin graph leaf to the trusted PluginStep runtime. Resolved plugin inputs remain private on `WorkflowGraphResolution`; canonical graph JSON exposes only their keys and SHA-256.

Before a ledger task is identified or resumed, the adapter prepares the current plugin plan. The task context binds `leafPlanningBinding` to the plan SHA-256. If a manifest, executor ID, publisher, input, effective permission, or policy source changes, the task identity changes and prior completion cannot authorize the new execution. Immediately before dispatch, the adapter prepares the plan again and rejects a changed binding.

Plugin results normalize to Workflow Engine 2 succeeded/failed outcomes and are persisted by the existing evidence-bound execution ledger. Process loss propagates without committing an outcome, so resume reuses the dispatch identity and skips already completed plugin side effects. Plugin leaves can run inside sequence, condition, switch, foreach, bounded-loop, and serialized fan-out/parallel controls. A concurrency bound greater than one remains prohibited for any subtree that may write until SDAI defines an explicit governed write-permit strategy.
