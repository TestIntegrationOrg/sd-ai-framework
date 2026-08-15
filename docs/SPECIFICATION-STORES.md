# SpecificationStore manifest, references, and lifecycle

SDAI 0.15 introduces an extension-first contract for centralized specification stores. The store model separates canonical identity and content from local discovery and lifecycle operations: manifests and snapshots remain deterministic, while create/register/list/doctor/context provide explicit local administration without any Git or network mutation.

## Canonical manifest

A store is an explicit existing local directory with one manifest at `.sdai-store/store.yaml`:

```yaml
apiVersion: sdai.specification-store/v1
kind: SpecificationStore
metadata:
  id: platform-specs
  version: 1.0.0
  description: Shared platform specifications
spec:
  specificationRoots:
    current: specs/current
    changes: specs/changes
  capabilities:
    - changes
    - current-specifications
  metadata:
    owner: platform-architecture
```

The identifier and capabilities are portable lowercase identifiers. The version is strict SemVer. Descriptions and optional finite-JSON metadata are NFC-normalized UTF-8. Specification roots are unique, non-overlapping, project-relative POSIX paths and must resolve to existing directories below the store root without symlink components. Store metadata cannot itself be declared as specification content.

Unknown or duplicate YAML fields, manifests larger than 1 MiB, invalid semantic versions, absolute/traversal/Windows-unsafe paths, case-colliding roots, symlink redirection, missing roots, non-finite values, and oversized optional metadata fail closed. The manifest input is byte-bounded before UTF-8 decoding or YAML construction.

`SpecificationStoreManifest.to_json()` is the canonical semantic representation. Its SHA-256 is independent of YAML formatting, mapping order, absolute machine paths, and source discovery order.

## Layered registry

`SpecificationStoreSource` registers one explicit local store root in one authority layer:

```text
core → organization → repository → user
```

Normal unlocked resolution chooses the highest SemVer precedence across all layers. An exact `id@version` may appear more than once only when its canonical manifest hash is identical; the highest layer is selected and every provenance record remains visible. SemVer build variants with equal precedence make unqualified latest selection ambiguous and require an exact version.

Core and organization sources may be authoritative locks. A lock applies to the complete store identifier, so repository or user definitions cannot route around it by choosing another version. Repository and user sources cannot declare locks.

Registry and resolution JSON expose only portable manifest-relative paths, layer/source labels, exact identities, canonical manifests, and hashes. Absolute discovery paths never enter canonical output.

## Read-only project references

A project consumes already-present stores through `.sdai/specification-stores.yaml`:

```yaml
apiVersion: sdai.specification-store-references/v1
kind: SpecificationStoreReferences
references:
  - store: platform-specs
    version: 1.0.0
    path: ../platform-specs
```

Each reference selects one exact `store@version` at one explicit existing local directory. Paths may be absolute or project-relative because they are local discovery inputs; resolved machine paths never enter snapshot output. Declarations are strict, byte-bounded YAML and reject unknown or duplicate fields, aliases, invalid UTF-8, duplicate exact identities, and case-colliding declared paths.

Resolution validates the manifest identity and, when supplied, the registry's canonical manifest hash. It scans only the manifest-declared specification roots. Every regular file receives a portable store-relative path, root identifier, byte size, and streaming SHA-256 digest. Entries, reference resolutions, and their canonical JSON hashes are deterministic across declaration and filesystem traversal order.

Projects may pin observed content after an initial resolution:

```yaml
content:
  manifestSha256: sha256:<canonical-manifest-digest>
  snapshotSha256: sha256:<canonical-content-snapshot-digest>
```

A content binding fails closed if either digest is stale. Snapshot construction is bounded to 100,000 files, 100,000 directories, 16 MiB per file, and 256 MiB total content. It rejects missing roots, non-regular files, symlinks, junctions, reparse points, path collisions, and duplicate or overlapping resolved store directories. Manifest and content scans are repeated and compared so mutation during inspection cannot produce canonical truth.

`ResolvedSpecificationStoreReference.read_current()` and `.read_change()` verify the complete store snapshot before and after the read. Their results carry exact store identity, canonical manifest hash, complete snapshot hash, and the hashes and sizes of the content files that produced the current specification or change bundle.

Reference resolution is strictly read-only. It performs no network, Git, clone, fetch, pull, push, or store-content mutation operation.

## Lifecycle CLI

SDAI provides an explicit local lifecycle surface:

```text
sdai store create STORE_ID --version VERSION --destination PATH [--description TEXT] [--json]
sdai store register STORE_PATH [--path PROJECT] [--json]
sdai store list [--path PROJECT] [--json]
sdai store doctor [--path PROJECT] [--json]
sdai store context [--store STORE_ID] [--version VERSION] [--path PROJECT] [--json]
```

`store create` creates the canonical `current` and `changes` roots plus `.sdai-store/store.yaml`. A missing or empty explicit destination may be initialized. Repeating the exact create is idempotent. A non-empty unmanaged destination, invalid existing store, or managed store with different canonical content is rejected rather than overwritten.

`store register` validates an already-present local store and writes only the project's `.sdai/specification-stores.yaml` declaration. It never copies or changes referenced specification content. Repeating the same identity/path registration is idempotent; the same exact identity at a different path is rejected. External local paths may be recorded as discovery inputs, but list/context outputs expose only `pathScope=project|external` rather than absolute machine paths.

`store list` returns exact identity, version, manifest hash, content snapshot hash, ordinal, and path scope. `store context` adds declared capabilities, portable store-relative roots, and per-content-file root/path/hash/size provenance. `--json` emits one canonical UTF-8 JSON document with sorted keys and no human decoration.

`store doctor` validates the complete reference set and immutable snapshot boundary. Its automation exit classes are stable:

```text
0  success / healthy
1  invalid or unsafe lifecycle operation
2  doctor found an unhealthy reference set
```

A project with no registered stores is healthy with a warning so bootstrap scripts can call doctor before registration. Invalid, stale, unsafe, redirected, overlapping, or otherwise unresolvable references produce the unhealthy exit class without printing local store paths in canonical output.

Lifecycle operations are local-only. They do not clone, fetch, pull, push, execute store content, or overwrite unmanaged files.

## API surface

- `load_specification_store_manifest(root)` validates a store and its declared layout.
- `build_specification_store_registry(sources)` builds atomically in deterministic authority/source/root order.
- `SpecificationStoreRegistry.resolve(id, version)` supports exact or unqualified SemVer selection.
- `list_versions`, `list_resolved`, `list_all_exact`, and `search` are deterministic.
- `load_specification_store_references(project_root)` validates project declarations.
- `resolve_specification_store_references(project_root, registry)` resolves and snapshots referenced local stores.
- `ResolvedSpecificationStoreReference.verify_unchanged()` revalidates manifest and content bytes.
- `create_store(destination, id, version)` creates an owned local store conservatively and idempotently.
- `register_store(project_root, store_root)` adds one explicit local project reference without mutating store content.
- `list_stores`, `doctor_stores`, and `export_store_context` provide deterministic library equivalents of the CLI.

Ownership routing and multi-repository feature graphs are delivered by the dependent 0.15 slices. They consume these contracts rather than creating parallel identity or provenance rules.