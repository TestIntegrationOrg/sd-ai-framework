# SDAI Integration Execution v1

SDAI 0.13 executes a resolved declarative Integration through versioned, provider-neutral contracts rather than constructing tool-specific shell commands.

The execution APIs are:

```text
sdai.integration-execution-request/v1
sdai.integration-execution-plan/v1
sdai.integration-execution-result/v1
sdai.integration-execution-error/v1
```

## Security boundary

A manifest describes requirements; it never grants them. `build_integration_execution_plan(...)` consumes the current `ResolvedIntegration`, a request, and the current `EffectiveConfiguration`.

Planning fails when:

- the request identity/hash does not bind the resolved manifest,
- a non-executable Integration is invoked,
- `inputMode: none` receives input,
- the Integration requires workspace write while effective SDAI policy denies it,
- a required environment-variable name is outside the effective policy allowlist, or
- a declared runtime input/output file overlaps a protected SDAI/source-of-truth path.

Execution rechecks workspace-write and environment policy immediately before launch so a previously built plan cannot bypass a later policy tightening. `WorkspaceMutationGuard` remains active during the external process and restores/rejects changes to protected paths.

## No shell interpolation

Execution always invokes an executable plus an argv array with `shell=False`.

The manifest already separates `argsBeforeInput`, dynamic input, and `argsAfterInput`. For `inputMode: argument`, the complete user input is inserted as exactly **one argv element**. Characters such as `;`, `&&`, `|`, `$()`, quotes, redirects, and spaces therefore remain data rather than shell syntax.

For `inputMode: stdin`, the UTF-8 input is sent over stdin. For `inputMode: file`, SDAI creates an exclusive project-relative runtime file and inserts only its declared relative path at the dynamic-input argv position. `inputMode: none` adds no dynamic argument.

## Explainability without input/secret leakage

The in-memory request retains the runtime input required to execute the process, but canonical request and plan JSON contain only:

- input byte count,
- input SHA-256,
- Integration identity/version,
- manifest SHA-256,
- executable/static argv structure,
- input/output modes and declared paths,
- timeout,
- required environment-variable **names**, and
- declared network/workspace requirements.

Raw user input is not serialized into request/plan JSON. Environment values are runtime-only and are never included as execution metadata. The execution plan SHA-256 therefore binds the exact input by hash without exposing it.

SDAI uses the existing minimal provider environment builder. Only the minimal OS/runtime base plus explicitly requested names permitted by effective policy are forwarded; the full employee/process environment is not inherited.

## File I/O

Runtime input/output files must remain inside the project, may not traverse symlink ancestors, and may not overlap effective protected paths.

SDAI never overwrites a pre-existing runtime file. Input files use exclusive creation. Output-file mode requires the external process to create the declared output path as a regular file. Runtime input/output files created for one execution are cleaned after capture; user-owned pre-existing paths cause a deterministic I/O failure instead of being replaced.

Use an unprotected runtime namespace such as `.integration-runtime/...`; `.sdai/**`, `.agents/**`, `specs/**`, or other policy-protected paths are rejected when protected by the effective configuration.

## Output normalization

The v1 modes normalize as follows:

- `none` → `null` output.
- `stdout` → strict UTF-8 stdout text.
- `stderr` → strict UTF-8 stderr text.
- `json-stdout` → strict UTF-8, duplicate-key-free, finite JSON parsed into canonical machine data.
- `json-stderr` → the same JSON rules applied to stderr.
- `file` → strict UTF-8 contents of the declared regular output file.

Invalid UTF-8, malformed/non-finite/duplicate-key JSON, a missing file output, or a symlink/non-file output is normalized as `malformed-output` rather than being accepted as provider truth.

## Stable runtime states

`sdai.integration-execution-result/v1` uses stable status values:

- `succeeded`
- `exit-error`
- `timed-out`
- `cancelled`
- `launch-error`
- `malformed-output`
- `policy-violation`
- `io-error`

Every non-success result carries `sdai.integration-execution-error/v1`, a stable error code/category/message, plus the Integration identity, manifest hash, and exact execution-plan hash. Non-zero exit normalization intentionally does not copy arbitrary stderr into the structured error metadata.

Current error families are:

- `SDAI-INTEGRATION-EXEC-001` — request/manifest/plan binding or execution contract error.
- `SDAI-INTEGRATION-EXEC-002` — effective policy prevents planning/execution.
- `SDAI-INTEGRATION-EXEC-003` — runtime file/containment/symlink safety failure.
- `SDAI-INTEGRATION-EXEC-004` — process launch failure.
- `SDAI-INTEGRATION-EXEC-005` — timeout.
- `SDAI-INTEGRATION-EXEC-006` — cancellation.
- `SDAI-INTEGRATION-EXEC-007` — non-zero exit status.
- `SDAI-INTEGRATION-EXEC-008` — malformed/invalid output.
- `SDAI-INTEGRATION-EXEC-009` — protected-path mutation detected and restored.

## Extension-first rule

This engine consumes the generic Integration manifest and registry contracts. It contains no `if tool == ...` command construction. A new normal CLI/harness can therefore reuse the same planning, policy, argv, environment, cancellation, and result-normalization machinery by adding a manifest and catalog entry rather than a new subprocess adapter in SDAI core.
