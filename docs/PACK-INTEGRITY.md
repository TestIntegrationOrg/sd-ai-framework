# SDAI Pack Integrity and Signature Evidence

SDAI 0.12 separates three decisions that must not be conflated:

1. **Content integrity** — what exact declared Pack bytes exist now?
2. **Cryptographic signature validity** — does a configured verifier confirm the signature over the recorded payload?
3. **Enterprise trust** — is that publisher/key/catalog allowed by policy?

This slice implements the first two. Enterprise trust policy is intentionally deferred to #137. A cryptographically valid signature therefore reports `trustStatus: not-evaluated`; it is not automatically a trusted Pack.

## Canonical content index

`sdai.pack-content/v1` is computed read-only from every regular file below the manifest's declared `contentRoots`.

Each entry records:

- Pack-relative POSIX path,
- exact file size, and
- SHA-256 of the exact file bytes.

Entries are sorted by canonical path. Duplicate canonical paths and case-insensitive path collisions fail closed so the same Pack cannot mean different things on Linux and Windows.

The content index intentionally does not include the manifest itself. The signed payload binds the manifest hash separately, so both manifest truth and declared content truth are explicit and reconstructible.

Adding, deleting, or changing any regular file under a declared content root changes the content SHA-256. Empty directories are not content truth.

## Path safety

Integrity walking is stricter than ordinary file discovery. SDAI rejects:

- absolute, drive-qualified, backslash, `.`/`..`, or NUL content paths,
- symlinked content files,
- symlinked content directories,
- declared roots that resolve through aliases, and
- resolved paths that escape the Pack root or traverse a symlink/reparse-point style alias.

The walker does not follow links. These rules keep the signed byte set Pack-owned and portable across supported operating systems.

## Signed payload

`sdai.pack-signature-payload/v1` contains only canonical Pack truth:

```json
{
  "apiVersion": "sdai.pack-signature-payload/v1",
  "packIdentity": "publisher/id@1.2.3",
  "publisher": "publisher",
  "manifestSha256": "sha256:...",
  "contentSha256": "sha256:..."
}
```

`packIdentity` must be canonical `publisher/id@SemVer`, and its publisher must exactly match the separate `publisher` field. The signature input is the canonical UTF-8 JSON bytes of this payload. `payloadSha256` records the SHA-256 of those exact bytes.

## Signature evidence

`sdai.pack-signature/v1` stores:

- every signed payload field,
- `payloadSha256`,
- portable signature algorithm identifier,
- verifier-specific `keyId`, and
- canonical Base64 signature bytes.

Unknown fields, duplicate JSON names, malformed hashes/identity/Base64, and a payload hash inconsistent with the stored fields fail closed.

The evidence itself has a stable SHA-256 over canonical JSON for provenance/reference use.

## Pluggable verification

Core SDAI does not hard-code a cryptographic library or vendor in this contract. A verifier implements:

```python
verify(*, key_id: str, payload: bytes, signature: bytes) -> bool
```

The framework selects a verifier by the evidence algorithm and supplies the **exact reconstructed signed payload bytes**. Missing algorithms, verifier exceptions, and false verification all produce explicit non-valid states; none degrade into an unsigned pass.

Concrete cryptographic backends can be added without changing the canonical Pack contracts.

## Current-proof verification

`verify_pack_signature()` recomputes the current manifest and declared-content hashes and reports separately:

- `integrityStatus`: current/stale,
- `publisherBound`: whether evidence publisher matches the current manifest publisher,
- `signatureStatus`: valid/invalid/unsupported/error,
- `verified`: whether the evidence is a valid proof for the **current** Pack truth, and
- `trustStatus: not-evaluated`.

A signature may remain cryptographically valid for its old payload while current Pack files have changed. In that case `signatureStatus` can be `valid`, but `integrityStatus` is `stale` and `verified` is false. This distinction prevents a historically valid signature from satisfying current proof.

Publisher/catalog allowlists and whether a valid key is trusted for a given publisher are policy decisions in #137.

## Evidence location

Signature evidence should normally be stored outside the manifest's declared `contentRoots`. Otherwise adding the signature file would itself change the content index and create a self-referential signing problem.

Loading an evidence file is read-only and rejects a symlinked evidence target.
