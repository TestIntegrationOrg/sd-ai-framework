# Immutable Audit Export and Sink Handoff

SDAI 0.19 packages the verified feature audit ledger for immutable retention without making the remote destination authoritative.

```bash
sdai audit export FEATURE-123 --destination /retention/sdai --json
```

The command uses the offline local-filesystem reference sink. Cloud, WORM, SIEM, and enterprise retention systems should implement the `AuditExportSink` protocol as extensions; deterministic core does not import vendor SDKs or require network access.

## Source of truth

The immutable payload is the exact canonical JSONL returned by `AuditLedger.export_jsonl()`. The bounded `sdai audit --json` report is an inspection contract and is **not** used as the export payload.

Packaging verifies the source ledger, captures the canonical bytes, and verifies the ledger again. A changed event count, ledger head, or export SHA fails closed.

## `sdai.audit-export/v1`

The deterministic manifest contains only immutable identity/integrity metadata:

- export ID derived from feature ID + verified ledger head SHA + canonical JSONL export SHA
- feature ID and event count
- ledger head SHA-256
- complete canonical export SHA-256 and byte length
- fixed chunk size/count
- ordered chunk index, name, byte offset, length, and SHA-256
- manifest SHA-256

There are no timestamps, provider values, prompts, outputs, approval principals, credentials, or sink-specific fields in the manifest. The chunk bytes themselves remain the exact canonical audit truth and are never rewritten for presentation/privacy purposes.

A later ledger head always creates a new export ID; prior export packages remain immutable.

## Sink protocol

`AuditExportSink` accepts one validated `AuditExportPackage` and returns a validated `sdai.audit-export-receipt/v1` receipt. A receipt binds:

- sink ID
- export ID
- manifest SHA
- ordered chunk SHAs
- deterministic receipt ID
- delivery status (`accepted` or `already-present`)

The receipt proves only that the sink adapter accepted/recognized those immutable bytes. It is not workflow state, policy approval, promotion authority, or enterprise identity authorization.

## Local reference sink

`LocalFilesystemAuditSink` publishes to:

```text
<destination>/<audit-export-id>/
  manifest.json
  chunk-000000.bin
  chunk-000001.bin
  ...
```

Publication is staging-first. Existing exports are fully revalidated before `already-present` is returned, making replay idempotent. A mismatched existing manifest/chunk set fails closed rather than being overwritten.

Source audit bytes are not modified by successful, failed, or replayed handoffs. `handoff_audit_export()` rechecks the source ledger after the sink call and fails if the ledger changed during delivery; it never retries the sink automatically.

## Extension model

Core provides `AuditExportSinkRegistry` as a deterministic in-process registry. Extension implementations may perform network/cloud delivery subject to existing SDAI extension permission/secrets controls, but core package validation and source-ledger integrity remain provider-independent.

## 0.18 boundary

Identity-backed enterprise approvals remain deferred while 0.18 is on hold. Export receipts do not verify GitHub Enterprise/OIDC/SSO identities, sign approvals, authorize approvers, or satisfy distinct-approver policy.
