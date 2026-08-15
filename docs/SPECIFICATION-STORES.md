# SpecificationStore manifest and registry

SDAI 0.15 introduces an extension-first contract for centralized specification stores. This first slice defines identity, layout, provenance, SemVer selection, and authority only. It does not clone, fetch, pull, push, register, or mutate a store.

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

This surface is strictly read-only. It performs no network, Git, clone, fetch, pull, push, registration, or store mutation operation.

## API surface

- `load_specification_store_manifest(root)` validates a store and its declared layout.
- `build_specification_store_registry(sources)` builds atomically in deterministic authority/source/root order.
- `SpecificationStoreRegistry.resolve(id, version)` supports exact or unqualified SemVer selection.
- `list_versions`, `list_resolved`, `list_all_exact`, and `search` are deterministic.
- `load_specification_store_references(project_root)` validates project declarations.
- `resolve_specification_store_references(project_root, registry)` resolves and snapshots referenced local stores.
- `ResolvedSpecificationStoreReference.verify_unchanged()` revalidates manifest and content bytes.

Lifecycle CLI commands, ownership routing, and multi-repository feature graphs are intentionally delivered by the dependent 0.15 slices. Those features consume these contracts rather than creating parallel identity or provenance rules.
