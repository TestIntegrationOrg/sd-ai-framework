# Feature repository ownership and routing

SDAI 0.15 defines a strict local repository map for multi-repository features. It assigns requirement, contract, component, and task identities to explicit existing local repositories. The map is declarative: SDAI never discovers a remote, searches neighboring directories for a better match, clones a repository, or widens ownership because of filesystem contents.

## Manifest

A project declares `.sdai/feature-repositories.yaml`:

```yaml
apiVersion: sdai.feature-repositories/v1
kind: FeatureRepositories
repositories:
  - id: api
    path: ../orders-api
    required: true
    capabilities:
      - requirements
      - contracts
      - components
      - tasks
    ownership:
      - type: requirement
        pattern: API-*
      - type: contract
        pattern: api:*
      - type: component
        pattern: api:*
      - type: task
        pattern: API-*

  - id: ui
    path: ../orders-ui
    capabilities:
      - requirements
      - components
      - tasks
    ownership:
      - type: requirement
        pattern: UI-*
      - type: component
        pattern: ui:*
      - type: task
        pattern: UI-*
```

The contract is byte-bounded to 1 MiB, UTF-8 only, and rejects YAML aliases, duplicate keys, unknown fields, duplicate repository identities, duplicate declared paths, and duplicate exact selectors across repositories. Repository identities are portable lowercase identifiers.

`path` is an explicit local discovery input. Required repositories must already exist and expose local Git worktree metadata. Optional repositories may be absent, but an entity that matches an unavailable optional repository still fails routing. Resolved absolute paths are not emitted in canonical resolution or routing JSON.

## Capabilities

Supported capabilities are:

```text
requirements
contracts
components
tasks
```

Every ownership selector must be backed by the matching capability. For example, a `contract` selector requires `contracts`. This keeps ownership and the repository's declared responsibilities consistent.

## Ownership selectors

Selectors are typed and case-sensitive:

```yaml
- type: task
  pattern: API-*
```

Supported entity types are `requirement`, `contract`, `component`, and `task`. Patterns use portable entity-id characters plus single `*` and `?` wildcards. `*` matches zero or more identity characters and `?` matches one. `**` is rejected so there is only one wildcard grammar; selectors are identity matchers, not filesystem globs.

Selectors are compiled to anchored deterministic regular expressions. Filesystem layout, Git remotes, branch names, and repository contents never participate in the match.

## Routing semantics

`route_feature_entities()` sorts its input canonically by type and identity. Each required entity must match ownership in exactly one repository:

- zero repository matches: fail closed for a required entity;
- more than one repository match: fail closed as ambiguous and report the matching repository/selector pairs;
- one repository match but the explicit repository is unavailable: fail closed;
- one repository match: emit a deterministic route decision;
- zero matches for an explicitly optional entity: retain it in `unmatchedOptional`.

Multiple selectors in the same repository may match an entity without changing repository ownership. SDAI selects the most specific matching selector deterministically for explainable provenance. Cross-repository overlap is always an error for an entity that exercises the overlap.

Every route decision records the entity, selected repository id/ordinal, selected ownership selector, canonical manifest hash, declaration source, and declaration source SHA-256. The route decision and aggregate result are themselves canonical SHA-256-bound JSON values.

## Resolution and safety

`resolve_feature_repositories()` resolves only the paths present in the manifest. It rejects symlinks, junctions, and reparse redirects in declaration or repository paths, required missing repositories, required non-directories, non-Git required targets, and duplicate resolved repository roots.

There is deliberately no fallback search. If `../orders-api` is missing while `../orders-api-copy` exists, SDAI reports the declared repository as missing rather than selecting the nearby directory.

## API

```python
manifest = load_feature_repository_manifest(project_root)
resolved = resolve_feature_repositories(project_root)
result = route_feature_entities(
    resolved,
    [
        RoutableEntity(FeatureEntityType.REQUIREMENT, "API-101"),
        RoutableEntity(FeatureEntityType.CONTRACT, "api:orders/v1"),
        RoutableEntity(FeatureEntityType.COMPONENT, "ui:checkout"),
        RoutableEntity(FeatureEntityType.TASK, "UI-204"),
    ],
)
```

The routing contract is deliberately independent of execution. Later 0.15 slices may consume these decisions to build multi-repository feature graphs and isolated execution plans, but this layer never checks out branches, invokes Git, or mutates a target repository.