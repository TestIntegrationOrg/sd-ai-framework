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
