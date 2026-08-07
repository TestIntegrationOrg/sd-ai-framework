# Execution Security Reference

SD-AI treats external AI providers and their output as untrusted execution components. The framework allows AI-assisted implementation, but it keeps governance, canonical agent/skill definitions, approvals, specifications, workflow state, and selected Git/CI controls inside a protected framework-owned boundary.

This document describes the v0.5.1 execution controls for both individual and enterprise modes.

## Trust model

```text
Trusted inputs / controls
  SD-AI core invariants
  effective policy
  approved specification / architecture / ADRs
  canonical agent and skill definitions
  workflow and approval state
             │
             ▼
       SD-AI runtime
             │
             ▼
       External provider
       (untrusted output)
             │
             ▼
      Repository changes
             │
             ▼
  Protected-path verification
             │
      ┌──────┴──────┐
      ▼             ▼
   allowed       protected
   changes        changes
      │             │
      ▼             ▼
   continue     restore + fail
```

AI output is not source of truth. An external agent may propose or implement allowed changes, but framework-owned source-of-truth artifacts remain protected.

## Advisory versus workspace-write

`advisory` is the default external execution mode. Built-in provider adapters use provider-native controls to make advisory execution read-only where the provider supports such controls.

`workspace-write` must be selected explicitly. It allows an external provider to modify unprotected repository content for the approved task, subject to effective policy and the protected-path guard.

Custom command providers and in-process Python provider plugins have a different trust model. A custom command may not provide an enforceable operating-system read-only sandbox, and Python plugins execute inside the SD-AI process. Enterprises should allow only reviewed/approved custom providers and plugins.

## Built-in protected paths

The following paths are core protected paths in v0.5.1 and cannot be removed by repository or user policy:

```text
.sdai/**
.agents/**
.codex/agents/**
.claude/agents/**
.claude/skills/**
.gemini/agents/**
.github/agents/**
.github/workflows/**
.github/CODEOWNERS
CODEOWNERS
.git/config
.git/HEAD
.git/index
.git/hooks/**
.git/refs/**
specs/**
```

Organization, repository, and user policy may add additional protected paths.

Examples of application paths that are not core-protected by default include `src/**`, `tests/**`, and ordinary project documentation. Organizations may protect additional paths according to their engineering controls.

## Protected-write lifecycle

For a workspace-writing external agent, SD-AI performs the protected-path check around provider execution:

```text
1. Resolve effective policy
2. Verify provider/profile/model/mode
3. Snapshot protected files
4. Execute external agent
5. Re-scan protected paths
6. Detect protected changes
7. If protected paths changed:
      restore original protected files
      remove newly created protected files/symlinks
      fail the agent step
8. If no protected paths changed:
      retain allowed repository changes
9. Persist framework-owned AI output / workflow state
```

Framework-owned lifecycle commands may intentionally update `specs/**`, `.sdai/**`, approval artifacts, workflow state, and other source-of-truth data. The restriction applies to external workspace-writing agents, not to the SD-AI runtime itself.

## Symlink containment

SD-AI resolves sensitive paths before reading or writing them. Feature artifact and prompt paths must remain inside their intended roots after symlink resolution.

The protected-write guard also rejects protected paths that are replaced with symlinks and restores the protected state. This prevents an agent from replacing a protected directory with a link to another location and using that indirection to evade the boundary.

## Prompt containment

Profile prompt names are resolved under `.sdai/prompts`. Absolute paths, `..` traversal, and symlink escapes are rejected.

Feature context reads are also contained inside `specs/<feature>` after symlink resolution. This prevents a repository-controlled feature artifact symlink from causing arbitrary local files to be included in an AI prompt.

## Prompt safety and dry-run

Prompt safety checks run while an invocation is built, before execution and before a dry-run can render sensitive prompt content.

The prompt guard blocks known credential/private-key patterns as defense-in-depth. It is not a complete secret scanner and does not replace repository/CI secret scanning.

External issue text, source snippets, logs, scanner results, Jira content, GitHub issue content, and generated artifacts are treated as untrusted data. The system prompt tells lifecycle agents not to treat such artifact text as higher-priority instructions.

## Provider subprocess environment

SD-AI does not pass the caller's complete process environment to external CLI providers.

The baseline environment contains only operating-system/runtime variables needed for normal process execution, including values such as:

