# Agentic Reliability and Token Usage

This reference describes SD-AI's provider-neutral safeguards for agent-enabled workflows. The implementation does not inspect Codex-, Claude-, Gemini-, or Copilot-specific parent-process names. A provider host explicitly registers its current execution context through the `HostProviderBridge` contract.

## Reliable execution sequence

Before invoking agents, run a non-billing preflight:

```bash
sdai providers doctor --workflow agentic --feature-id FEATURE-123
# equivalent workflow-oriented command
sdai run FEATURE-123 --preflight-only
sdai run FEATURE-123 --preflight-only --json
```

Preflight resolves workflow steps, semantic agents, profiles, policy, executables, declared nesting support, and ordered route fallbacks without sending the feature prompt. A CLI executable whose authentication cannot be proven without execution is reported as `degraded` with reason `provider-auth-unverified`, not falsely reported as fully ready. `blocked` exits with code 2.

The recommended runbook is:

1. Create the feature and persist a deliberate workflow choice.
2. Run preflight and resolve every blocked result.
3. Run or explicitly resume the workflow.
4. If one step fails, inspect diagnostics and use `sdai step retry FEATURE STEP`.
5. Review per-attempt and aggregate usage with `sdai usage FEATURE`.

Normal `sdai run` is already resumable: persisted completed steps are skipped and the failed or paused boundary is reconsidered. `--resume` makes that intent explicit for engineer runbooks.

## Provider-neutral host reuse

`ProviderCapabilities.nested_execution` is `supported`, `unsupported`, or `unknown`. When the selected adapter declares `unsupported` or `unknown`, SD-AI may reuse the registered current host only when all of these checks pass:

- the host declares the requested capability and execution mode;
- the host profile exists, matches the declared provider, and passes effective policy;
- its provider-neutral invocation identity is not already in the combined invocation chain;
- its optional maximum nesting depth is not exhausted.

If no eligible host is registered, SD-AI keeps the selected provider decision; it does not guess a host from environment or process names. This makes the mechanism usable by multiple present and future providers without provider-specific nested-session detection.

Host integrations construct `AgentRuntime(..., host_bridge=bridge)` and return either a legacy string or a structured `ProviderResult`. Diagnostics and audit metadata record requested and effective provider/profile plus `hostReused: true` whenever the bridge is used.

## Timeouts and termination

Profiles support independent limits:

```yaml
profiles:
  codex:
    provider: codex
    timeout_seconds: 900
    startup_timeout_seconds: 10
    first_output_timeout_seconds: 60
    idle_output_timeout_seconds: 120
    termination_grace_seconds: 1
```

`startup_timeout_seconds` bounds observed local process creation, `first_output_timeout_seconds` detects a silent process, `idle_output_timeout_seconds` detects a stalled process after stdout begins, and `timeout_seconds` remains the hard total deadline. Metadata-only SD-AI heartbeat events do not reset stdout deadlines. On cancellation or timeout, SD-AI terminates the process group, waits the configured grace period, then kills it if necessary.

Failure evidence distinguishes no-first-output, idle-output, total timeout, cancellation, provider execution, policy, and availability failures. Prompt and provider output are not copied into progress or failure classifications.

## Ordered provider fallback

A route may be a single profile (backward compatible) or an ordered list:

```yaml
version: 1
routes:
  architecture: [claude, codex]
  coding:
    profiles: [codex, copilot]
```

SD-AI creates a fresh governed invocation for each candidate. It advances only for bounded transient categories such as timeout, rate limit, or provider unavailability. Authentication, policy, malformed output, audit, observability, cancellation, and unknown failures do not silently switch providers. Each attempt receives separate diagnostic and usage evidence.

An explicit workflow-step profile or `--profile` override remains pinned and does not use the route list.

## Token accounting

Provider adapters may return `ProviderResult` with:

- input, cached-input, output, reasoning, and total tokens;
- measurement source: `provider-reported`, `locally-counted`, `estimated`, or `unavailable`;
- a completeness flag and an unavailability reason;
- optional model, provider request ID, and finish reason.

Missing values remain JSON `null`; SD-AI never converts unknown billed usage to zero. The generic CLI adapter estimates input/output using UTF-8 byte length and labels it `estimated` and incomplete. A native adapter should return provider-reported counts when its API exposes them.

```bash
sdai usage FEATURE-123
sdai usage FEATURE-123 --workflow agentic --step implementation
sdai usage FEATURE-123 --attempt ATTEMPT_ID --json
```

The JSON report contains every terminal attempt, sums of known values, per-field coverage, and `actualTotalKnown`. Failed and retried calls remain visible so totals do not only describe the final successful call. Usage is also attached to provider diagnostics and agent audit terminal events.

## Evidence locations and limits

Provider attempts are stored under:

```text
specs/changes/<feature>/.sdai/diagnostics/provider/<attempt-id>/
```

Exact billing remains provider-authoritative. Some providers do not expose cache or reasoning tokens, and a process can fail before returning any usage. In those cases the report remains partial and says why. Preflight cannot prove remote authentication or quota unless an adapter supplies a safe readiness implementation; it reports that uncertainty instead of invoking a billable task.
