# SDAI Extensions — Stable 1.0 Contract

SDAI extensions provide the extension-first foundation for skills, semantic agents, workflows, workflow components, artifact schemas, plugin steps, validators, quality gates, integrations, and packs. The foundation originated in 0.6 and is promoted to a stable compatibility surface for SDAI 1.0.

The stable contract separates **resolution/provenance** from each extension type's format-specific runtime semantics. Existing canonical agent, skill, and workflow formats remain valid; SDAI does not require them to be rewritten into external manifests.

## 1.0 compatibility promise

For the 1.x line:

- external extension manifests using `apiVersion: sdai/v1` remain supported;
- the public symbols exported by `sdai.extensions` at the 1.0 stability boundary remain import-compatible;
- existing `SDAI-EXT-*` and `SDAI-REG-*` error meanings remain compatibility-sensitive;
- the five registry layers, priorities, and authoritative lock rules remain stable;
- additive fields/kinds/APIs may require an explicitly versioned contract update, while a breaking envelope or resolution change requires a new contract version and migration policy;
- organization/built-in locks cannot be weakened by pack/repository/user extensions.

The machine-readable stability descriptor is available through:

```python
from sdai.extensions import extension_contract, extension_contract_json

contract = extension_contract()
print(contract.sha256)
print(extension_contract_json())
```

The descriptor uses `apiVersion: sdai.extension-contract/v1`, canonical JSON, and a deterministic `contractSha256`. It reports the supported manifest API version, extension kinds, manifest envelope shape/defaults, registry layers/priorities/lock authority, public Python symbols, and stable error-code families.

## Compatibility with existing projects

The 1.0 stability declaration is additive and preserves the established adapters:

- semantic agents remain `.sdai/agents/*.agent.md`;
- canonical skills remain `.agents/skills/*/SKILL.md` with optional `sdai.yaml` sidecars;
- legacy `.sdai/skills/<name>/skill.yaml` + `SKILL.md` fallback remains supported;
- canonical `.agents/skills` wins when canonical and legacy skills share a name;
- historically permitted legacy skill names continue through the compatibility adapter even though new external manifest IDs use the stricter portable lowercase grammar;
- provider-native generated agent files remain derived artifacts, not canonical semantic-agent definitions;
- existing workflow/provider execution semantics are not changed by the registry layer.

## External manifest envelope

The supported 1.0 external manifest API version is `sdai/v1`:

```yaml
apiVersion: sdai/v1
kind: Skill
metadata:
  id: java-security
  version: 1.0.0
  description: Java secure-coding expertise
spec:
  capabilities:
    - coding
    - security
```

The current stable extension kinds are exactly:

- `Skill`
- `Agent`
- `Workflow`
- `WorkflowComponent`
- `ArtifactSchema`
- `PluginStep`
- `Validator`
- `QualityGate`
- `Integration`
- `Pack`

The envelope allows only `apiVersion`, `kind`, `metadata`, and `spec`. `apiVersion`, `kind`, and `metadata` are required. `spec` is optional and defaults to an empty mapping.

Metadata allows only `id`, `version`, and `description`. `id` and `version` are required; `description` is optional and defaults to the empty string. New manifest IDs use the portable lowercase filesystem-safe grammar and versions use semantic-version syntax.

Unknown top-level fields, metadata fields, API versions, and kinds fail closed. Kind-specific `spec` validation belongs to the corresponding extension runtime; the common envelope guarantees that `spec` is a mapping.

## Public Python API

The stable package surface is exported from `sdai.extensions`. Existing pre-1.0 imports remain supported, including:

```python
from sdai.extensions import (
    API_VERSION,
    ExtensionKey,
    ExtensionKind,
    ExtensionManifest,
    ExtensionManifestError,
    ExtensionMetadata,
    ExtensionRegistry,
    ExtensionRegistryError,
    ExtensionSource,
    RegistryEntry,
    RegistryLayer,
    build_extension_registry,
    load_extension_manifest,
    parse_extension_manifest,
    parse_extension_manifest_text,
    register_extension_source,
)
```

