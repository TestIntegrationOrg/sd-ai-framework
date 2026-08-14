# SDAI Pack Catalogs and Enterprise Trust Policy

SDAI 0.12 treats Pack discovery and Pack trust as deterministic framework decisions. A friendly catalog name, a valid Pack signature, or a provider assertion is never sufficient by itself.

This slice adds two framework contracts:

- `sdai.pack-catalog/v1` for deterministic Pack discovery metadata, and
- `sdai.pack-trust-policy/v1` plus resolved/decision contracts for enterprise trust evaluation.

## Catalog identity and integrity

A catalog has both a portable `id` and an exact `source` URI. **Trust policy is bound to the exact source URI, not the friendly ID.** This prevents a lower scope from creating a different catalog named `corp` and inheriting trust intended for the organization's real catalog source.

Catalog source identities are strict and intentionally conservative:

- URI scheme must be lowercase,
- HTTP/HTTPS/catalog authorities must be lowercase,
- embedded credentials are rejected,
- fragments are rejected,
- whitespace/backslashes are rejected, and
- HTTP/HTTPS/catalog sources require an authority.

A catalog file can additionally be loaded with both an expected source and expected canonical catalog SHA-256. Source or integrity mismatch fails closed.

## Catalog entries

Each catalog entry contains the complete canonical `sdai.pack-manifest/v1`, a stable package source URI, and the exact content SHA-256 defined by the Pack integrity layer.

This means discovery carries enough deterministic truth to feed the #135 resolver without asking a provider to reconstruct package metadata.

Within one catalog, duplicate or conflicting exact `publisher/id@version` entries are rejected. Across multiple catalogs, the resolver accepts duplicate exact identities only when manifest, source, and content truth are identical; disagreement fails closed.

## Scope resolution and provenance

Catalogs may be supplied at organization, repository, and user scopes. Resolution is additive and provenance-rich rather than last-writer-wins.

The same catalog ID cannot point at different sources across scopes. The same source cannot carry different catalog IDs or different catalog bytes across scopes. An identical catalog repeated at several scopes is de-duplicated while preserving ordered provenance:

```text
organization -> repository -> user
```

Search and info queries are deterministic. Resolver candidates are emitted from the resolved catalog set with exact source and content hashes.

## Enterprise trust policy

A Pack trust policy controls four monotonic dimensions:

```json
{
  "apiVersion": "sdai.pack-trust-policy/v1",
  "requireSignatures": true,
  "allowedCatalogs": ["catalog://corp"],
  "deniedCatalogs": [],
  "allowedPublishers": ["acme"],
  "deniedPublishers": []
}
```

`["*"]` means unrestricted for an allowlist. An empty allowlist means allow none. Deny lists use explicit identities.

Organization, repository, and user policy resolve monotonically:

- `requireSignatures` is ORed, so a lower scope cannot turn off a requirement established above it.
- catalog allowlists are intersected.
- publisher allowlists are intersected.
- catalog denylists are unioned.
- publisher denylists are unioned.

Therefore lower scopes can only preserve or further restrict enterprise trust; they cannot widen it.

## Signature validity is not the same as trust

#136 deliberately reports cryptographic verification with `trustStatus: not-evaluated`. #137 is where that proof is combined with catalog and publisher policy.

A trust decision checks that:

1. the Pack entry is actually present in the resolved catalog,
2. the exact catalog source is permitted and not denied,
3. the manifest publisher is permitted and not denied,
4. a signature is present when policy requires one,
5. any supplied signature report exactly matches the catalog entry's identity, publisher, manifest SHA-256, and content SHA-256, and
6. the signature report is current and cryptographically verified.

A supplied invalid/stale signature is never silently ignored just because signatures are optional. Once evidence is presented for the trust decision, invalid evidence blocks rather than becoming equivalent to unsigned state.

The resulting `sdai.pack-trust-decision/v1` records the exact Pack identity, publisher, catalog ID/source/hash, catalog-scope provenance, resolved policy hash, signature requirement/result, decision, and stable reasons.

## Key trust boundary

The signature verifier configuration from #136 is the cryptographic key trust anchor: it decides whether the referenced `keyId` verifies the exact signed payload. This policy slice does not infer publisher ownership from a key ID string.

The enterprise publisher allowlist is a separate authorization rule over the publisher already bound into the signed Pack payload and catalog manifest. Both conditions must be satisfied when signatures are required.

## Unavailable and malformed inputs

Catalog and policy loading are read-only. Missing files, symlinked files, invalid UTF-8/JSON, duplicate JSON keys, unknown fields, source mismatches, integrity mismatches, and unsafe source identities fail closed with Pack-specific errors.

Network retrieval is intentionally outside this contract. The caller may fetch a catalog through an approved transport, then pass the local bytes plus expected source/hash into the deterministic loader. This keeps catalog trust semantics independent from HTTP client behavior.
