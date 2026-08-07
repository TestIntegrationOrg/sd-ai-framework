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

For the normative merge rules—including provider/profile/model intersections, deny/require boolean precedence, mandatory skills, environment allowlists, and organization-policy deployment assumptions—see [Enterprise Policy Reference](ENTERPRISE-POLICY.md).

## Source-of-truth boundary

External workspace-writing agents cannot persist changes to framework governance/canonical agent files or `specs/**`. Framework-owned lifecycle commands create/update specifications, architecture, approvals, workflow state, AI artifacts, and validation evidence.

The exact built-in protected path set, restoration behavior, symlink containment, prompt containment, environment isolation, and provider-argument restrictions are documented in [Execution Security Reference](EXECUTION-SECURITY.md).

## Provider selection

Provider/profile/model choice remains available in both modes. Individual users choose among locally configured profiles. Enterprise users choose among profiles/models permitted by effective organization policy and capability-specific rules.

## Approvals

Approval artifacts remain policy assertions rather than cryptographic enterprise identity proof. Effective policy may require a prior approval before workspace-write and may prohibit manual `--force` bypass. Identity-backed SSO/GitHub Enterprise approvals remain a future control.

## Documentation authority

Use these references for detailed behavior:

- [Configuration modes](CONFIGURATION-MODES.md) — individual vs enterprise user experience and configuration model.
- [Enterprise policy](ENTERPRISE-POLICY.md) — effective-policy schema, precedence, merge semantics, and enterprise deployment boundary.
- [Execution security](EXECUTION-SECURITY.md) — provider execution trust boundary and exact v0.5.1 hardening controls.
- [Security policy](../SECURITY.md) — security posture, limitations, and vulnerability reporting.
