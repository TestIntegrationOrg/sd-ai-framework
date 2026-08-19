# Unified SDAI Diagnostics (0.20)

`sdai diagnostics FEATURE [--json] [--run RUN] [--task TASK] [--path PROJECT]` is the read-only operator surface for understanding context selection, provider/model routing, provider execution timing/liveness, retry decisions, cancellation/failure state, and the relevant 0.19 audit identities from one command.

Diagnostics are observability evidence. They do not execute workflows/providers, retry work, select a replacement model, approve a change, or mutate canonical state.

## Stable contract

JSON output uses `sdai.diagnostics/v1` and contains:

- `featureId`, resolved feature workspace, selectors and deterministic `reportSha256`;
- `context` — current-repository-state context plan plus prompt/context size/hash metrics;
- `routing` — persisted routing decision/reason when available, otherwise an explicit hash-only/missing reason;
- `providerAttempts` — bounded verified provider lifecycle summaries;
- `retryExecutions` — bounded retry policy/decision/summary evidence;
- `audit` — lock-free verified audit chain identifiers and selected event identities;
- `partialReasons` — explicit reasons when evidence cannot fully explain a historical execution.

No section includes raw prompts, selected context text, provider output, streamed chunks, credentials, raw exception messages, or approval identity material.

## Context semantics

The context section is explicitly labeled `basis: current-repository-state`. It explains what SDAI would select now for the capability/profile/semantic agent associated with the latest relevant provider attempt, including file/skill reason codes and character/UTF-8 byte/SHA-256 metrics.

This is not falsely presented as a historical prompt snapshot. Historical prompt/context/output are hash-bound by 0.19 provenance; unified diagnostics does not reconstruct secret-bearing text from those hashes.

If the current profile/agent/configuration no longer resolves, the context section reports `available: false` with a bounded error type rather than fabricating context.

## Routing decision persistence

Starting with 0.20.8, `execute_routed_invocation` persists the deterministic privacy-safe routing document before provider execution under:

`.sdai/diagnostics/routing/<routing-decision-sha256>.json`

The document is immutable/idempotent by routing decision SHA-256. Persistence conflict/integrity failure occurs before the provider call. The same decision SHA is already bound into agent audit/provider diagnostics, allowing the CLI to explain the selected profile and deterministic selection reason.

Historical routed attempts may have only `routingDecisionDocumentSha256` in provider/audit evidence. In that case diagnostics reports:

- `available: false`;
- the verified decision hash;
- reason `historical-routing-document-not-persisted`.

It never recomputes a current route and labels it as the historical decision.

## Provider lifecycle

Provider attempt files are read from `.sdai/diagnostics/provider/<attempt-id>/` and must pass:

- canonical UTF-8 JSON byte verification;
- event SHA-256 verification;
- feature/attempt identity matching;
- contiguous unique sequence validation;
- supported lifecycle phases;
- at most one terminal event, which must be last.

The CLI summarizes starting/running/succeeded/failed/cancelled state, provider/profile/model, startup/invocation/total/first-output timing, heartbeat count/latest heartbeat, bounded failure category/type, routing decision hash, audit-start hash and provider capability metadata.

Raw provider output is never displayed.

## Retry lifecycle

Retry evidence under `.sdai/diagnostics/retry/<retry-id>/` is validated against the 0.20.5 policy/decision hashes before display. Diagnostics shows retry status, attempt count, policy SHA, each failed-attempt action/delay/reason/classification/diagnostic-attempt identity, and final bounded classification.

An execution with a policy but no summary is reported as in progress/partial rather than treated as completed.

## Audit verification without mutation

Unified diagnostics intentionally does **not** call the normal locking `AuditLedger` reader. It performs lock-free canonical JSONL verification directly against the 0.19 `AuditEvent` contract:

- strict UTF-8/canonical bytes;
- per-event contract/event SHA;
- contiguous sequence;
- feature identity;
- previous-event hash chain;
- bounded ledger/event sizes.

This means inspecting a feature with no audit history does not create `.sdai/audit`, `events.jsonl`, or `ledger.lock`. Existing audit bytes are never modified by diagnostics.

`--run` and `--task` filter audit events first. Provider attempts are correlated only from selected audit bindings that point at `.sdai/diagnostics/provider/<attempt-id>/...`. If the bounded selected audit result is truncated, the report explicitly marks correlation partial.

## Exit codes

- `0` — diagnostics/evidence available, including explicitly partial historical evidence;
- `2` — invalid input, unsafe path, corruption/integrity failure, or unsupported evidence contract;
- `3` — no selected execution/audit/retry data.

CLI error output exposes only a stable SDAI error code or exception type, not raw provider exception text.

## Read-only guarantee

Building either human or JSON diagnostics must leave the project file tree byte-for-byte unchanged. It does not construct a provider, call a model, sleep/retry, acquire/create an audit lock, or write routing/context/audit/diagnostic state.

The only new write introduced by 0.20.8 happens during **routed execution before provider invocation**, when the already-computed privacy-safe routing decision is persisted for later explanation. `sdai diagnostics` itself remains strictly read-only.

## 0.18 boundary

Unified diagnostics does not implement or depend on 0.18 Identity-Backed Enterprise Approvals. Local actor/approval strings remain provenance only; the diagnostics surface does not label them externally identity-verified or enterprise-authorized.
