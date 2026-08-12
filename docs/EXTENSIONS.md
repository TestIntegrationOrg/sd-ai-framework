# SDAI Extension Foundation

SDAI 0.6 introduces a versioned extension foundation so future skills, semantic agents, workflows, workflow components, validators, quality gates, integrations, and packs can be discovered and governed without adding hard-coded framework branches for every extension.

This document describes the public Python foundation API added by issues #36–#38. CLI scaffolding, richer authoring examples, and migration of the existing agent/skill loaders are separate 0.6 tasks.

## Compatibility boundary

The 0.6 extension foundation is additive.

- Existing canonical semantic agents under `.sdai/agents/*.agent.md` continue to use the existing loader until the registry migration task.
- Existing canonical skills under `.agents/skills/*/SKILL.md` continue to use the existing loader until the registry migration task.
- Legacy `.sdai/skills` fallback behavior remains unchanged.
- Provider-native generated agent files remain derived artifacts and are not made canonical by this foundation.
- No existing workflow execution behavior is changed by issues #36–#38.

This separation lets the registry foundation stabilize before current runtime loaders are adapted to it.

## Manifest envelope

The first supported manifest API version is `sdai/v1`.

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

The envelope is strict. Unknown top-level or metadata fields are rejected. `metadata.version` uses semantic version syntax, and IDs are deliberately filesystem-safe because later extension distribution will map IDs to managed paths.

Kind-specific `spec` schemas are introduced by the corresponding extension implementation tasks; the foundation currently guarantees that `spec` is a mapping.

## Public Python API

The supported 0.6-development import surface is exposed from `sdai.extensions`:

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

## Safe file loading

`load_extension_manifest(project_root, path)` reads UTF-8 text using YAML safe loading and requires the file to remain inside the supplied containment root, including symlink resolution.

Different extension layers may use different containment roots. For example, an organization extension root may live outside the application repository while repository extensions remain contained by the repository root.

## Registry layers

Definitions resolve through these layers:

```text
builtin < pack < org < repo < user
```

Without a lock, the highest-precedence definition wins. Definitions in the same layer for the same `(kind, id)` are an error rather than an implicit last-write-wins rule.

Built-in and organization definitions may be marked `locked`. A lock prevents normally higher-precedence layers from replacing that definition. Pack, repository, and user definitions cannot declare themselves authoritative locks.

## Deterministic registry construction

Use `ExtensionSource` and `build_extension_registry` when constructing a registry from files:

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

- parsed manifest
- registry layer
- source label
- resolved manifest path when loaded from a file
- lock state

This provenance is the basis for later `sdai ... explain`, policy, pack, audit, and traceability features.

## Error model

Manifest errors use `SDAI-EXT-*` codes. Registry errors use `SDAI-REG-*` codes. Callers should surface the full actionable message rather than parsing human prose; stable structured error output is added with the CLI/API work.

## What comes next

The next 0.6 work adapts the existing agent and skill loaders to register canonical definitions through this foundation while preserving backward compatibility. Later tasks add CLI scaffolding/validation, organization lock policy wiring, behavioral evaluations, and version synchronization.
