# Execution Security Reference

SD-AI treats external AI providers and their output as untrusted execution components. AI-assisted implementation is allowed, but governance, effective policy, specifications, workflow/approval state, canonical agent and skill definitions, audit evidence, and selected Git/CI controls remain inside framework-owned boundaries.

This document describes the current execution-security controls for both individual and enterprise modes.

## Trust model

```text
Trusted controls and source of truth
  SD-AI core invariants
  effective policy
  approved specifications / architecture / ADRs
  canonical agents and skills
  workflow and approval state
  audit/provenance ledger
              |
              v
        SD-AI runtime
              |
              v
      External provider
      (untrusted execution/output)
              |
              v
       Repository changes
              |
              v
   protected-path verification
        /            \
       v              v
   allowed         protected
   changes          changes
       |              |
       v              v
    continue      restore + fail
```

AI output is not source of truth. An external agent may propose or implement allowed changes, but framework-owned source-of-truth artifacts remain protected.

## Advisory versus workspace-write

`advisory` is the default external execution mode. Built-in provider adapters use provider-native controls to make advisory execution read-only where supported.

`workspace-write` must be selected explicitly. It allows an external provider to modify unprotected repository content for the approved task, subject to effective policy and the protected-path guard.

Custom command providers and in-process Python provider plugins have a different trust model. A custom command may not provide an enforceable operating-system read-only sandbox, and Python plugins execute inside the SD-AI process. Enterprises should allow only reviewed providers/plugins.

## Core protected paths

The following paths are protected by SD-AI core policy and cannot be removed by repository or user policy:

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

Organization, repository, and user policy may add additional protected paths. Application paths such as `src/**`, `tests/**`, and ordinary project documentation are not core-protected by default.

## Protected-write lifecycle

For workspace-writing external execution, SD-AI performs this boundary check:

```text
1. Resolve effective policy.
2. Verify provider/profile/model/mode.
3. Snapshot protected content.
4. Execute the external provider.
5. Re-scan protected paths.
6. Detect protected mutations, including symlink replacement.
7. Restore protected content and fail if a protected mutation occurred.
8. Retain allowed unprotected changes for normal validation/review.
9. Persist framework-owned workflow and audit evidence.
```

Framework-owned lifecycle commands may intentionally update protected source-of-truth artifacts. The restriction applies to untrusted external provider execution, not to the SD-AI runtime acting under its own lifecycle authority.

## Symlink and path containment

Sensitive paths are resolved before reads/writes. Feature artifacts and prompt paths must remain inside their intended roots after symlink resolution. The protected-write guard also rejects protected paths replaced with symlinks and restores the protected state.

## Prompt safety

Profile prompts are resolved below `.sdai/prompts`; absolute paths, traversal, and symlink escapes are rejected. Feature-context reads are contained below the selected feature workspace.

Prompt safety checks occur before provider execution and before dry-run rendering. Known credential/private-key patterns are blocked as defense in depth. This does not replace repository or CI secret scanning.

External issue text, source snippets, logs, scanner findings, integration content, and generated artifacts are treated as untrusted data rather than higher-priority instructions.

## Provider subprocess environment

SD-AI does not copy the caller's entire environment into external CLI providers. The environment is divided into two security classes.

### Process-runtime baseline

A minimal set needed for ordinary process startup/runtime remains available even under a fail-closed enterprise allowlist:

```text
PATH
TMP / TEMP / TMPDIR
LANG / LC_ALL
TERM
COMSPEC / SYSTEMROOT / WINDIR / PATHEXT
```

These values are not intended to provide provider credential discovery or network routing.

### Policy-gated environment

Variables that may expose credential stores, user configuration, network routing, or trust configuration are policy-gated:

```text
HOME / USERPROFILE
APPDATA / LOCALAPPDATA / XDG_CONFIG_HOME
HTTPS_PROXY / HTTP_PROXY / NO_PROXY
SSL_CERT_FILE / REQUESTS_CA_BUNDLE
```

Known provider authentication/config variables are also policy-gated:

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

Profile-specific environment variables are policy-gated as well.

### Individual mode

When no effective environment restriction is configured, SD-AI preserves normal provider authentication/config discovery while still excluding unrelated environment secrets.

### Enterprise mode

Enterprise mode is fail-closed for every policy-gated variable. The organization policy is authoritative:

- If the organization omits `execution.environment_allowlist`, the effective policy-gated allowlist is empty.
- A repository or user policy cannot widen that omission.
- If the organization supplies an allowlist, lower layers may only narrow it.
- A provider can use a native credential store only when the organization policy **explicitly allows** the environment variables required to discover that native credential store (for example `HOME`, `USERPROFILE`, `APPDATA`, or a provider-specific config-home variable).
- Proxy, custom trust-root, and provider credential variables likewise require explicit organization permission.

This prevents repositories or user-local configuration from silently reintroducing provider credentials, cloud configuration, proxy routing, database/Jira tokens, or arbitrary CI secrets into a third-party provider process.

Example organization policy:

```yaml
version: 1
execution:
  environment_allowlist:
    - HOME
    - OPENAI_API_KEY
    - HTTPS_PROXY
    - NO_PROXY
```

## Provider argument controls

Provider profiles may use `extra_args` for normal configuration, but built-in providers reject arguments that attempt to alter SD-AI-owned permission/sandbox boundaries. Security-sensitive categories include sandbox, approval, permissions, shell/network/tool controls, MCP, hooks, full-auto/yolo, and similar privilege-affecting flags.

## Enterprise policy invariants

Before provider execution, effective policy checks the selected profile/provider/model and execution mode. Organization policy can restrict providers/profiles/models/capabilities, disable workspace-write, require approval, prohibit force bypass, add protected paths, require skills, require architecture evidence, prohibit waivers, and bound the provider environment.

Repository and user policy may narrow organization policy but cannot widen organization-owned denies or allowlists. Core protected paths cannot be removed.

## Audit and provenance

Current SD-AI releases include tamper-evident audit/provenance support. Agent and workflow execution can be hash-bound to verified ledger events, configuration/policy inputs, artifacts, trace evidence, and immutable export packages. Audit evidence is not an authorization database and does not turn an `approved_by` string into verified enterprise identity.

Identity-backed enterprise approvals remain a separate held/deferred capability; do not infer authenticated corporate identity from local approval artifacts. See [Audit + Provenance](AUDIT-PROVENANCE.md).

## Failure behavior

A provider failure fails the current agent step according to retry and `on_failure` behavior. A protected-path violation fails after protected content is restored. Allowed unprotected application changes from the same attempt may remain for inspection/reconciliation, but the step is not treated as successful.

## Remaining deployment responsibilities

Execution hardening does not replace enterprise platform controls. High-assurance deployments should additionally use corporate identity and endpoint management for `SDAI_ORG_POLICY_PATH`, branch protection and CODEOWNERS, required CI checks, approved provider accounts, repository secret scanning/DLP, and container/runner isolation where stronger OS-level sandboxing is required.

Python provider plugins execute in-process and must be treated as trusted code. Custom command providers may not supply a provider-native read-only sandbox. Prompt secret detection remains defense in depth rather than complete DLP.

## Related documentation

- [Enterprise policy](ENTERPRISE-POLICY.md)
- [Audit + Provenance](AUDIT-PROVENANCE.md)
- [Configuration modes](CONFIGURATION-MODES.md)
- [Governance model](GOVERNANCE.md)
- [Security policy](../SECURITY.md)
