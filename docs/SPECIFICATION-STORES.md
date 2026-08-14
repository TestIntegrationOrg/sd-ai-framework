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

Unknown or duplicate YAML fields, invalid semantic versions, absolute/traversal/Windows-unsafe paths, case-colliding roots, symlink redirection, missing roots, non-finite values, and oversized optional metadata fail closed.

`SpecificationStoreManifest.to_json()` is the canonical semantic representation. Its SHA-256 is independent of YAML formatting, mapping order, absolute machine paths, and source discovery order.

## Layered registry

`SpecificationStoreSource` registers one explicit local store root in one authority layer:

```text
core → organization → repository → user
```

Normal unlocked resolution chooses the highest SemVer precedence across all layers. An exact `id@version` may appear more than once only when its canonical manifest hash is identical; the highest layer is selected and every provenance record remains visible. SemVer build variants with equal precedence make unqualified latest selection ambiguous and require an exact version.

Core and organization sources may be authoritative locks. A lock applies to the complete store identifier, so repository or user definitions cannot route around it by choosing another version. Repository and user sources cannot declare locks.

Registry and resolution JSON expose only portable manifest-relative paths, layer/source labels, exact identities, canonical manifests, and hashes. Absolute discovery paths never enter canonical output.

## API surface

- `load_specification_store_manifest(root)` validates a store and its declared layout.
- `build_specification_store_registry(sources)` builds atomically in deterministic authority/source/root order.
- `SpecificationStoreRegistry.resolve(id, version)` supports exact or unqualified SemVer selection.
- `list_versions`, `list_resolved`, `list_all_exact`, and `search` are deterministic.

Read-only project references, content snapshots, lifecycle CLI commands, ownership routing, and multi-repository feature graphs are intentionally delivered by the dependent 0.15 slices. Those features consume this contract rather than creating parallel identity or provenance rules.
