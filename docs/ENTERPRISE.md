# Enterprise Integrations and Governance

SD-AI v0.4 adds an enterprise control layer without making a specific work tracker, AI provider, scanner, or CI system the framework's control plane.

## Configuration files

`sdai init` and `sdai upgrade` add these files when they are missing:

```text
.sdai/
├── governance.yaml
├── approval-policies.yaml
├── quality-gates.yaml
├── integrations.yaml
└── workflows/
    └── enterprise.yaml
```

Team-owned files are never overwritten by upgrade.

## Approval governance

Approval artifacts are feature scoped:

```text
specs/<feature>/approvals/<gate>.yaml
```

Policies support:

- minimum distinct approvers
- required roles
- optional identity allowlists

Example:

```yaml
gates:
  production-architecture:
    min_approvals: 2
    required_roles: [architect, security]
    allowed_approvers:
      - architect@example.com
      - security@example.com
```

Record approvals with:

```bash
sdai approve FEATURE-123 production-architecture \
  --by architect@example.com \
  --role architect
```

A repeated approval from the same identity replaces that identity's previous record and does not inflate the distinct-approver count.

The default v0.4 scaffold keeps the older `architecture` gate identity-only for v0.3 compatibility and provides role-backed `enterprise-architecture` and `enterprise-security` gates.

> Current role/identity assertions are local policy assertions. Cryptographically signed or externally verified enterprise identity is a future extension.

## Workflow policy-as-code

`.sdai/governance.yaml` can restrict:

- maximum parallel-agent fan-out
- allowed workspace-write profiles
- required quality gates by validation rigor
- whether workflow-policy findings block automatic execution

Example:

```yaml
workflow:
  enforce: true
  max_parallelism: 4
  allowed_workspace_write_profiles: [codex, copilot]

quality:
  required_gates:
    light: []
    standard: [tests]
    critical: [tests, trivy, sonar]
```

Check policy without running the workflow:

```bash
sdai policy check --workflow enterprise
```

Policy enforcement is disabled by default so existing v0.1-v0.3 workflows continue to run after upgrade.

## Conditions

Workflow conditions use a small non-executable DSL:

```text
always
never
env:NAME
env:NAME=value
artifact:relative/path
approved:gate
step:step-id=completed
not:<condition>
```

No Python expression or `eval` is used.

Example:

```yaml
- id: trivy
  type: quality-gate
  gate: trivy
  if: env:SDAI_TRIVY
```

A normal workflow run marks a false condition as condition-skipped and continues. A deliberate manual `--force` can bypass the condition for investigation or recovery.

## Retry and failure behavior

Steps may define:

```yaml
retry:
  max_attempts: 3
  delay_seconds: 2
  backoff_multiplier: 2
on_failure: stop
```

`on_failure` values:

- `stop` — stop the automatic workflow
- `continue` — record the failure and continue executing later workflow steps

Manual execution always returns the result of the selected step directly.

## Parallel AI review

Parallel groups are bounded by `governance.yaml` and currently permit only advisory/read-only agent children:

```yaml
- id: design-review
  type: parallel
  steps:
    - id: architecture
      type: agent
      capability: architecture
      profile: claude
      mode: advisory
    - id: security
      type: agent
      capability: security
      profile: copilot
      mode: advisory
```

This restriction prevents multiple write-capable agents from concurrently changing the same working tree.

Each child has its own prompt, skills, profile, retry policy, condition, and persisted output artifact.

## Quality gates

`.sdai/quality-gates.yaml` defines command-based checks as argument arrays rather than shell strings.

Example:

```yaml
gates:
  tests:
    enabled: true
    command: [pytest, -q]
    success_exit_codes: [0]

  trivy:
    enabled: true
    command: [trivy, fs, --exit-code, "1", --severity, HIGH,CRITICAL, .]
    success_exit_codes: [0]

  sonar:
    enabled: true
    command: [sonar-scanner, -Dsonar.qualitygate.wait=true]
    success_exit_codes: [0]
```

Run a gate manually:

```bash
sdai gates run tests --feature-id FEATURE-123
```

When a feature ID is supplied, the result is saved under:

```text
specs/<feature>/quality-gates/<gate>.md
```

Those reports become bounded context for downstream review agents.

## GitHub

The GitHub adapter delegates authentication to the installed `gh` CLI. SD-AI does not persist GitHub credentials.

Issue intake:

```bash
sdai intake github FEATURE-123 \
  --repo acme/service \
  --issue 123 \
  --workflow enterprise
```

Pull-request creation:

```bash
sdai pr create FEATURE-123 \
  --repo acme/service \
  --base main
```

The current Git branch is used as the PR head unless `--head` is supplied. PRs are drafts by default; pass `--ready` to create a ready-for-review PR.

## Jira

The Jira adapter reads credentials from the environment only.

Supported configuration:

```text
JIRA_BASE_URL
JIRA_EMAIL
JIRA_API_TOKEN
```

or:

```text
JIRA_BASE_URL
JIRA_BEARER_TOKEN
```

Create intake:

```bash
sdai intake jira PROJ-123 --workflow enterprise
```

Jira rich-text descriptions are flattened into readable feature-intake text while the source issue reference remains in the artifact.

## Integration doctor

```bash
sdai integrations doctor
```

This checks local GitHub CLI availability and Jira environment configuration. It does not store or print credential values.

## Manual operation remains first-class

Enterprise governance does not remove manual control:

```bash
sdai step list FEATURE-123 --workflow enterprise
sdai step run FEATURE-123 design-reviews --workflow enterprise
sdai step run FEATURE-123 tests --workflow enterprise
```

A workspace-write AI step before an unsatisfied earlier approval requires explicit `--force`:

```bash
sdai step run FEATURE-123 implementation --workflow enterprise --force
```

The command is allowed because manual recovery is a framework requirement, but the bypass is made explicit and visible.
