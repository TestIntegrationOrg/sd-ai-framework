# Context Planning and Explanation (0.20)

SDAI 0.20 plans agent context before provider execution. The planner is deterministic and provider-independent: it chooses relevant lifecycle artifacts, governance, skills, and trace-linked source files, then records only metadata about those choices in the versioned `sdai.context-plan/v1` contract.

## Explain context before execution

Use the read-only command:

```text
sdai context explain FEATURE --capability coding
sdai context explain FEATURE --capability review --agent reviewer --json
```

Optional flags select an existing profile/semantic agent or execution mode. `--path` selects the project root. Explanation never creates or calls a provider.

The JSON report uses `sdai.context-explain/v1`. It includes:

- current or legacy feature workspace identity;
- selected artifact/source paths, SHA-256 identities, sizes, truncation state, and deterministic reason codes;
- excluded context and exclusion reason codes;
- selected/excluded skills and the profile/semantic-agent/policy reasons that nominated them;
- prompt/context character counts, UTF-8 byte counts, and SHA-256 identities;
- profile/provider/semantic-agent routing metadata already resolved by SDAI policy;
- deterministic plan and report hashes.

Raw artifact text, governance text, skill instructions, system prompts, and task prompts are intentionally absent from the explanation output.

## Size metrics

SDAI reports deterministic metrics for:

- `featureContext`
- `governanceContext`
- `skillsContext`
- `systemPrompt`
- `taskPrompt`
- `combinedPrompt`

Character counts use Python Unicode code points. Byte counts use the exact UTF-8 encoding that SDAI uses for deterministic hashing. These two measurements are core deterministic metrics and do not depend on a model vendor.

Token counts are not canonical because model tokenizers differ. The Python explanation API accepts an optional deterministic token-estimator callable. Without one, the report states `provider-tokenizer-not-configured`; SDAI never guesses a token count.

## Stale-plan protection

A `ContextPlan` is hash-bound to selected artifact bytes and effective skill metadata/instructions. `AgentRuntime.build_invocation_from_context_plan(...)` re-resolves the current canonical plan before prompt composition. If files, policy-driven selection, skills, or planning budgets changed after explanation, composition fails closed instead of silently using the stale plan.

This makes an explanation an accurate preview of the context that can be composed from that same repository state, without turning the explanation into execution authority.

## Context authority and budgets

Governance/constitution files are execution authority and are placed ahead of feature relevance files inside the bounded context-file budget. Feature context is capability-filtered and, for current `specs/changes/<FEATURE>` workspaces, enriched using existing cross-artifact trace facts. Repository source is included only when it explicitly references a relevant trace entity.

Legacy `specs/<FEATURE>` workspaces retain deterministic lifecycle context behavior and report the `legacy-workspace-trace-fallback` diagnostic when current-spec cross-artifact indexing is unavailable.

Explicit isolated-task context remains a separate contract: `build_explicit_context_invocation(...)` does not scan normal feature artifacts or build a context plan. It still applies project governance and capability-applicable skills.

## Privacy boundary

Planning/explanation is metadata-oriented. Plan/report contracts may expose portable repository-relative paths, non-secret profile/provider identifiers, SHA-256 identities, sizes, reason codes, and truncation/budget state. They must not expose raw prompts, context, provider output, credentials, tokens, or secret-bearing artifact contents.

0.20 context planning does not implement or depend on 0.18 Identity-Backed Enterprise Approvals. Local approval data remains local provenance/assertion only.
