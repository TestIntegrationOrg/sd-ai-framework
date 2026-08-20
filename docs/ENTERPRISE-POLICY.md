# Enterprise Policy Reference

SD-AI uses one runtime and capability model for individual engineers and enterprise employees. Enterprise mode adds an organization-owned upper boundary that repository, user, workflow, agent, and CLI choices must respect.

## Effective policy model

```text
SD-AI core invariants
        +
organization policy
        +
repository policy
        +
user policy
        =
effective configuration
```

The organization policy is supplied through the fixed environment variable `SDAI_ORG_POLICY_PATH`. Repository configuration cannot rename or redirect that variable. The policy path must be absolute and outside the repository.

If `SDAI_ORG_POLICY_PATH` is present, SD-AI treats the effective mode as enterprise even when repository configuration says `individual`. If enterprise mode is configured without a company-managed organization policy, loading fails closed.

The organization must control how `SDAI_ORG_POLICY_PATH` and the policy file are provisioned, for example with a managed developer environment, corporate launcher, CI runner, or endpoint-management policy.

## Merge semantics

### Allow lists: intersection

Provider, profile, model, capability, and environment allowlists can only become narrower. A lower layer cannot add an option excluded by an upper layer.

Provider-level and profile-level model rules both apply; the effective model set is their intersection.

### Mandatory requirements: additive

Required skills and protected paths accumulate. Core protected paths are always present and cannot be removed. Required architecture artifacts also accumulate.

### Denies win

For security-sensitive booleans:

- `execution.workspace_write: false` wins over lower-layer `true` values.
- `execution.require_prior_approval_for_workspace_write: true` remains required.
- `execution.allow_force_approval_bypass: false` cannot be re-enabled below it.
- `architecture_validation.allow_waivers: false` cannot be re-enabled below it.

## Provider and model policy

Enterprise policy should normally define an approved set rather than forcing one provider:

```yaml
version: 1
providers:
  allowed_profiles: [claude-enterprise, codex-enterprise, copilot-enterprise]
  allowed_providers: [claude, codex, copilot]
  allowed_models:
    claude: [approved-architecture-model]
    codex: [approved-coding-model]
capabilities:
  architecture:
    allowed_profiles: [claude-enterprise, codex-enterprise]
  coding:
    allowed_profiles: [codex-enterprise, copilot-enterprise]
```

Employees may choose any option remaining in the effective set. An unapproved provider/profile/model is rejected before its external process starts.

## Enterprise provider environment policy

External provider processes receive a bounded environment rather than the caller's complete environment.

A small process-runtime baseline (`PATH`, temporary-directory variables, locale/terminal variables, and required Windows process variables) remains available so the executable can launch normally.

Everything that can influence provider credential discovery, user configuration, proxy routing, custom trust roots, provider authentication, or profile-specific environment is policy-gated. Examples include:

```text
HOME / USERPROFILE
APPDATA / LOCALAPPDATA / XDG_CONFIG_HOME
HTTPS_PROXY / HTTP_PROXY / NO_PROXY
SSL_CERT_FILE / REQUESTS_CA_BUNDLE
OPENAI_API_KEY / CODEX_HOME
GH_TOKEN / GITHUB_TOKEN / GH_HOST
ANTHROPIC_API_KEY / CLAUDE_CONFIG_DIR
GEMINI_API_KEY / GOOGLE_API_KEY
GOOGLE_APPLICATION_CREDENTIALS / GOOGLE_CLOUD_PROJECT
```

The organization policy is authoritative for these variables:

- If organization policy omits `execution.environment_allowlist`, the effective policy-gated allowlist is empty.
- Repository and user policy cannot widen an organization omission.
- If organization policy defines an allowlist, lower layers may only narrow it.
- Native provider credential-store discovery requires the organization to explicitly allow the relevant discovery variables such as `HOME`, `USERPROFILE`, `APPDATA`, or a provider-specific configuration-home variable.

Example:

```yaml
version: 1
execution:
  environment_allowlist:
    - HOME
    - OPENAI_API_KEY
    - HTTPS_PROXY
    - NO_PROXY
```

This design prevents repository-controlled policy from silently reintroducing employee/CI credentials or network configuration into a third-party provider process.

## Policy schema

Supported top-level keys:

```yaml
version: 1
providers: {}
capabilities: {}
execution: {}
skills: {}
architecture_validation: {}
```

Provider keys:

```yaml
providers:
  allowed_profiles: []
  allowed_providers: []
  allowed_models: {}
```

Capability keys:

```yaml
capabilities:
  architecture:
    allowed_profiles: []
    allowed_providers: []
```

Execution keys:

```yaml
execution:
  workspace_write: true
  require_prior_approval_for_workspace_write: false
  allow_force_approval_bypass: true
  protected_paths: []
  environment_allowlist: []
```

Skill keys:

```yaml
skills:
  required:
    coding: [secure-coding]
```

Architecture-validation keys:

```yaml
architecture_validation:
  required:
    critical: [threat-model, deployment-view]
  allow_waivers: false
```

Unknown policy keys are rejected rather than ignored. Protected path patterns must be repository-relative and cannot contain `..` or absolute paths.

## Repository and user policy

Repository policy defaults to `.sdai/policy.yaml` and may be redirected only to another relative path inside the repository. User policy may be supplied by absolute path through `SDAI_USER_POLICY_PATH`.

Individual mode can use repository and user policy without an organization layer. In enterprise mode, neither layer can expand organization authority.

## Approvals, identity, and provenance

Local approval files remain workflow assertions. Enterprise policy can require approvals and prevent force bypass, but identity-backed enterprise approvals are still held/deferred; local `approved_by` values must not be represented as SSO-verified identities.

Tamper-evident audit/provenance is implemented separately and records/hash-binds execution evidence without becoming an authorization database. See [Audit + Provenance](AUDIT-PROVENANCE.md).

## Related documentation

- [Execution security](EXECUTION-SECURITY.md)
- [Audit + Provenance](AUDIT-PROVENANCE.md)
- [Configuration modes](CONFIGURATION-MODES.md)
- [Governance model](GOVERNANCE.md)
- [Security policy](../SECURITY.md)
- [Organization-policy example](../examples/organization-policy.yaml)
