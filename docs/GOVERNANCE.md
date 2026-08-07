# Governance Model

SD-AI uses one governance/runtime model for individual engineers and enterprise employees.
The difference is which policy layers are present and which layer owns the upper bound on permissions.

## Policy layers

```text
SD-AI core invariants
        +
organization policy (optional for individual, required for enterprise)
        +
repository policy
        +
user policy
        =
effective configuration
```

Organization policy is discovered through `SDAI_ORG_POLICY_PATH` and must be outside the repository. When it is present, SD-AI treats the effective mode as enterprise even if repo configuration requests individual mode.

Allow lists are intersected. Required skills and protected paths accumulate. A lower layer may become stricter but cannot widen an organization allowlist or re-enable a capability the organization disabled.

## Source-of-truth boundary

External workspace-writing agents cannot persist changes to framework governance/canonical agent files or `specs/**`. Framework-owned lifecycle commands create/update specifications, architecture, approvals, workflow state, AI artifacts, and validation evidence.

## Provider selection

Provider/profile/model choice remains available in both modes. Individual users choose among locally configured profiles. Enterprise users choose among profiles/models permitted by effective organization policy and capability-specific rules.

## Approvals

Approval artifacts remain policy assertions rather than cryptographic enterprise identity proof. Effective policy may require a prior approval before workspace-write and may prohibit manual `--force` bypass. Identity-backed SSO/GitHub Enterprise approvals remain a future control.
