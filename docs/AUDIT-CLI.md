# Audit Query and Reporting

SDAI 0.19 provides a read-only audit inspection surface over the tamper-evident feature audit ledger.

```bash
sdai audit FEATURE-123
sdai audit FEATURE-123 --json
```

The command verifies the existing audit ledger before returning event data. It never creates audit history merely because a feature is queried, never executes a provider/workflow/plugin, and never changes workflow, policy, approval, trace, or promotion state.

## Filtering

Selectors use AND semantics and preserve ledger sequence order:

```bash
sdai audit FEATURE-123 \
  --category ai \
  --actor-kind ai \
  --action agent.execution.succeeded \
  --run RUN-123 \
  --workflow standard \
  --step implement \
  --task TASK-123 \
  --binding agent-invocation/output \
  --status succeeded
```

`--binding` matches an exact binding kind, source, or SHA-256 identity. An empty selection is a valid verified report.

## JSON contract

`--json` emits canonical `sdai.audit-report/v1` JSON with a deterministic `reportSha256`. The report contains:

- feature ID and canonical audit source
- verified event count, ledger head SHA-256, and canonical JSONL export SHA-256
- selected/returned counts and a truncation flag
- bounded event summaries: sequence, event ID/hash, timestamp, category, actor kind, action kind, status, safe execution IDs, and binding kind/source/hash
- audit-to-trace relationship counts and deterministic missing/stale/hash-mismatch gaps from the existing canonical trace authority

At most 500 event summaries are returned in one report. `selectedCount` still reports the total number of matching events.

The report intentionally does **not** expand actor subjects, action subjects/reasons, provider/model values, raw metadata, prompts, contexts, outputs, credentials, or approval principal/note content.

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Ledger verified and selected provenance has no audit linkage gaps. An empty filtered selection is valid. |
| `2` | Ledger verified, but existing trace provenance reports missing/stale/hash-mismatched audit-bound evidence or linkage is unavailable. |
| `3` | Feature exists but has no audit events yet. |
| `4` | Invalid selector/input or unsafe/ambiguous feature workspace. |
| `5` | Audit ledger integrity failure/tampering, or the ledger changed while a consistent report snapshot was being obtained. |

JSON errors use `sdai.audit-error/v1` and include a deterministic error SHA.

## Authority boundary

Audit reporting is inspection only. The append-only ledger remains the audit truth, the canonical trace graph remains the relationship authority, and typed evidence freshness remains owned by the existing trace freshness engine.

Existing local approval artifacts can be referenced by hash as provenance. They are **local assertions**, not verified enterprise identities. Identity-backed approvals (GitHub Enterprise/OIDC/SSO identity verification, signatures, authorization, distinct-approver enforcement) remain outside this 0.19 surface while 0.18 is held.
