# Provider Execution Diagnostics (0.20)

SDAI records a privacy-safe lifecycle for governed provider attempts under the feature workspace at `.sdai/diagnostics/provider/<attempt-id>/`. These records are observability evidence; they do not replace workflow state, routing policy, audit provenance, or provider authority.

## Lifecycle

A normal attempt records canonical `sdai.provider-diagnostic/v1` events in this order:

1. `started` — persisted after the 0.19 agent audit start event and before provider construction;
2. `provider-ready` — persisted after provider construction, before `Provider.complete`;
3. `completed` or `failed` — persisted exactly once after the provider call/failure.

Startup, provider invocation, and total elapsed times use a monotonic nanosecond clock. UTC timestamps are informational event timestamps. The runtime accepts an injectable diagnostic clock and attempt-ID factory so tests can prove timing deterministically without wall-clock assumptions.

## First-output capability

The base provider contract advertises execution mechanics through `ProviderCapabilities`: streaming, heartbeat, cancellation, and first-output timing. Existing complete-only providers report all four as unsupported. Their diagnostics explicitly report first-output timing as unavailable with reason `provider-complete-interface`; SDAI does not invent a first-token/first-byte timestamp.

Streaming/heartbeat/cancellation behavior is added by later 0.20 slices and will extend this capability contract rather than create a second provider interface.

## Audit linkage

When 0.19 audit provenance is active, the diagnostic `started` event records the audit start-event SHA-256. The terminal agent audit event binds the persisted terminal diagnostic file by project-relative source path and SHA-256. This provides hash linkage in both directions while keeping the audit ledger authoritative for provenance and the diagnostic files authoritative for timing details.

## Failure and privacy boundaries

Diagnostic failure records contain only a bounded failure category and exception type. Raw exception messages are never persisted. System prompts, task prompts, selected context, provider output, credentials, and tokens are also absent. Routing decisions are represented only by a SHA-256 of the serialized routing document plus the already policy-approved profile/provider/model metadata.

Diagnostic start persistence fails closed before provider construction. If a terminal diagnostic or terminal audit write fails after the provider ran, the overall execution fails without retrying the provider. Observability failure must never cause duplicate AI/provider execution.

## Filesystem safety

Diagnostic paths are feature-scoped, bounded to the project root, and reject symlink components. Attempt IDs use portable identifier syntax. Event files are create-only canonical JSON and are never overwritten in place.

0.20 diagnostics do not implement or depend on 0.18 Identity-Backed Enterprise Approvals.
