# Integration Ecosystem Catalog

SDAI 0.13 ships a framework-owned Integration catalog under `sdai/builtin_integrations`. The catalog is intentionally declarative: tool growth changes manifests and documentation, not provider-routing branches in the Integration registry, materializer, or execution engine.

This support snapshot was verified against current vendor documentation on 2026-08-14. Native paths change over time, so each manifest is versioned and remains subject to the Integration lifecycle's status/repair/upgrade checks.

## Canonical source model

SDAI's shared project skill source remains:

```text
.agents/skills/<skill>/SKILL.md
```

When a harness has a distinct project-native skill directory, its builtin Integration mirrors the canonical corpus into that directory. The projection is byte-preserving and managed by the Integration install-state contract; unmanaged or user-modified native files are never silently replaced.

Tools that already consume `.agents/skills` natively do not need a redundant second copy. `generic-agents` exists for projects that want SDAI to *create* the universal layout from a neutral managed source:

```text
.sdai/integration-assets/agents/skills  ->  .agents/skills
```

The source and target deliberately differ because v1 projections are real managed copies, not aliases or no-op declarations.

## Shipped base Integrations

| Integration | Native project surface | What SDAI ships | Current level |
|---|---|---|---|
| `codex` | `.codex/skills` | Skills + audited advisory CLI execution | Native |
| `claude-code` | `.claude/skills` | Skills + audited advisory CLI execution | Native |
| `github-copilot` | `.github/skills` | Skills + audited advisory CLI execution | Native |
| `gemini-cli` | `.gemini/skills` | Skills + audited advisory CLI execution | Native |
| `cursor` | `.cursor/skills` | Skills | Native |
| `cline` | `.cline/skills` | Skills | Native |
| `kiro` | `.kiro/skills` | Skills | Native |
| `junie` | `.junie/skills` | Skills | Native |
| `devin` | `.cognition/skills` | Skills | Native |
| `qwen-code` | `.qwen/skills` | Skills | Native |
| `rovo-dev` | `.rovodev/skills` | Skills | Native |
| `opencode` | `.opencode/skills` | Skills | Native |
| `kimi-code` | `.kimi-code/skills` | Skills | Native |
| `factory-droid` | `.factory/skills` | Skills | Native |
| `generic-agents` | `.agents/skills` | Universal Agent Skills bootstrap | Portable |

### Shared-native consumers

Two requested harnesses are intentionally **not** represented by fake duplicate skill manifests:

- **Goose** consumes the universal `.agents/skills` layout; use the canonical project source directly or `generic-agents` when SDAI should materialize that layout. Goose also supports `.goosehints` context, but converting an arbitrary SDAI agent/rule format into `.goosehints` would require transformation semantics that Integration v1 deliberately does not pretend to provide.
- **Zed Agent** protects and consumes project `.agents/skills` directly and supports project `AGENTS.md`. Use the canonical layout or `generic-agents`. A no-op `.agents/skills -> .agents/skills` manifest would violate v1's source/target ownership model and add no value.

This is classified as **native shared support**, not unsupported support. The absence of a redundant tool-specific manifest is deliberate.

## Optional native companions

Optional surfaces live in separate Integrations so a base skill Integration never fails merely because a project does not define tool-specific commands or subagents.

| Integration | Neutral project source | Native target | Kind |
|---|---|---|---|
| `cursor-commands` | `.sdai/integration-assets/cursor/commands` | `.cursor/commands` | command |
| `qwen-code-commands` | `.sdai/integration-assets/qwen-code/commands` | `.qwen/commands` | command |
| `rovo-dev-subagents` | `.sdai/integration-assets/rovo-dev/subagents` | `.rovodev/subagents` | agent-file |
| `factory-droids` | `.sdai/integration-assets/factory/droids` | `.factory/droids` | agent-file |

The asset files must already be in the native format expected by the destination harness. v1 materialization is intentionally byte-preserving; it does not reinterpret frontmatter, model names, tool permissions, or provider-specific schemas.

## Audited execution manifests

Codex, Claude Code, GitHub Copilot CLI, and Gemini CLI already have mature SDAI provider execution boundaries. Their builtin Integration manifests encode the same **advisory/read-only** defaults declaratively:

- one executable plus an argv list;
- no shell;
- task input on UTF-8 stdin;
- normalized stdout output;
- explicit 600-second timeout;
- `requiresNetwork: true`;
- `requiresWorkspaceWrite: false`;
- only declared authentication/config environment variable names.

These manifests do not grant workspace-write access. A future writable Integration must declare that requirement and still pass the effective SDAI policy gate at execution time.

## Custom CLI template

`docs/examples/integrations/custom-cli.integration.yaml` demonstrates how a new CLI can integrate without writing a provider adapter:

```yaml
capabilities:
  - agent-execution
execution:
  executable: your-agent-cli
  argsBeforeInput: [run, --non-interactive]
  inputMode: stdin
  outputMode: json-stdout
security:
  requiresNetwork: true
  requiresWorkspaceWrite: false
  environment: [YOUR_AGENT_API_KEY]
```

Replace only manifest data: executable, argv, I/O mode, timeout, security requirements, and optional native projections. Do not encode a shell command string.

## Explicit limitations

The v1 catalog does **not** claim more than the underlying harness exposes safely:

- Provider-specific subagent files are not generated from `.sdai/agents/*.agent.md` when the schemas differ. Use an optional native-format asset projection instead.
- Shared-native `.agents/skills` consumers are not given redundant self-copy manifests.
- Tool settings, MCP configuration, hooks, permission databases, credentials, and user-home configuration are outside project-native v1 materialization unless a later version defines an explicit governed contract for them.
- Only the four already-audited SDAI CLI providers ship executable declarations in this slice. Other harnesses remain projection-only until their non-interactive invocation and safety modes are independently verified.
- A manifest describes requirements; it never widens effective SDAI policy.

## Vendor references used for the support snapshot

Primary documentation used to validate the current paths includes:

- OpenAI Codex: `AGENTS.md` and Agent Skills documentation / the official `openai/codex` repository.
- Anthropic Claude Code: project skills under `.claude/skills`.
- GitHub Copilot: Agent Skills including `.github/skills` and `.agents/skills` compatibility.
- Google Gemini CLI: project `.gemini/skills` and `.gemini/commands` documentation.
- Cursor: project `.cursor/skills` / `.agents/skills` Agent Skills and `.cursor/commands` documentation.
- Cline: project `.cline/skills` Agent Skills documentation.
- Kiro: project `.kiro/skills` Agent Skills documentation.
- JetBrains Junie: project `.junie/skills` documentation.
- Devin: project `.cognition/skills` and `.agents/skills` Agent Skills documentation.
- Qwen Code: project `.qwen/skills` and command documentation.
- Atlassian Rovo Dev: project `.rovodev/skills` and `.rovodev/subagents` documentation.
- OpenCode: project `.opencode/skills`, `.claude/skills`, and `.agents/skills` documentation.
- Kimi Code: project `.kimi-code/skills`, `.agents/skills`, `.kimi-code/agents`, and `.agents/agents` documentation.
- Factory Droid: project `.factory/skills`, `.factory/commands`, `.factory/droids`, and `AGENTS.md` documentation.
- Goose: official project context support for `.goosehints` plus the shared `.agents/skills` convention documented by Block tooling.
- Zed: project `.agents/skills` and `AGENTS.md` documentation.

When a vendor adds or changes a surface, update the affected manifest version and this matrix; do not add an integration-specific branch to core resolution/materialization code.
