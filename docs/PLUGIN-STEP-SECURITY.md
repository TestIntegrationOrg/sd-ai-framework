# Plugin Step Permission and Trusted-Executor Boundary

SDAI 0.8 introduces the security contract for genuinely new execution mechanisms. This is deliberately **not** a generic shell-step feature.

The first #77 slice establishes the manifest, policy, trusted-executor, permission, safe-argv, and structured-result APIs. Workflow/orchestrator integration is a separate focused slice so a new workflow step type is not enabled before this security contract has passed review.

## Trust model

```text
untrusted / repository-authored YAML manifest
                ↓
strict PluginStep parser
                ↓
organization → repository → user policy intersection
                ↓
trusted publisher check
                ↓
registered executor ID lookup
                ↓
permission-checked framework services
                ↓
structured PluginResult
```

A `PluginStep` manifest cannot:

- import a Python module
- specify a Python callable
- contain a shell command string
- select `shell=True`
- bypass the registered-executor registry
- write SDAI protected source-of-truth paths through framework services
- request network access in v1

Executor implementations are trusted installed code. SDAI does not pretend that in-process trusted Python code is a hostile-code sandbox. The permission model governs manifests and the framework-mediated services supplied to trusted executors; installation/signature/trusted-publisher provenance is strengthened further by the signed-pack/catalog milestone.

## Plugin manifest

Repository-native location:

```text
.sdai/plugin-steps/<id>.yaml
```

The compatibility extension location is also reserved:

```text
.sdai/extensions/plugin-steps/<id>.yaml
```

If both locations define the same ID, loading fails closed.

Example:

```yaml
apiVersion: sdai/v1
kind: PluginStep
metadata:
  id: trivy-adapter
  version: 1.0.0
  description: Trusted Trivy adapter
spec:
  publisher: company-security
  executor: company-trivy
  permissions:
    filesystem:
      read: [src, pom.xml]
      write: []
    network: false
    environment: []
    commands: [trivy]
    workspace_write: false
```

`publisher` and `executor` are IDs, not module/import paths.

## Plugin policy

Repository policy:

```text
.sdai/plugin-policy.yaml
```

Optional organization and user policy locations:

```text
SDAI_ORG_PLUGIN_POLICY_PATH
SDAI_USER_PLUGIN_POLICY_PATH
```

Organization/user environment paths must be absolute regular non-symlink files.

Example:

```yaml
version: 1
allowed_plugins: [trivy-adapter]
denied_plugins: []
trusted_publishers: [company-security]
permissions:
  filesystem:
    read: [src, pom.xml]
    write: []
  network: false
  environment: []
  commands: [trivy]
  workspace_write: false
```

Resolution order is:

```text
organization → repository → user
```

Security merge semantics are intentionally one-way:

- `denied_plugins` = union
- `allowed_plugins` = intersection where declared
- `trusted_publishers` = intersection where declared
- filesystem read/write allowlists = intersection where declared
- environment allowlists = intersection where declared
- command allowlists = intersection where declared
- `workspace_write` = logical AND

A lower layer can narrow permissions; it cannot restore a plugin, publisher, command, environment variable, filesystem scope, or write capability denied by organization policy.

With no plugin-policy files, only publisher `sdai` is trusted and all external command/environment/filesystem-write permissions remain unavailable.

## Network permission

`network: true` is rejected in v1.

SDAI does not currently have one cross-platform OS network sandbox with equivalent Linux/Windows guarantees. Recording `network: false` in a manifest without a real enforcement boundary would be misleading if SDAI then allowed untrusted plugin code. Therefore v1 exposes **no network service** and refuses manifests that request network authority.

A later execution backend may add enforceable network capability under an isolated container/sandbox contract. Until then SDAI fails closed.

## Filesystem services

A trusted executor receives a `PluginExecutionServices` object instead of raw framework mutation APIs.

Available operations include:

```python
services.read_text("src/file.java")
services.write_text("generated/report.txt", "...")
services.getenv("MY_ALLOWED_VARIABLE")
services.run_argv("trivy", ["fs", "."])
```

Each operation must be declared by the manifest and allowed by effective policy.

Paths are portable repository-relative POSIX paths. SDAI rejects absolute paths, drive-letter paths, backslashes, parent/dot traversal, invalid Windows path names, control characters, and symlink write targets.

Framework-mediated writes additionally reject protected paths including:

```text
.git/**
.sdai/**
.agents/**
.codex/**
.claude/**
.gemini/**
.github/workflows/**
.github/agents/**
specs/**
CODEOWNERS
```

This prevents a plugin service call from rewriting canonical specifications, approvals/state, workflow/policy configuration, provider-native agent definitions, CI controls, or Git metadata.

## Safe argv

`run_argv(executable, argv)` is deliberately not a shell API.

Rules:

- executable is a bare policy-approved executable name
- executable must be declared by the plugin manifest
- argv is a list/tuple of literal strings
- NUL/newline/runtime-template syntax is rejected
- executable is resolved separately
- the child environment contains only explicitly allowed variables plus Windows process essentials when required
- `shell=False` is unconditional
- stdout/stderr are captured
- the framework does not concatenate a command string

The executable itself is a trusted organization-approved tool. Command permission is not equivalent to sandboxing a malicious executable; trusted publisher/tool governance is a prerequisite.

## Environment

Executors cannot ask the framework for arbitrary inherited environment variables. `getenv(name)` succeeds only for a name requested by the manifest and retained by effective policy. The same filtered set is supplied to framework-mediated argv execution.

## Execution contract

```python
plan = prepare_plugin_step(...)
plan, result = execute_plugin_step(...)
```

`prepare_plugin_step()` is deterministic and side-effect free. It validates:

- manifest shape/version/identity
- publisher/executor IDs
- requested permissions
- effective allow/deny policy
- trusted publisher
- workspace-write/filesystem/environment/command subsets
- JSON-compatible inputs with no runtime template syntax

`execute_plugin_step(..., dry_run=True)` returns the validated plan without requiring a registered executor and without side effects.

Real execution requires the manifest's executor ID to be present in `PluginExecutorRegistry`. The manifest cannot register it.

Executors return:

```python
PluginResult(
    status="passed" | "failed",
    summary="...",
    findings=(PluginFinding(...),),
    data={...},
)
```

Invalid executor return types/statuses fail closed.

## Error families

| Code | Meaning |
|---|---|
| `SDAI-PLUGIN-001` | malformed manifest/path/input/permission contract |
| `SDAI-PLUGIN-002` | malformed/unsafe plugin policy source |
| `SDAI-PLUGIN-003` | effective policy/trusted-publisher denial |
| `SDAI-PLUGIN-004` | permission cannot be enforced by v1 (currently network) |
| `SDAI-PLUGIN-005` | runtime service permission/protected-path/safe-argv violation |
| `SDAI-PLUGIN-006` | trusted executor registration/result contract failure |

## Next #77 slice

After this contract is merged, the next focused PR will:

1. add `PluginStep` to the shared extension/scaffolding API,
2. add workflow `type: plugin` parsing,
3. validate/prepare plugin steps during workflow dry-run,
4. execute only through this trusted registry/services boundary,
5. persist structured result/provenance as framework evidence,
6. prove org deny cannot be bypassed through workflow components/overlays,
7. retain the no-shell/no-network/protected-path guarantees.
