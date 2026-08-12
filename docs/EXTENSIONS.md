# SDAI Extension Foundation

SDAI 0.6 introduces a versioned extension foundation so future skills, semantic agents, workflows, workflow components, validators, quality gates, integrations, and packs can be discovered and governed without adding hard-coded framework branches for every extension.

The foundation is intentionally incremental: existing canonical file formats remain valid while their runtime lookup is moved onto the common registry/provenance model.

## Compatibility boundary

The extension foundation is additive and backward compatible.

- Canonical semantic agents remain `.sdai/agents/*.agent.md` files.
- Canonical skills remain `.agents/skills/*/SKILL.md` with optional `sdai.yaml` sidecars.
- Legacy `.sdai/skills/<name>/skill.yaml` + `SKILL.md` fallback remains supported.
- When canonical and legacy skills share a name, the canonical `.agents/skills` definition still wins.
- Existing v0.5 skill names, including historically permitted uppercase names, continue to load through an internal compatibility adapter even though newly-authored external `sdai/v1` manifest IDs use a stricter lowercase grammar.
- Provider-native generated agent files remain derived artifacts. They are not registered as canonical semantic agents.
- Existing workflow/provider execution behavior is unchanged by the agent/skill registry migration.

The runtime agent and skill loaders now resolve their canonical source through `ExtensionRegistry`, then parse the existing source format with the same format-specific validation used before the migration. This separates **resolution/provenance** from **file-format semantics**.

## Manifest envelope

The first supported external manifest API version is `sdai/v1`.

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

Supported kinds are:

- `Skill`
- `Agent`
- `Workflow`
- `WorkflowComponent`
- `Validator`
- `QualityGate`
- `Integration`
- `Pack`

The external envelope is strict. Unknown top-level or metadata fields are rejected. `metadata.version` uses semantic version syntax, and new manifest IDs are deliberately filesystem-safe because later extension distribution will map IDs to managed paths.

Kind-specific `spec` schemas are introduced by the corresponding extension implementation tasks; the foundation currently guarantees that `spec` is a mapping.

## Public extension Python API

The 0.6-development extension surface is exposed from `sdai.extensions`:

```python
from sdai.extensions import (
    API_VERSION,
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

`ExtensionKey` is also public for registry/introspection tooling.

The agent/skill compatibility layer additionally exposes registry introspection from the existing modules:

```python
from sdai.agent_platform.definitions import (
    build_agent_definition_registry,
    explain_agent_definition,
)
from sdai.agent_platform.skills import (
    build_skill_registry,
    explain_skill,
)
```

The `explain_*` functions return `RegistryEntry`, including source path and layer provenance, without changing the canonical file format.

## Safe file loading

`load_extension_manifest(project_root, path)` reads UTF-8 text using YAML safe loading and requires the file to remain inside the supplied containment root, including symlink resolution.

Different extension layers may use different containment roots. For example, an organization extension root may live outside the application repository while repository extensions remain contained by the repository root.

Existing agent and skill format-specific loaders continue to use SDAI's established UTF-8 and path-containment helpers after registry resolution.

## Registry layers

Definitions resolve through these layers:

```text
builtin < pack < org < repo < user
```

Without a lock, the highest-precedence definition wins. Definitions in the same layer for the same `(kind, id)` are an error rather than an implicit last-write-wins rule.

Built-in and organization definitions may be marked `locked`. A lock prevents normally higher-precedence layers from replacing that definition. Pack, repository, and user definitions cannot declare themselves authoritative locks.

The current v0.5-compatible canonical agents and skills are registered at the repository layer. Organization/pack/user authoring and policy wiring are added by later extension tasks; the registry model is already prepared for those layers.

## Existing agent/skill compatibility adapters

The registry migration does **not** rewrite existing `.agent.md` or `SKILL.md` files into external manifests. Instead, SDAI creates an in-memory adapter `ExtensionManifest` whose `source` and `path` point to the original canonical file.

For agents, the compatibility registry entry uses:

```text
kind: Agent
format: sdai-agent-markdown
layer: repo
```

For skills:

```text
canonical: kind=Skill, format=agents-skill, layer=repo
legacy:    kind=Skill, format=sdai-legacy-skill, layer=repo
```

Only the winning source is registered when a canonical skill shadows a legacy skill of the same name. This preserves the prior fallback contract while avoiding an artificial same-layer duplicate.

## Deterministic registry construction

Use `ExtensionSource` and `build_extension_registry` when constructing a registry from external manifest files:

```python
from pathlib import Path

from sdai.extensions import ExtensionSource, RegistryLayer, build_extension_registry

registry = build_extension_registry(
    [
        ExtensionSource(
            root=Path("/opt/company/sdai-extensions"),
            path=Path("secure-coding.yaml"),
            layer=RegistryLayer.ORG,
            locked=True,
            label="company-engineering-policy",
        ),
        ExtensionSource(
            root=Path.cwd(),
            path=Path(".sdai/extensions/service-skill.yaml"),
            layer=RegistryLayer.REPO,
        ),
    ]
)
```

The builder sorts sources into deterministic layer order before registration. This is important for enterprise locks: authoritative definitions are installed before the repository/user layers they protect. A containment, validation, duplicate, or lock violation raises an exception; the builder does not return a partially constructed registry.

For one-at-a-time controlled registration, use `register_extension_source`.

## Provenance

Every `RegistryEntry` records:

- parsed/adapted manifest
- registry layer
- source label
- resolved canonical source path when available
- lock state

This provenance is the basis for CLI `explain`, policy, pack, audit, and traceability features.

## Error model

External manifest errors use `SDAI-EXT-*` codes. Registry errors use `SDAI-REG-*` codes. Existing agent/skill format errors retain their existing `AgentDefinitionError` and `SkillError` behavior so old projects do not receive a silent contract change during the registry migration.

## What comes next

The next 0.6 work adds extension scaffolding and validation CLI commands, then the engineering constitution/requirements quality layer, behavioral skill/agent evaluations, deterministic version synchronization, and final 0.6 compatibility validation.
