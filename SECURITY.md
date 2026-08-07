# Security Policy

SD-AI is an early-stage project. Do not place production secrets, private keys, credentials, tokens, or sensitive production data in prompts or generated artifacts.

## External agents

External profiles run third-party executables with the current project as their working directory. Use the minimum permissions required by the task and understand each provider's sandbox, tool, network, and authentication behavior.

- `advisory` is the default execution mode.
- `workspace-write` must be selected explicitly.
- SD-AI does not automatically enable broad `allow all` permissions.
- Prompt-safety checks block selected high-confidence secret patterns before an external invocation.
- The safety guard is defense-in-depth, not a complete secret scanner.
- Keep provider credentials outside `.sdai/agents.yaml` and generated feature artifacts.

Custom command providers cannot be made read-only by SD-AI itself; their safety depends on the configured command and its environment.

## Reporting vulnerabilities

Please report security issues privately to the repository maintainers rather than opening a public issue with exploit details.
