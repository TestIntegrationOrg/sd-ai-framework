# Configuration modes

SD-AI is one framework with one runtime and one capability set. `individual` and
`enterprise` change **where policy comes from and who may widen permissions**; they do
not enable or remove features.

Both modes support semantic `.agent.md` files, shared `SKILL.md` skills, prompts,
custom workflows, manual step execution, Codex, Claude, Copilot, Gemini, custom/local
providers, provider overrides, workspace-write, approvals, GitHub/Jira integrations,
quality gates, and policy enforcement.

## Individual mode

Individual mode is the default when `operating_mode` is omitted.

```yaml
# .sdai/config.yaml
operating_mode: individual
```

The engineer may configure provider profiles in `.sdai/agents.yaml`, repository policy
in `.sdai/policy.yaml`, and optionally a personal policy file through
`SDAI_USER_POLICY_PATH`.

```yaml
# .sdai/policy.yaml
version: 1
providers: {}
execution:
  workspace_write: true
  require_prior_approval_for_workspace_write: false
  allow_force_approval_bypass: true
  protected_paths:
    - .github/workflows/**
skills:
  required:
    coding: [secure-coding]
```

No organization allowlist is required, so `--profile claude`, `--profile codex`, a
local model, or a custom provider can be selected as long as the profile itself is
configured and enabled.

Core SD-AI source-of-truth paths remain protected from external workspace-writing
agents in both modes: `.sdai/**`, `.agents/**`, provider-native generated agent files,
and `specs/**`. Framework-owned lifecycle commands may still update those artifacts.

## Enterprise mode

Enterprise mode uses the same commands and provider-selection UX. The company supplies
an organization policy file outside the repository and exposes its absolute path as
`SDAI_ORG_POLICY_PATH`.

```yaml
# .sdai/config.yaml
operating_mode: enterprise
```

or a centrally managed environment can set:

```text
SDAI_OPERATING_MODE=enterprise
SDAI_ORG_POLICY_PATH=/company-managed/sdai/organization-policy.yaml
```

If `SDAI_ORG_POLICY_PATH` is present, organization policy is applied even when a
repository says `operating_mode: individual`. This prevents a repo-local change from
weakening company controls.

Example organization policy:

```yaml
version: 1
providers:
  allowed_profiles:
    - claude-enterprise
    - codex-enterprise
    - copilot-enterprise
  allowed_providers: [claude, codex, copilot]
  allowed_models:
    claude: [approved-architecture-model]
    codex: [approved-coding-model]
    copilot: [approved-review-model]

capabilities:
  architecture:
    allowed_profiles: [claude-enterprise, codex-enterprise]
  coding:
    allowed_profiles: [codex-enterprise, copilot-enterprise]

execution:
  workspace_write: true
  require_prior_approval_for_workspace_write: true
  allow_force_approval_bypass: false
  protected_paths:
    - .github/workflows/**
  environment_allowlist:
    - HTTPS_PROXY
    - NO_PROXY
    - OPENAI_API_KEY
    - ANTHROPIC_API_KEY
    - GH_TOKEN

skills:
  required:
    architecture: [company-architecture-standard]
    coding: [company-secure-coding]
```

The employee still chooses within the approved set:

```bash
sdai step run PAY-123 architecture-review \
  --workflow enterprise \
  --agent architect \
  --profile claude-enterprise

sdai step run PAY-123 architecture-review \
  --workflow enterprise \
  --agent architect \
  --profile codex-enterprise
```

An unapproved profile or model is denied before its provider process starts.

## Policy layering

Effective policy is computed from these layers:

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

Allow lists are intersected. Mandatory skills and protected paths are accumulated.
Boolean restrictions such as disabling workspace writes cannot be re-enabled by a
lower layer. Therefore repository/user policy may become stricter but cannot expand
beyond an organization allowlist.

## Provider environment isolation

External CLI agents do not inherit the whole developer/CI environment. SD-AI passes a
minimal OS environment, provider authentication variables, and profile variables from
`environment_allowlist`. When policy defines `execution.environment_allowlist`, those
additional/provider variables are filtered through it.

Custom Python provider plugins execute in-process and therefore remain trusted code;
enterprises should allow only approved plugins.

## Protected workspace writes

A `workspace-write` agent may change application files, tests, and other unprotected
repository content. SD-AI snapshots protected paths before execution. If an agent
changes a protected source-of-truth path, SD-AI restores those files and fails the
agent step.

This is defense-in-depth around provider-native sandboxes and is intentionally applied
in both individual and enterprise modes.
