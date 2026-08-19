# Provider Retry Policy (0.20)

SDAI 0.20.5 adds deterministic, privacy-safe retry decisions around the existing single provider-attempt execution boundary. Retry is orchestration evidence and policy; it does not replace provider routing, workflow state, audit provenance, or provider authority.

## Single-attempt authority

`AgentRuntime.execute_invocation(...)` remains one governed provider attempt. Every call produces its normal 0.19 audit start/terminal provenance and 0.20 provider diagnostics. `execute_with_retry(...)` never hides a retry loop inside that attempt. Instead, when policy allows another attempt, it invokes the same governed boundary again with a new diagnostic attempt identity.

A retry execution uses a stable retry ID and attempt IDs such as `<retry-id>-a001`, `<retry-id>-a002`, and so on. Retry decisions point to those diagnostic IDs; provider diagnostics are already hash-linked into terminal audit provenance. This preserves a reconstructable chain from retry decision to provider attempt to audit evidence.

## Default compatibility

`RetryPolicy()` defaults to `max_attempts=1`. Existing callers therefore retain one-attempt behavior unless they explicitly opt into retries. Retry policy is bounded to at most ten attempts and uses deterministic exponential delay with optional deterministic jitter.

## Failure taxonomy

`classify_provider_failure(...)` emits only bounded category/reason/exception-type metadata. Raw exception messages are never persisted in retry evidence.

Core categories are:

- `cancelled` — caller/provider cancellation; never retry;
- `timeout` — retryable for advisory execution when enabled;
- `rate-limit` — HTTP/provider throttling such as 429; retryable when enabled;
- `authentication` — authentication/authorization/permission failures; never retry;
- `provider-unavailable` — transient connection/service statuses may retry, while a missing executable fails immediately;
- `malformed-output` — invalid UTF-8, empty/malformed/parse/decode output; never retry by default;
- `local-subprocess` — generic non-zero/local process failure; fail fast because core cannot prove it is transient;
- `policy` — deterministic policy rejection; never retry;
- `observability` — provider diagnostic persistence/integrity failure; never retry;
- `audit` — audit provenance/persistence failure; never retry;
- `unknown` — fail closed.

The classifier may inspect a provider exception message transiently to recognize transport/status markers, but that message is not included in `FailureClassification`, `RetryDecision`, policy files, or summary files.

## Retry decision rules

A failure may retry only when all of the following are true:

1. another attempt remains under `max_attempts`;
2. execution mode is `advisory`;
3. the failure classification itself is retryable;
4. the category is enabled by the effective retry policy;
5. the retry decision is successfully persisted before any backoff or next provider call.

Workspace-write retries fail closed with reason `workspace-write-side-effect-ambiguity`. A provider or tool may already have modified the workspace before a transport failure, so SDAI does not assume idempotency. A later execution-identity capability could relax this only with proof that replay is safe.

Audit, diagnostic, policy, cancellation, and retry-evidence persistence failures are never reasons to execute the provider again. In particular, an observability write failure after a provider returned cannot be “repaired” by another model invocation.

## Backoff and deterministic jitter

The delay for failed attempt `n` is bounded exponential backoff:

`min(maxDelayMs, baseDelayMs * multiplier^(n-1))`

Optional jitter is deterministic, not random at execution time. SDAI derives a stable SHA-256 seed from the feature, capability, mode, profile/provider/model, and routing-decision hash, then combines it with the attempt number and failure category. Equivalent inputs produce the same delay.

## Evidence

When the feature workspace exists, retry evidence is stored under:

`.sdai/diagnostics/retry/<retry-id>/`

The directory contains:

- `000-policy.json` — the exact versioned retry policy and policy SHA-256;
- `<attempt>-decision.json` — one canonical decision after each failed governed attempt;
- `summary.json` — final status, number of attempts, policy SHA-256, and bounded final classification.

Files are create-only canonical JSON, project-bounded, and symlink-safe. A decision must persist before SDAI sleeps or starts another provider attempt. Failure to persist retry evidence therefore fails closed without a second call.

## Escalation boundary

`execute_with_retry` accepts an optional escalation observer invoked only after a terminal `fail` decision. The observer receives the immutable `RetryDecision`; it cannot return a replacement provider result or silently select another model/profile. Provider/model fallback and cost/risk routing remain owned by the routing layer and 0.20.6.

## Cancellation

A caller-owned `ProviderCancellationToken` is passed unchanged to every governed attempt. A cancelled attempt is terminal for the retry controller and is never backed off/retried.

## Security and 0.18 boundary

Retry evidence contains no prompts, context, provider output, credentials, streamed chunks, raw exception messages, or approval identities. 0.20.5 does not implement or depend on 0.18 Identity-Backed Enterprise Approvals.
