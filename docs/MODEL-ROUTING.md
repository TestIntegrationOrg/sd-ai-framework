# Risk, Cost, Latency and Provider-Health Routing (0.20)

SDAI model routing is deterministic and provider-neutral. Optimization never grants provider authority: capability, effective policy, task risk/complexity, routing tier, context size, technology support, maximum cost class, and explicit availability remain hard eligibility constraints. Only candidates that pass those constraints participate in optimization ranking.

## Compatibility

The existing `sdai.model-routing/v1` contract remains the compatibility boundary. A historical `RoutingRequest` that does not supply optimization, fallback order, or provider-health inputs retains the historical request/candidate document shape, rank tuple, selection reason, and deterministic behavior.

New optimization fields are serialized only when explicitly used.

## Routing request extensions

`RoutingRequest` adds three optional inputs:

- `optimization`: `balanced` (default), `cost`, or `latency`;
- `fallback_profiles`: explicit deterministic profile order for non-explicit routing;
- `provider_health`: a bounded mapping of profile/provider/model names to `ProviderHealthSignal` values.

Explicit `requested_profile`, `requested_provider`, or `requested_model` retains the existing no-silent-fallback contract. If the explicit request is eligible it is selected; if policy/risk/health makes it ineligible, routing returns no selection rather than quietly switching vendors.

## Provider-health snapshot

`build_provider_health_snapshot(project_root, feature_id)` derives an explicit `sdai.provider-health/v1` snapshot from persisted 0.20 provider terminal diagnostics. It does **not** become policy or canonical truth and `route_model` does not read it automatically. A caller chooses whether to pass `snapshot.signals` into a routing request.

The helper:

- verifies every terminal diagnostic's canonical SHA-256 before using it;
- rejects unsafe/symlink paths and unsupported/malformed records;
- bounds total and per-profile history;
- ignores caller-cancelled attempts when judging provider success/failure health;
- computes successful p50 total latency only from successful attempts;
- reports `healthy`, `degraded`, `unavailable`, or `unknown` plus bounded counts and a source digest.

Two most-recent `provider-unavailable` failures mark a profile unavailable. Cancel-only history remains unknown. These rules are deterministic for the same persisted diagnostic set.

## Ranking

Hard eligibility is evaluated before ranking. A provider-health state of `unavailable` is an additional hard rejection; `degraded` remains eligible but ranks behind healthy/unknown alternatives.

When optimization extensions are active, ranking is stable and follows:

1. configured `fallback_profiles` order, when supplied;
2. provider-health state;
3. selected optimization preference;
4. existing routing priority, cost class, default-route preference, and stable provider/model/profile tie breakers.

For `cost`, lower cost class is preferred before latency. For `latency`, lower observed p50 latency is preferred before cost; missing latency ranks behind known latency after health. `balanced` keeps routing priority before cost/latency while still honoring explicit fallback and health.

This means a lower-cost or faster provider can never bypass organization/repository policy, unsupported critical/regulated risk, advanced-tier requirements, context limits, technology support, or maximum cost constraints.

## Fallback and escalation

`fallback_profiles` is deterministic ranking input, not a provider retry loop. 0.20.5 owns retry/backoff decisions; this routing layer owns provider/model selection. Callers may use a new routing decision after an explicit terminal escalation, but the retry controller does not silently change semantic agent or model authority.

## Privacy

Routing and health metadata contain profile/provider/model identifiers already allowed by policy, bounded health counts/states, latency integers, and SHA-256 source identities. They contain no prompt, selected context text, provider output, streamed chunks, credentials, raw exception messages, or approval identities.

0.20.6 does not implement or depend on 0.18 Identity-Backed Enterprise Approvals.
