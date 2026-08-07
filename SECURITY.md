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

## Enterprise integrations

- GitHub authentication is delegated to the local `gh` CLI; SD-AI does not persist GitHub tokens.
- Jira credentials are read from environment variables only and Jira base URLs must use HTTPS.
- `.sdai/integrations.yaml` contains integration configuration/variable names, not credential values.

## Quality gates

Quality-gate commands are executed as argument lists and never through an implicit shell. This reduces command-injection risk from workflow configuration.

Gate stdout/stderr may still contain sensitive information emitted by third-party tools. Before persisting a gate report, SD-AI redacts values from environment variables whose names look credential-sensitive (for example token/secret/password/key variables) and bounds the maximum stored output size.

This redaction is defense-in-depth. Configure scanners and build tools not to print credentials in the first place.

## Approvals

v0.4 role-backed approvals are policy assertions stored as feature artifacts. They are not yet cryptographically signed and do not independently prove enterprise identity or group membership. Use identity allowlists where appropriate and treat signed/external identity verification as a separate control until that capability is implemented.

## Reporting vulnerabilities

Please report security issues privately to the repository maintainers rather than opening a public issue with exploit details.