```text
PATH
HOME / USERPROFILE
TMP / TEMP / TMPDIR
LANG / LC_ALL
TERM
COMSPEC / SYSTEMROOT / WINDIR / PATHEXT
APPDATA / LOCALAPPDATA / XDG_CONFIG_HOME
HTTPS_PROXY / HTTP_PROXY / NO_PROXY
SSL_CERT_FILE / REQUESTS_CA_BUNDLE
```

Known provider authentication variables are considered separately:

```text
Codex:
  OPENAI_API_KEY
  CODEX_HOME

GitHub Copilot:
  GH_TOKEN
  GITHUB_TOKEN
  GH_HOST

Claude:
  ANTHROPIC_API_KEY
  CLAUDE_CONFIG_DIR

Gemini:
  GEMINI_API_KEY
  GOOGLE_API_KEY
  GOOGLE_APPLICATION_CREDENTIALS
  GOOGLE_CLOUD_PROJECT
```

Profile-specific environment variables may also be requested through the profile environment allowlist.

### Individual mode

Without an effective policy environment restriction, provider authentication variables and profile-approved variables may be passed from the user's environment.

### Enterprise mode

Enterprise mode is fail-closed for provider/profile variables. If effective policy has no `execution.environment_allowlist`, SD-AI passes none of the provider-specific credential variables above. The provider may still authenticate through its own native credential store.

When the organization defines an environment allowlist, only the intersection of requested variables and effective policy is passed.

This prevents unrelated values such as cloud credentials, database passwords, Jira tokens, and arbitrary CI secrets from automatically entering a third-party provider process.

## Provider argument controls

Provider profiles may use `extra_args` for normal provider configuration, but built-in providers reject arguments that attempt to alter the SD-AI-owned security boundary.

Privilege-affecting categories include sandbox, permission, approval, write, shell, network, tool allow/deny, MCP, hooks, full-auto/yolo, and similar execution-control arguments.

The built-in provider adapter—not a profile's `extra_args`—owns these security-sensitive flags.

This prevents a profile from declaring `advisory` mode while appending an argument that silently re-enables workspace writes or unrestricted tools.

## Enterprise policy interaction

Before provider execution, effective policy checks the selected profile/provider/model and the requested execution mode.

Enterprise policy can:

- restrict allowed profiles and providers
- restrict models by provider and/or profile
- restrict providers/profiles by capability
- disable workspace-write
- require prior approval before workspace-write
- prohibit `--force` approval bypass
- add protected paths
- add mandatory skills
- restrict environment variables passed to providers

Repository/user policy may narrow organization policy but cannot expand it.

See [Enterprise policy](ENTERPRISE-POLICY.md) for exact merge semantics.

## Manual execution

Manual execution remains supported in both modes:

```bash
sdai step run FEATURE-123 implementation --workflow enterprise
```

The manual path uses the same effective provider/model/mode checks as normal workflow execution.

If policy requires prior approval before workspace-write, the step is blocked until the approval is satisfied. If effective policy also sets `allow_force_approval_bypass: false`, `--force` cannot override that requirement.

## Failure behavior

A provider failure fails the current agent step according to workflow retry and `on_failure` behavior.

A protected-path violation is treated as an execution failure after SD-AI restores protected content. Allowed application changes made during the same provider execution may remain in the working tree; the step is still failed so the developer can inspect/reconcile the attempt rather than treating it as successful.

## Security limitations and future controls

v0.5.1 materially hardens local execution, but several enterprise controls remain separate deployment concerns or roadmap items:

- organization identity and endpoint policy must control the trustworthiness of `SDAI_ORG_POLICY_PATH`
- role-backed approvals are not yet cryptographically/SSO identity-backed
- Python provider plugins execute in-process and must be treated as trusted code
- custom command providers may not offer a provider-native read-only sandbox
- prompt secret detection is defense-in-depth, not a full DLP system
- the current workspace guard protects critical paths but does not yet execute every provider inside a disposable OS/container sandbox
- organization-wide immutable audit/provenance is a future control

For high-assurance enterprise deployments, use SD-AI together with repository branch protection, CODEOWNERS, CI required checks, corporate identity, endpoint controls, approved provider accounts, and normal secure software supply-chain controls.

## Related documentation

- [Enterprise policy](ENTERPRISE-POLICY.md)
- [Configuration modes](CONFIGURATION-MODES.md)
- [Governance model](GOVERNANCE.md)
- [Security policy](../SECURITY.md)
