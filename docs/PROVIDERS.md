# Provider Adapters

SD-AI does not bundle, install, authenticate, or license third-party coding agents. It invokes installed tools behind a common provider interface.

| Provider | Executable | Integration |
|---|---|---|
| Codex | `codex` | `codex exec`, prompt on stdin; advisory uses read-only sandbox |
| GitHub Copilot | `copilot` | piped prompt with silent/noninteractive output; no broad allow-all flags |
| Claude Code | `claude` | print mode with piped task context |
| Gemini CLI | `gemini` | noninteractive prompt with piped task context |
| Custom/local | configured command | stdin by default or `{prompt}` placeholder |

Run `sdai agents doctor` to check whether configured executables are present on `PATH`. This does not prove that a provider is authenticated or licensed.

## Live execution progress

Manual agent steps emit prompt-safe progress to stderr while the provider runs:

```bash
sdai step run FEATURE-123 architecture-review --workflow agentic
sdai step run FEATURE-123 architecture-review --workflow agentic --verbose
```

The default view reports invocation start, subprocess start, periodic heartbeat, first output, completion, and failure. Subprocess-backed providers include the PID and elapsed time. `--verbose` additionally shows the semantic agent, profile, provider/model identifier, execution mode, configured timeout, UTF-8 prompt byte count, and encoding.

Progress events are metadata only. They never include the prompt, system instructions, provider stdout/stderr, command arguments, environment values, or credentials. Timeout failures use the `timeout` category and remain distinct from a normal non-zero provider exit (`provider-execution`). Durable, post-run evidence remains available through `sdai diagnostics FEATURE`.

### Avoid nested coding-agent execution

Generally do not run an SDAI Codex-backed step from inside an already-running Codex session (or nest another provider inside its own interactive agent session). The parent and child can compete for stdin/stdout, cancellation signals, credentials, sandbox authority, and workspace ownership, making failures and side effects harder to attribute. Prefer invoking `sdai step run` directly from a human-controlled terminal or CI job. If a supervising automation must launch SDAI, keep the parent non-interactive, give one layer clear workspace ownership, and preserve the child's stderr progress and exit status.

## Custom command provider

```yaml
profiles:
  internal-agent:
    provider: command
    enabled: true
    command: [company-agent, run]
    prompt: auto
    capabilities: [requirements, architecture, coding, review, testing]
    skills: [spec-traceability]
```

Commands execute without a shell. If `{prompt}` is present it is replaced; otherwise the combined task is sent on stdin.

## Python provider plugins

The factory also discovers Python entry points in the `sdai.providers` group. This allows API-backed, remote, or internal providers to be added without changing SD-AI core.
