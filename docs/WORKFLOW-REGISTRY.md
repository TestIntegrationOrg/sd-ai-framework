# Layered Workflow Registry

SDAI 0.14 introduces `sdai.workflow-registry/v2` and `sdai.workflow-registry-resolution/v2` for deterministic Workflow Engine 2 discovery and resolution.

## Sources and precedence

Each `WorkflowSource` is a project-shaped root containing the existing `.sdai/workflows/<name>.yaml` layout. Sources are assigned one of SDAI's established layers: `builtin < pack < org < repo < user`. Registry construction sorts sources by authority and stable source identity, so filesystem enumeration and caller order cannot change the result.

Repository workflows therefore keep their current location and format. Builtin, organization, Pack, and user workflow libraries use the same project-shaped layout rather than requiring provider-specific Python code.

## Two versions, two purposes

Existing top-level `version:` remains the integer Workflow Engine schema version (for example, version 9 for Workflow Engine 2 control flow).

An optional `registry_version:` is the semantic version of the workflow definition in the layered registry. Legacy workflow YAML that omits it receives registry identity `0.0.0`, preserving existing definitions without migration. A resolved identity is `<name>@<registry_version>`.

Unqualified resolution selects the highest SemVer precedence. Equal-precedence build variants such as `1.0.0+one` and `1.0.0+two` are intentionally ambiguous and require an exact reference.

## Immutability and locks

An exact `name@registry_version` may appear in multiple layers only when both its canonical source hash and its resolved canonical Workflow Engine 2 graph resolution are identical. This prevents byte-equivalent top-level YAML from silently meaning something different because source-local components or composition changed.

Builtin and organization sources may be authoritative locks. A locked definition blocks higher-precedence definitions of the same workflow name, including attempts to route around the lock with another semantic version. Repo/user/Pack sources cannot mark themselves locked.

## Provenance

Resolution reports source provenance separately from resolved graph provenance:

- `sourceSha256` binds canonical normalized source YAML data.
- `graphSha256` binds the canonical Workflow Engine 2 graph.
- `graphResolutionSha256` binds the full graph resolution contract, including composition/inheritance/overlay provenance visible to the graph resolver.
- `selectedProvenance` identifies the winning authority layer/source/path.
- `provenance` preserves every byte/graph-identical exact definition that contributed to the same identity.

Absolute source-root paths are not included in the canonical registry contract. Provenance paths remain project-relative POSIX paths, and Unicode source labels are normalized to NFC.

## APIs

`WorkflowRegistry.resolve()` accepts either a workflow name or exact `name@version`. `list()`, `search()`, and `info()` provide deterministic library APIs for later CLI/automation work. Registry construction is transactional per source: a conflicting or malformed source cannot partially mutate a usable registry.

## Safety and compatibility

Workflow discovery rejects symlink workflow directories/files, path traversal/non-portable provenance, malformed semantic versions, filename/name mismatch, and conflicting exact identities. Canonical graph resolution is performed with an empty process environment so organization/user environment variables on the machine cannot silently alter registry truth.

This registry is additive. Existing `load_workflow()` and `.sdai/workflows/<name>.yaml` behavior remains unchanged; later 0.14 overlay and execution slices consume the registry contract.
