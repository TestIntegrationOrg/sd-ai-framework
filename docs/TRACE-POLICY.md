# Trace Coverage Policy

SDAI 0.10 turns current trace facts into deterministic risk-based policy gates. Policy evaluation is read-only and provider-neutral: only canonical graph relationships and **valid current** typed evidence count.

## Command

```text
sdai trace policy FEATURE --risk trivial|standard|critical|regulated
sdai trace policy FEATURE --risk critical --json
```

Exit codes:

- `0` all effective thresholds are satisfied
- `2` one or more coverage dimensions are below policy and produce blocking findings
- `1` policy, graph, evidence, or operational validation failed

Machine output uses `sdai.trace-policy-report/v1`.

## Coverage dimensions

Every declared requirement is evaluated across six dimensions:

| Dimension | Counts when |
|---|---|
| `requirements` | the requirement has an explicit current `evidenced-by` proof whose typed evidence freshness is `valid` |
| `tasks` | an explicit trace path from the requirement reaches a task without crossing through another requirement |
| `code` | an explicit trace path reaches a repository code node |
| `tests` | an explicit trace path reaches a test node, or reaches valid typed `test` evidence |
| `security` | the requirement trace reaches valid typed `security` evidence |
| `approvals` | the requirement trace reaches valid typed `approval` evidence |

Evidence with freshness `stale`, `missing`, or `blocked` never contributes to `requirements`, `security`, or `approvals`. Structural task/code/test graph facts remain structural facts even when separate proof evidence becomes stale.

## Built-in risk defaults

| Risk | requirements | tasks | code | tests | security | approvals |
|---|---:|---:|---:|---:|---:|---:|
| `trivial` | 0 | 0 | 0 | 0 | 0 | 0 |
| `standard` | 80 | 80 | 80 | 80 | 0 | 0 |
| `critical` | 100 | 100 | 100 | 100 | 100 | 100 |
| `regulated` | 100 | 100 | 100 | 100 | 100 | 100 |

Critical and regulated features therefore require complete current requirement proof and complete implementation/test/security/approval trace coverage by default.

## Layered policy

Trace policy follows the same authority model used by SDAI artifact schemas:

1. built-in framework defaults
2. organization policy
3. repository policy
4. user policy

Organization policy is supplied through an absolute file or directory path:

```text
SDAI_ORG_TRACE_POLICY_PATH=/opt/company/sdai/trace-policy.yaml
```

Repository policy lives at:

```text
.sdai/trace-policy.yaml
```

User policy may be supplied through:

```text
SDAI_USER_TRACE_POLICY_PATH=/home/user/.config/sdai/trace-policy.yaml
```

External variables may reference one YAML file or a directory of `*.yaml` / `*.yml` files. Symlinked or malformed policy sources fail closed.

## Policy contract

```yaml
apiVersion: sdai.trace-policy/v1
kind: TraceCoveragePolicy
metadata:
  id: company-critical
spec:
  risks:
    standard:
      requirements: 95
      tests: 90
      security: 100
    critical:
      requirements: 100
      tasks: 100
      code: 100
      tests: 100
      security: 100
      approvals: 100
```

Each value is a finite percentage from `0` through `100`. Documents are strict: unknown top-level fields, risks, dimensions, invalid identifiers, malformed YAML, and out-of-range values fail closed.

## Non-weakening rule

Threshold resolution is monotonic. For each risk and dimension, SDAI records every layer contribution and chooses the **maximum** as the effective minimum.

Example:

```text
builtin standard requirements = 80%
org     standard requirements = 95%
repo    standard requirements = 60%
user    standard requirements = 20%
------------------------------------
effective minimum             = 95%
```

A repository or user can strengthen policy but can never reduce a framework or organization minimum. JSON and human output include every contribution plus `enforced_by` provenance showing the layer/source responsible for the effective threshold.

## Current-state guarantee

Policy evaluation rebuilds the canonical graph and re-evaluates typed evidence freshness against current Git history and current bound bytes. Rewritten history, changed code/artifacts/tests, missing bindings, blocked evidence, and failed evidence therefore cannot satisfy a current policy gate.
