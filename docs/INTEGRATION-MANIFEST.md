# SDAI Declarative Integration Manifest v1

SDAI 0.13 introduces a provider-neutral Integration manifest so normal agent/IDE/CLI integrations can be described as data rather than hard-coded core adapters.

The canonical API version is:

```text
sdai.integration-manifest/v1
```

This contract describes **what an integration needs and where it projects native files**. It does not grant permissions. SDAI execution policy remains authoritative and may deny any declared network, workspace-write, environment, or executable requirement.

## Example

```yaml
apiVersion: sdai.integration-manifest/v1
id: acme-agent
version: 1.2.3
displayName: Acme Agent
description: Acme agent CLI and native project integration.
capabilities:
  - agent-execution
  - agent-files
  - commands
  - skills
projections:
  - kind: agent-file
    source: .sdai/agents
    target: .acme/agents
  - kind: command
    source: .sdai/commands
    target: .acme/commands
  - kind: skill
    source: .agents/skills
    target: .acme/skills
execution:
  executable: acme-agent
  argsBeforeInput: [run, --mode, safe]
  inputMode: argument
  argsAfterInput: [--format, json]
  outputMode: json-stdout
  outputPath: null
  timeoutSeconds: 600
security:
  requiresNetwork: true
  requiresWorkspaceWrite: false
  environment:
    - ACME_API_KEY
    - HTTPS_PROXY
```

## Identity and canonical form

`id` is a portable lowercase identifier and `version` is SemVer. The stable identity is `id@version`.

SDAI normalizes human text and portable paths to Unicode NFC. Semantically unordered collections such as capabilities, projections, and environment-variable names are sorted deterministically. Argument arrays are **not** sorted because argument position is semantic.

Canonical serialization is finite UTF-8 JSON with sorted keys, no insignificant whitespace, and an optional trailing LF via `to_text()`. The manifest SHA-256 is computed from canonical JSON bytes and is formatted as `sha256:<lowercase hex>`.

Unknown fields, duplicate JSON/YAML keys, malformed SemVer, and ambiguous declarations fail closed.

## Capabilities

v1 supports these explicit capabilities:

- `agent-execution`
- `skills`
- `commands`
- `agent-files`

Projection capabilities require at least one corresponding projection. `agent-execution` and the `execution` object must either both be present or both be absent. This prevents a manifest from claiming support that its declaration cannot provide.

## Native projections

Each projection contains exactly:

```yaml
kind: skill | command | agent-file
source: project/relative/source
 target: project/relative/target
```

`source` identifies the SDAI/canonical project-relative input and `target` identifies the tool-native project-relative destination. #152 owns materialization; v1 only defines the deterministic contract.

Paths use `/` separators and must be portable across Windows and POSIX. Absolute paths, drive-qualified paths, `.`/`..`, empty segments, backslashes, control characters, Windows-reserved device names, and non-portable Windows filename characters are rejected. Projection targets must be unique and must not overlap by ancestry, preventing two declarations from claiming the same managed namespace.

## Safe execution declaration

The execution object is structured; it is not a shell command string:

- `executable` — one portable executable name or project-relative executable path.
- `argsBeforeInput` — literal argv tokens before dynamic input.
- `inputMode` — `none`, `stdin`, or `argument`.
- `argsAfterInput` — literal argv tokens after dynamic input.
- `outputMode` — `none`, `stdout`, `json-stdout`, or `file`.
- `outputPath` — required safe project-relative path only for `file`; otherwise `null`.
- `timeoutSeconds` — integer from 1 through 86400.

Dynamic prompt/input placement is structural. Static argv tokens may not contain the legacy `{prompt}` replacement marker. Future execution planning inserts dynamic input as a single argv element when `inputMode: argument`, or sends it as UTF-8 stdin when `inputMode: stdin`. SDAI never needs to concatenate the manifest into a shell string.

Shell metacharacters inside a literal argv token remain literal data under this contract; #151 is responsible for executing the plan with direct executable+argv subprocess semantics and applying provider/execution policy.

## Security declaration

`security` contains requirements, not grants:

- `requiresNetwork` — whether the integration requires network access.
- `requiresWorkspaceWrite` — whether the integration process requires project writes.
- `environment` — uppercase portable environment-variable **names only**.

Secret values are never part of the manifest or its explainable canonical form. Later policy resolution may further restrict the environment names and declared privileges. Repository/user configuration cannot use a manifest to weaken organization execution policy.

`outputMode: file` requires `requiresWorkspaceWrite: true` because the declared output is project-relative.

## Error contract

v1 uses stable error families:

- `SDAI-INTEGRATION-001` — schema, identity, version, enum, or canonical-form error.
- `SDAI-INTEGRATION-002` — path/projection safety error.
- `SDAI-INTEGRATION-003` — executable/argv/input/output execution declaration error.
- `SDAI-INTEGRATION-004` — security declaration error.

These codes are intended for automation; human detail may become more specific without changing the error family.

## Extension-author invariants

An Integration author must be able to add a normal tool by providing a manifest, later registry packaging/configuration, tests, and documentation—not by adding `if integration == ...` branches to SDAI core.

The manifest must remain provider-neutral. It may describe a native tool and its requirements, but semantic SDAI agent roles, enterprise policy, approvals, evidence, and governance remain independent framework concepts.
