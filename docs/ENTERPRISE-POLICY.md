# Enterprise Policy Reference

SD-AI uses one runtime and one capability set for both individual engineers and enterprise employees. Enterprise mode does not remove capabilities; it adds an organization-owned upper boundary that repository, user, workflow, agent, and CLI choices must respect.

## Effective policy model

SD-AI resolves policy in this order:

```text
SD-AI core invariants
        +
organization policy (when configured)
        +
repository policy
        +
user policy
        =
effective configuration
```

The organization policy is supplied through the fixed environment variable `SDAI_ORG_POLICY_PATH`. Repository configuration cannot rename or redirect that variable. The organization policy file must be an absolute path outside the repository.

If `SDAI_ORG_POLICY_PATH` is present, SD-AI treats the effective operating mode as enterprise even when `.sdai/config.yaml` says `individual`.

For enterprise deployment, the organization must also control how `SDAI_ORG_POLICY_PATH` and the policy file are provisioned—for example through a managed developer environment, corporate launcher, CI runner, endpoint-management policy, or another trusted control. SD-AI prevents repository-local redirection, but it does not itself provide operating-system identity or endpoint-policy enforcement.

## Same capabilities in both modes

Individual and enterprise modes both support Codex, Claude, GitHub Copilot, Gemini, local/custom command providers and provider plugins; semantic `.agent.md` definitions; shared `SKILL.md` skills; provider-native synchronization; custom prompts/workflows; manual steps; provider/profile/model overrides; advisory/workspace-write execution; approvals; conditions; retries; parallel reviews; pause/resume; GitHub/Jira integrations; and test/Trivy/Sonar gates.

The difference is who may widen the set of allowed choices.

## Policy merge semantics

### Allow lists: intersection

Provider, profile, model, and capability allow lists are intersected across policy layers. A lower layer cannot add a provider/profile/model that an upper layer did not allow.

```text
organization providers: Claude, Codex, Copilot
repository providers:   Claude, Codex
user providers:         Claude
----------------------------------------
effective providers:    Claude
```

### Model rules: all applicable rules apply

Provider-level and profile-level model rules both apply. Their effective allowed set is the intersection.

```yaml
providers:
  allowed_models:
    claude: [model-a, model-b]
    claude-enterprise: [model-b]
```

For profile `claude-enterprise`, only `model-b` is allowed. If policy restricts models for a selected provider/profile, the profile must explicitly pin an approved model.

### Mandatory skills: additive union

Required skills accumulate across layers. Lower layers may add mandatory skills but cannot remove upper-layer mandatory skills.

### Protected paths: additive union

Core SD-AI protected paths are always present. Organization, repository, and user policy may add more protected paths.

### Workspace-write: deny wins

If any active layer sets `execution.workspace_write: false`, workspace-write is disabled. A lower layer cannot re-enable it.

### Required approval: require wins

If any active layer sets `execution.require_prior_approval_for_workspace_write: true`, workspace-writing manual steps must satisfy prior workflow approval requirements.

### Force approval bypass: deny wins

If any active layer sets `execution.allow_force_approval_bypass: false`, `--force` cannot bypass a required prior approval.

## Enterprise provider and model choice

Enterprise policy should normally define an approved set rather than a single mandatory provider.

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

Employees may still choose any effective allowed option:

```bash
sdai step run FEATURE-123 architecture-review --workflow enterprise \
  --agent architect --profile claude-enterprise

sdai step run FEATURE-123 architecture-review --workflow enterprise \
  --agent architect --profile codex-enterprise
```

An unapproved provider/profile/model is rejected before its external provider process starts.

## Enterprise provider environment policy

External provider processes receive a minimal environment rather than inheriting the full developer or CI environment.

Enterprise mode is fail-closed for provider/profile credential environment variables. If effective policy does not define `execution.environment_allowlist`, no provider-specific credential variables are passed by SD-AI. Provider CLIs may still use their own native credential stores.

```yaml
execution:
  environment_allowlist:
    - OPENAI_API_KEY
    - ANTHROPIC_API_KEY
    - GH_TOKEN
    - HTTPS_PROXY
    - NO_PROXY
```

Repository/user policy can narrow this list but cannot expand beyond the effective organization list.

## Policy file schema

Supported top-level keys:

```yaml
version: 1
providers: {}
capabilities: {}
execution: {}
skills: {}
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

Unknown policy keys are rejected rather than silently ignored. Protected path patterns must be repository-relative and cannot contain `..` or absolute paths.

## Repository and user policy

Repository policy defaults to `.sdai/policy.yaml` and may be redirected only to another relative path inside the repository through `.sdai/config.yaml`. User policy may be supplied by absolute path through `SDAI_USER_POLICY_PATH`.

Individual mode can use repository and user policy without an organization policy. This gives individual engineers the same governance capabilities when they want stricter controls.

## Approvals and identity

Current role-backed approvals are policy assertions stored as feature artifacts. Enterprise policy can require those approvals and prevent force bypass, but they are not yet cryptographically or SSO identity-backed.

For a security-grade enterprise approval boundary, a future release should bind approval evidence to authenticated GitHub Enterprise/SSO/corporate identities and immutable audit evidence.

## Related documentation

- [Configuration modes](CONFIGURATION-MODES.md)
- [Governance model](GOVERNANCE.md)
- [Execution security](EXECUTION-SECURITY.md)
- [Security policy](../SECURITY.md)
- [Organization-policy example](../examples/organization-policy.yaml)
