# SDAI Pack Manifest Contract

SDAI 0.12 introduces a distributable Pack contract that is separate from the legacy 0.6 extension `kind: Pack` manifests under `.sdai/extensions/packs/`.

The 0.12 contract is framework-owned and provider-neutral:

```text
apiVersion: sdai.pack-manifest/v1
id: secure-coding
publisher: acme
version: 1.2.3

description: Secure engineering methodology

capabilities:
  - skills
  - workflows

contentRoots:
  - skills
  - workflows

dependencies:
  - publisher: sdai
    id: core-quality
    version: ">=1.0.0,<2.0.0"

compatibility:
  framework: ">=0.5.4,<1.0.0"
  apis:
    - sdai.extension/v1
    - sdai.pack-manifest/v1
```

## Identity

A Pack coordinate is:

```text
publisher/id
```

An exact Pack identity is:

```text
publisher/id@version
```

`publisher`, `id`, and capability identifiers use lowercase portable identifiers. Publisher identity is explicit rather than inferred from a catalog URL or provider.

## Semantic versions

Pack and framework versions use strict SemVer 2.0.0:

```text
MAJOR.MINOR.PATCH[-PRERELEASE][+BUILD]
```

Abbreviated versions such as `1` or `1.2`, `v1.2.3`, and numeric identifiers with prohibited leading zeroes are rejected.

Prerelease ordering follows SemVer 2.0.0. Build metadata does **not** change version precedence. It remains part of an exact Pack identity and can be required by an exact `=` constraint.

## Version constraints

The v1 constraint grammar is deliberately small and deterministic. A constraint is either `*` or a comma-separated conjunction of comparators:

```text
=1.2.3
>=1.2.0,<2.0.0
>1.0.0,<=1.9.9
```

`==` is accepted and canonicalized to `=`. A bare full SemVer is canonicalized to exact `=`. The manifest serializer sorts comparator clauses into a stable representation.

This slice does not define dependency graph selection. The resolver and lockfile in #135 consume these constraints.

## Compatibility

Every manifest declares:

- `compatibility.framework`: the supported SDAI framework-version constraint.
- `compatibility.apis`: the versioned SDAI APIs the Pack expects.

The current framework package version and roadmap capability version are intentionally separate concepts. A Pack must constrain the package version it can actually execute against; roadmap label `0.12` is not substituted for the package version.

## Canonical truth and hashing

`PackManifest.to_json()` emits canonical UTF-8 JSON using sorted object keys, compact separators, and no ASCII escaping. Semantic set-like fields are canonicalized before serialization:

- capabilities are sorted,
- content roots are sorted,
- required APIs are sorted,
- dependencies are sorted by `publisher/id`, and
- version comparator clauses have stable ordering.

Human text and content-root strings are normalized to Unicode NFC. `PackManifest.sha256` is the SHA-256 of these canonical UTF-8 JSON bytes and therefore does not depend on YAML key order, list order for set-like fields, operating system, provider, or model.

Duplicate capabilities, roots, APIs, dependency coordinates, or comparator clauses are rejected rather than silently deduplicated.

## Path safety

`contentRoots` are Pack-relative POSIX paths. The v1 contract rejects:

- absolute paths,
- drive-qualified paths,
- backslashes,
- empty, `.` or `..` path segments,
- NULs,
- missing declared content-root directories, and
- symlink components.

`load_pack_manifest()` validates the manifest and declared roots against an actual Pack directory. The Pack root and manifest itself may not be symlinks. A symlink that could redirect declared content outside Pack ownership fails closed even when the resolved target exists.

These checks are intentionally stricter than generic repository path resolution because later signature/integrity work must hash an unambiguous Pack-owned byte set.

## Strict schema

`sdai.pack-manifest/v1` rejects unknown or missing fields at the manifest, dependency, and compatibility levels. There is no arbitrary provider metadata or extension bag in the signed canonical truth.

Signature evidence, publisher trust decisions, catalogs, lockfiles, lifecycle state, and eval certification are separate 0.12 contracts built in later slices. Keeping those concerns out of the manifest prevents a provider or installation environment from changing Pack truth by assertion.

## Legacy extension Packs

Existing files such as `.sdai/extensions/packs/sdai-java.yaml` continue to use `sdai.extension/v1` with `kind: Pack`. They remain compatibility inputs for the existing language/technology subsystem.

0.12 does not reinterpret those files as signed distributable Packs. Migration or packaging of legacy extension assets can be layered onto the new contract later without changing the meaning of existing manifests.
