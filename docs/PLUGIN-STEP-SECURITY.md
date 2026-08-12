# Plugin Step Permission and Trusted-Executor Boundary

SDAI 0.8 introduces the security contract for genuinely new execution mechanisms. This is deliberately **not** a generic shell-step feature.

The first #77 slice establishes the manifest, policy, trusted-executor, permission, safe-argv, and structured-result APIs. Workflow/orchestrator integration is a separate focused slice so a new workflow step type is not enabled before this security contract has passed review.

## Trust model

```text
untrusted / repository-authored YAML manifest
                ↓
strict sdai/v1 PluginStep parser
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

A `PluginStep` manifest cannot import a Python module, name a Python callable, contain a shell command string, select `shell=True`, bypass the registered-executor registry, write protected source-of-truth paths through framework services, or request network access in v1.

Executor implementations and administrator-approved external executables are trusted installed code/tools. SDAI does not claim that in-process trusted Python or an approved executable is hostile-code sandboxed. The permission model governs manifests and the framework-mediated services supplied to those trusted executors.

## Plugin manifest

Repository-native location:

```text
.sdai/plugin-steps/<id>.yaml
```

Compatibility location:

```text
.sdai/extensions/plugin-steps/<id>.yaml
```

If both locations define the same ID, loading fails closed.

```yaml
apiVersion: sdai/v1
kind: PluginStep
metadata:
  id: trivy-adapter
  version: 1.0.0
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

`PluginStep` is a first-class shared `sdai/v1` extension kind. `publisher` and `executor` are IDs, not module/import paths.

## Plugin policy

Repository policy is `.sdai/plugin-policy.yaml`. Optional organization/user policy paths are supplied through `SDAI_ORG_PLUGIN_POLICY_PATH` and `SDAI_USER_PLUGIN_POLICY_PATH`; those paths must be absolute regular non-symlink files.

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

Resolution is organization → repository → user. Denies union; declared allowlists intersect; workspace-write permission is logical AND. A lower layer can narrow but cannot restore a plugin, publisher, path, environment variable, command, or write capability denied by a higher layer.

With no policy files, only publisher `sdai` is trusted and external command/environment/filesystem-write permissions remain unavailable.

## Network permission

`network: true` is rejected in v1. SDAI does not currently have an equivalent enforceable Windows/Linux network sandbox, so it does not advertise a permission it cannot enforce.

## Filesystem services

Trusted executors receive `PluginExecutionServices` rather than unrestricted framework mutation APIs:

```python
services.read_text("src/file.java")
services.write_text("generated/report.txt", "...")
services.getenv("MY_ALLOWED_VARIABLE")
services.run_argv("trivy", ["fs", "."])
```

Paths are portable repository-relative POSIX paths. Absolute/drive paths, backslashes, traversal, Windows-invalid names, control characters, and DOS device names are rejected.

For framework-mediated reads/writes, every existing path component is checked for symlinks. Writes additionally compare both the lexical path and resolved destination against protected paths, so an allowed path such as `generated/**` cannot be symlink-aliased into `specs/**` or another protected namespace.

Protected-path comparison is case-insensitive on every platform, preventing Windows aliases such as `.GIT`/`.SDAI`. Protected writes include:

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
.github/CODEOWNERS
docs/CODEOWNERS
```

## Safe argv and trusted executable discovery

`run_argv(executable, argv)` is not a shell API. The executable is a bare manifest+policy-approved name, argv is a literal string list, unsafe NUL/newline/template syntax is rejected, `shell=False` is unconditional, and stdout/stderr are captured.

Command approval by name is **not** resolved through the ambient host `PATH`. Administrators must provide:

```text
SDAI_PLUGIN_TRUSTED_COMMAND_PATH
```

as an OS-path-separator-delimited list of absolute existing non-symlink directories. Workspace-controlled directories are rejected. SDAI calls executable lookup only against those trusted directories; when the setting is absent, command execution fails closed. The setting is framework configuration and is not copied into the plugin child environment.

The child environment contains only manifest-requested variables retained by effective policy, plus Windows process essentials (`SYSTEMROOT`/`WINDIR`) when supplied by the host execution context.

## JSON/evidence contract

Plugin inputs and structured result data must be JSON-compatible. Non-finite numbers (`NaN`, `Infinity`, `-Infinity`) are rejected, and JSON serialization uses `allow_nan=False`. Runtime-template syntax is not accepted as an input interpolation mechanism.

## Execution contract

`prepare_plugin_step()` is deterministic and side-effect free. It validates manifest identity, publisher/executor IDs, requested permissions, effective allow/deny policy, trusted publisher, permission subsets, and JSON-compatible inputs.

`execute_plugin_step(..., dry_run=True)` returns the validated plan without requiring a registered executor or producing side effects. Real execution requires the executor ID to already exist in `PluginExecutorRegistry`; YAML cannot register executable code.

Executors return `PluginResult(status="passed" | "failed", ...)`. Invalid return types/statuses or non-JSON result data fail closed.

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

After this SDK contract is merged, the next focused PR will add workflow `type: plugin` parsing/execution through this boundary, validate/prepare plugin steps during dry-run, persist structured result/provenance evidence, and prove organization deny cannot be bypassed through workflow composition or overlays. No generic shell primitive will be introduced.
