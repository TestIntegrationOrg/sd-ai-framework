# SDAI 1.0 Extension Authoring Guide

This guide is the practical path for extending SDAI without modifying its core
Python. The normative compatibility boundary is [SDAI Extensions — Stable 1.0
Contract](EXTENSIONS.md); kind-specific documents own their runtime semantics.

## Choose an extension kind

| Kind | Stable role and current runtime path | Canonical guidance |
|---|---|---|
| `Skill` | Composable expertise; canonical skill files are runtime-consumed | [Skills](SKILLS.md) |
| `Agent` | Provider-neutral semantic responsibility; canonical agent files are runtime-consumed | [Agent files](AGENT-FILES.md) |
| `Workflow` | Lifecycle orchestration; canonical workflow files are runtime-consumed | [Workflows](WORKFLOWS.md) |
| `WorkflowComponent` | Reusable workflow fragments; the shared `sdai/v1` component manifest is runtime-consumed | [Workflow components](WORKFLOW-COMPONENTS.md) |
| `ArtifactSchema` | Deterministic artifact validation contract; shared `sdai/v1` schema manifests are runtime-consumed | [Artifact schemas](ARTIFACT-SCHEMAS.md) |
| `PluginStep` | Permission-declared custom behavior; runtime uses the separately versioned trusted PluginStep manifest/executor contract | [Plugin step registry](PLUGIN-STEP-REGISTRY-V2.md) |
| `Validator` | Registry/provenance envelope only in 1.0; no built-in executor consumes this manifest kind | [Extension contract](EXTENSIONS.md) |
| `QualityGate` | Registry/provenance envelope only; executable gates are configured in `.sdai/quality-gates.yaml` | [Quality gates](ENTERPRISE.md#quality-gates) |
| `Integration` | The shared scaffold is registry/provenance metadata; runtime discovery/execution uses `sdai.integration-manifest/v1` | [Integration manifest](INTEGRATION-MANIFEST.md) |
| `Pack` | The shared scaffold is the legacy registry envelope; signed installable packs use `sdai.pack-manifest/v1` | [Pack manifest](PACK-MANIFEST.md) |

Use an existing semantic kind before proposing new core runtime behavior. A
provider adapter is a separate Python plugin boundary: implement `Provider` and
publish it through the `sdai.providers` entry-point group described in
[Provider adapters](PROVIDERS.md).

## Understand the two authoring forms

SDAI does not force established canonical assets into a second format:

- semantic agents remain `.sdai/agents/*.agent.md`;
- canonical skills remain `.agents/skills/<name>/SKILL.md` with an optional
  `sdai.yaml` sidecar;
- workflows remain under `.sdai/workflows/` in their existing workflow format.

The external registry envelope registers extension identity and provenance. Its
stable version is `apiVersion: sdai/v1`:

```yaml
apiVersion: sdai/v1
kind: WorkflowComponent
metadata:
  id: architecture-review
  version: 1.0.0
  description: Reusable architecture review step
spec:
  inputs: {}
  requires: []
  steps:
    - id: validate-architecture
      type: validate
```

The common envelope accepts only `apiVersion`, `kind`, `metadata`, and `spec`.
Kind-specific documents define the allowed `spec`; the envelope alone does not
grant execution authority or promise that a runtime consumes every registered
kind. In particular, `Validator` and `QualityGate` scaffolds are registry-only
in 1.0. Configure executable quality gates in `.sdai/quality-gates.yaml`.

## Scaffold supported kinds

From an initialized repository, use `sdai create` for the supported authoring
shortcuts:

```bash
sdai create skill java-security
sdai create agent performance-engineer
sdai create workflow service-review
sdai create workflow-component architecture-review
sdai create validator java-layering
sdai create quality-gate mutation-tests
sdai create integration custom-cli
sdai create pack java-enterprise
```

Existing files are preserved unless `--force` is supplied explicitly.
`ArtifactSchema` and `PluginStep` are stable registry kinds but currently have
no `sdai create` shortcut. Follow their kind-specific guidance and validated
manifest locations instead of inventing a scaffold command. The generic
`validator`, `quality-gate`, `integration`, and `pack` shortcuts create the
legacy/shared registry envelope; consult the table above before assuming that
the generated envelope is an executable or installable runtime definition.

## Validate before execution

Validate a scaffolded name or exact manifest path:

```bash
sdai extensions validate skill java-security
sdai extensions validate pack java-enterprise
sdai extensions validate validator .sdai/extensions/validators/java-layering.yaml
```

`sdai extension` is retained as a singular compatibility alias. Validation is
not authorization: loading, policy, permissions, locks, and runtime-specific
checks still apply when the extension is resolved or executed.

For reusable Python tooling, inspect the stable machine-readable contract:

```python
from sdai.extensions import extension_contract, extension_contract_json

contract = extension_contract()
print(contract.sha256)
print(extension_contract_json())
```

The stable import surface and compatibility-sensitive error families are
enumerated in `docs/EXTENSIONS.md` and in the returned
`sdai.extension-contract/v1` descriptor.

## Resolution, locks, and provenance

Registry definitions resolve in this stable order:

```text
builtin(0) < pack(10) < org(20) < repo(30) < user(40)
```

Without a lock, the highest-priority definition wins. Duplicate definitions
for the same kind and ID within one layer fail closed.

Only `builtin` and `org` may declare authoritative locks. A lock prevents all
normally higher-priority layers from overriding that definition. `pack`,
`repo`, and `user` cannot declare or weaken authoritative locks. Every resolved
entry retains its layer, source label, canonical path when available, and lock
state as provenance.

Use `load_extension_manifest(project_root, path)` or the higher-level registry
APIs instead of directly opening untrusted paths. Stable loading enforces UTF-8,
safe YAML, root containment, and resolved-symlink containment.

## Test an extension

An extension contribution should prove:

1. valid creation or parsing and stable metadata;
2. successful kind-specific validation;
3. deterministic resolution from the intended registry layer;
4. duplicate, malformed, path-escape, and unauthorized-override failures;
5. the actual runtime journey, such as skill selection, workflow resolution,
   integration projection, plugin permission enforcement, or pack install;
6. compatibility with existing projects and historical formats it affects.

Repository contributors can run focused extension tests while iterating:

```bash
python -m pytest -q tests/test_extension_manifests_v06.py
python -m pytest -q tests/test_extension_registry_v06.py
python -m pytest -q tests/test_extension_scaffolding_v06.py
```

Before submission, run the full and installed-wheel gates from
`CONTRIBUTING.md`.

## Package and distribute safely

- Repository-local assets belong in their documented `.sdai/` or
  `.agents/skills/` locations.
- Organization policy/assets should use the organization registry layer and
  locks where authority is required.
- Related reusable assets should ship as a Pack with explicit dependencies,
  integrity, trust, lockfile, and evaluation policy.
- Declarative external tools should use Integration manifests. The packaged
  examples in `src/sdai/builtin_integrations/` and
  `docs/examples/integrations/custom-cli.integration.yaml` are canonical
  references.
- Custom executable workflow behavior must use PluginStep permission contracts;
  never hide shell interpolation or undeclared access inside another kind.

Do not copy provider credentials, organization policy, approval evidence, or
machine-local paths into distributable assets.

## Compatibility and deprecation

For SDAI 1.x, do not silently remove a public `sdai.extensions` symbol,
manifest kind, `sdai/v1` envelope behavior, registry layer, lock guarantee, or
compatibility-sensitive error meaning. Additive fields or kinds require an
explicit contract update and regression coverage.

A breaking change requires a new versioned contract, old/new behavior notes,
a deprecation window, migration guidance, and preserved fail-closed authority.
Historical compatibility tests remain enabled. Package version, manifest
version, pack version, workflow/schema version, and JSON `apiVersion` are
independent contracts and must not be mechanically synchronized.

## Troubleshooting

| Symptom | Check |
|---|---|
| Manifest rejected | Confirm exact `sdai/v1` envelope fields, portable lowercase ID, semantic version, and mapping-valued `spec` |
| Duplicate error | Remove the duplicate `(kind, id)` in the same registry layer; SDAI does not use last-write-wins |
| Override rejected | Inspect built-in/organization locks and effective policy; lower authority cannot bypass them |
| Path rejected | Keep the manifest inside its declared layer root and inspect symlink targets |
| Kind validates but will not run | Run the kind-specific validator and inspect required permissions, dependencies, and policy |
| Provider is unavailable | Use `sdai agents doctor`; provider availability is separate from extension resolution |

Do not respond to a policy/lock/path failure by weakening the guard. Diagnose
the owning authority, source, and compatibility contract.

## Security and held scope

Extension authors must treat manifests, instructions, tool output, and remote
content as untrusted data. Request minimal permissions, keep commands as
executable-plus-argument arrays, and keep organization policy monotonic.

The 0.18/#25 identity-backed approvals remain held and are outside SDAI 1.0.
Organization registry authority and local actor/approval evidence do not prove
GitHub Enterprise, OIDC, SSO, cryptographic approver identity, identity-backed
authorization, or distinct-approver enforcement.
