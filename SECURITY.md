# Security Policy

SD-AI treats AI output and external issue/log/scanner content as untrusted. Do not place
production secrets, private keys, credentials, tokens, or sensitive production data in
prompts or generated artifacts.

## Configuration modes

Individual and enterprise modes use the same security engine. Enterprise mode adds a
company-managed organization policy through `SDAI_ORG_POLICY_PATH`; repository and user
configuration may narrow but cannot expand organization provider/model permissions.
Organization policy must be stored outside the repository.

## External agents

- `advisory` is the default execution mode.
- `workspace-write` must be selected explicitly.
- Built-in provider adapters own their sandbox/tool/approval flags; `extra_args` cannot
  override those privilege controls.
- External CLI providers receive a minimal environment rather than inheriting the
  caller's complete environment. Provider/profile variables are allowlisted.
- Prompt-safety checks run when an invocation is built, so dry-run cannot print content
  that real execution would reject.
- The prompt safety guard is defense-in-depth, not a complete secret scanner.

Custom command providers cannot always be made OS-level read-only by SD-AI; their
safety depends on the configured command. Python provider plugins execute in-process and
must be treated as trusted code.

## Protected source-of-truth paths

Workspace-writing agents are not allowed to modify SD-AI governance, canonical
agent/skill definitions, provider-native generated definitions, or `specs/**`.
SD-AI snapshots protected paths around an external workspace-write execution. If those
paths change, it restores them and fails the step. Organization/repository/user policy
may add more protected paths.

## Path containment

Feature artifact and prompt reads/writes resolve symlinks and are required to remain
inside their intended project/feature directories. This prevents repository-controlled
symlinks or `../` prompt names from reading or writing outside the workspace.

## Enterprise integrations

- GitHub authentication is delegated to the local `gh` CLI; SD-AI does not persist
  GitHub tokens.
- Jira credentials are read from environment variables only and Jira base URLs must use
  HTTPS.
- `.sdai/integrations.yaml` contains integration configuration/variable names, not
  credential values.

## Approvals

Role-backed approvals are policy assertions stored as feature artifacts. They are not
yet cryptographically signed and do not independently prove enterprise identity or
group membership. In enterprise mode, organization policy can require prior approval
for workspace-write and can prohibit `--force` bypass. Identity-backed approval remains
a future control for security-grade non-repudiation.

## Reporting vulnerabilities

Please report security issues privately to repository maintainers rather than opening a
public issue with exploit details.