The 1.0 stability/introspection additions are:

```python
from sdai.extensions import (
    EXTENSION_CONTRACT_API_VERSION,
    EXTENSION_STABILITY,
    ExtensionContract,
    ExtensionLayerContract,
    extension_contract,
    extension_contract_json,
)
```

`RegistryLayer.priority` exposes stable precedence and `RegistryLayer.lockable` exposes whether a layer may declare an authoritative lock.

## Registry layers and authority

Definitions resolve through the stable order:

```text
builtin(0) < pack(10) < org(20) < repo(30) < user(40)
```

When no lock applies, the highest-priority definition wins. Duplicate definitions for the same `(kind, id)` in one layer are an error rather than implicit last-write-wins behavior.

Only `builtin` and `org` are lockable authoritative layers. A locked definition prevents every normally higher-priority layer from overriding it. `pack`, `repo`, and `user` cannot declare authoritative locks. Lock/duplicate violations fail closed and registry construction never returns a partially constructed registry.

Use `ExtensionSource` plus `build_extension_registry` for deterministic multi-source construction. Sources are sorted by authority/precedence before registration, so authoritative locks are installed before the layers they protect.

## Safe loading and provenance

`load_extension_manifest(project_root, path)` uses UTF-8, YAML safe loading, and repository/root containment including symlink resolution. Different extension layers may use different containment roots, such as an organization-managed extension root outside an application repository.

Every `RegistryEntry` records:

- parsed/adapted manifest;
- registry layer;
- source label;
- resolved canonical path when available;
- lock state.

This provenance feeds explanation, policy, packs, audit, traceability, and diagnostics.

## Authoring and validation CLI

The installed CLI retains the established authoring shortcuts:

```bash
sdai create skill java-security
sdai create agent performance-engineer
sdai create workflow service-review
sdai create workflow-component architecture-review
sdai create validator java-layering
sdai create quality-gate mutation-tests
sdai create integration cursor
sdai create pack java-enterprise
```

These shortcuts cover the scaffold kinds currently exposed by `ScaffoldKind`. `ArtifactSchema` and `PluginStep` are valid stable registry/manifest kinds but do not imply a `sdai create` shorthand unless their authoring runtime exposes one.

Validate canonical or manifest-backed definitions with:

```bash
sdai extensions validate skill java-security
sdai extensions validate pack java-enterprise
sdai extensions validate validator .sdai/extensions/validators/java-layering.yaml
```

`sdai extension ...` remains a singular alias. Existing scaffold files are never replaced implicitly; replacement requires explicit `--force`.

## Error model

Manifest-envelope failures retain `SDAI-EXT-001` through `SDAI-EXT-011`. Registry failures retain `SDAI-REG-001` through `SDAI-REG-004`. Existing agent/skill format-specific errors continue to use their existing exception contracts rather than being silently rewritten into extension-envelope errors.

The machine descriptor lists the compatibility-sensitive error families so tooling can detect contract drift.

## Breaking changes and deprecation

A 1.x change must not silently remove a stable public symbol, manifest kind, supported `sdai/v1` envelope behavior, registry layer, or lock guarantee. A future breaking change must:

1. introduce a new versioned compatibility contract;
2. document the old/new behavior and deprecation window;
3. provide migration/upgrade guidance before removal;
4. preserve deterministic fail-closed policy authority during migration;
5. pass historical extension compatibility tests.

The broader 1.0 migration/rollback and JSON-contract inventory are separate stabilization slices; this document freezes only the extension/manifest boundary.

## Held 0.18 boundary

The extension contract does not implement or depend on held #25 Identity-Backed Enterprise Approvals. Registry `org` lock authority is deterministic configuration authority; it is **not** a claim that an organization identity, approver, signature, SSO/OIDC principal, or GitHub Enterprise actor has been externally verified.
