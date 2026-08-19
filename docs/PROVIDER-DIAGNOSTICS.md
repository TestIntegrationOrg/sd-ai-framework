# Provider Execution Diagnostics (0.20)

SDAI records a privacy-safe lifecycle for governed provider attempts under the feature workspace at `.sdai/diagnostics/provider/<attempt-id>/`. These records are observability evidence; they do not replace workflow state, routing policy, audit provenance, or provider authority.

## Lifecycle

Every attempt starts with canonical `sdai.provider-diagnostic/v1` events:

1. `started` — persisted after the 0.19 agent audit start event and before provider construction;
2. `provider-ready` — persisted after provider construction and capability discovery;
3. zero or more metadata-only progress events for observable providers;
4. exactly one terminal `completed`, `failed`, or `cancelled` event.

Providers using the historical complete-only interface continue to produce the original three-event lifecycle (`started`, `provider-ready`, terminal). Sequence numbers remain deterministic; progress-capable attempts extend the sequence rather than rewriting earlier records.

Startup, provider invocation, first-output, heartbeat, and total elapsed times use a monotonic nanosecond clock. UTC timestamps are informational event timestamps. The runtime accepts an injectable diagnostic clock and attempt-ID factory so tests can prove timing deterministically without wall-clock assumptions.

## Provider execution capabilities

`ProviderCapabilities` describes execution mechanics only: streaming, heartbeat, cancellation, and first-output timing. It never grants provider authority or changes semantic-agent/model policy.

Existing complete-only providers may report all capabilities unsupported. Their diagnostics explicitly report first-output timing as unavailable with reason `provider-complete-interface`; SDAI does not invent a token/byte timestamp.

Observable providers may implement `complete_observable(...)` and receive a caller-owned `ProviderCancellationToken` plus a metadata-only progress callback. Progress is represented by `ProviderProgressEvent` values whose kinds are limited to `first-output` and `heartbeat`. These events contain bounded reason codes, never provider output text.

### First output

A provider that advertises `first_output_timing` may emit one `first-output` signal. SDAI records elapsed nanoseconds from `provider-ready` to that signal and rejects duplicate/unsupported first-output signals. Streamed model content is not persisted in the diagnostic event.

### Heartbeat

A provider that advertises `heartbeat` may emit bounded `heartbeat` signals while it is running. Heartbeats indicate liveness only; they do not claim new model output, workflow progress, or task completion.

`CliProvider` uses a managed subprocess loop and advertises heartbeat/cancellation/first-output timing but not streaming. While a CLI process is alive it emits synthetic `subprocess-running` heartbeat metadata at a bounded interval. It may report `stdout-observed` once when output first becomes observable, but the output bytes themselves remain private to the provider result path.

## Cooperative cancellation

`AgentRuntime.execute_invocation(..., cancellation=...)` and `AgentRuntime.execute(..., cancellation=...)` accept a `ProviderCancellationToken`. A token cancelled before invocation prevents the provider call. Observable providers receive the same token for in-flight cancellation.

Cancellation terminates the attempt with a `cancelled` diagnostic event and stable failure category `cancelled`; timeout remains a separate `timeout` classification. Cancellation never triggers an automatic provider retry. The terminal 0.19 audit event records the execution as failed with the bounded exception type and binds the terminal diagnostic file when persistence succeeds.

Providers that do not implement the observable execution hook remain explicitly non-cancellable once their legacy `complete()` call has begun. SDAI does not claim cancellation support that a provider cannot honor.

For CLI providers SDAI starts the subprocess in its own process boundary. POSIX cancellation first terminates the process group and escalates to kill after a bounded grace period; Windows starts a new process group and uses terminate/kill escalation. The more extensive cross-platform subprocess/encoding hardening remains owned by 0.20.7 rather than being duplicated here.

## Audit linkage

When 0.19 audit provenance is active, the diagnostic `started` event records the audit start-event SHA-256. The terminal agent audit event binds the persisted terminal diagnostic file by project-relative source path and SHA-256. This provides hash linkage in both directions while keeping the audit ledger authoritative for provenance and the diagnostic files authoritative for timing/progress details.

## Failure and privacy boundaries

Diagnostic failure records contain only a bounded failure category and exception type. Raw exception messages are never persisted. System prompts, task prompts, selected context, provider output, streamed chunks, credentials, and tokens are also absent. Routing decisions are represented only by a SHA-256 of the serialized routing document plus the already policy-approved profile/provider/model metadata.

Diagnostic start persistence fails closed before provider construction. Progress persistence failure propagates through the observable provider boundary so a running subprocess/provider is cancelled/terminated rather than continuing without diagnostics. If a terminal diagnostic or terminal audit write fails after the provider ran, the overall execution fails without retrying the provider. Observability failure must never cause duplicate AI/provider execution.

## Filesystem safety

Diagnostic paths are feature-scoped, bounded to the project root, and reject symlink components. Attempt IDs use portable identifier syntax. Event files are create-only canonical JSON and are never overwritten in place.

0.20 diagnostics, heartbeat, and cancellation do not implement or depend on 0.18 Identity-Backed Enterprise Approvals.
