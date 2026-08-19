# UTF-8 and Cross-Platform Provider Subprocess Hardening (0.20)

SDAI executes external provider CLIs through `CliProvider` with a binary, no-shell process boundary. 0.20.7 hardens that boundary so Windows/Linux host code pages, Unicode workspaces, large streams, timeout/cancellation, and malformed provider output cannot silently corrupt an agent invocation or consume unbounded memory.

## Binary UTF-8 boundary

Prompt/system content is composed as Python Unicode, encoded explicitly with UTF-8, and written to binary stdin. Provider stdout and stderr are read as bytes and decoded with strict UTF-8 only after the process terminates. SDAI never relies on the parent console encoding, Windows ACP/OEM code page, or the child locale for its own stdin/stdout/stderr conversion.

This supports Unicode feature/workspace paths and content such as accented text, CJK characters, and emoji consistently on Windows and Linux. A child process may run under an ASCII locale and still exchange SDAI payloads correctly when it uses its binary stdio boundary.

Invalid provider UTF-8 fails with `ProviderEncodingError`. The error identifies the stream and byte offset but exposes only the offending bytes, not adjacent provider content.

## Continuously drained bounded streams

Provider stdout and stderr are drained concurrently by dedicated binary reader threads while the main thread owns timeout, cancellation, heartbeat, and first-output diagnostic sequencing. Reader threads do **not** write provider diagnostics directly; they only drain bytes and set a first-output signal. This prevents diagnostic sequence races between first-output and heartbeat events.

Captured bytes are bounded independently:

- stdout default: 4 MiB, configurable up to 64 MiB;
- stderr default: 256 KiB, configurable up to 16 MiB;
- I/O read chunk default: 64 KiB, configurable up to 1 MiB.

The process pipe continues to be drained even after the capture buffer is full so a verbose child cannot deadlock on a full OS pipe. After termination, an exceeded bound fails explicitly with `ProviderOutputLimitError`; SDAI never returns a silently truncated model answer. Non-zero exit stderr shown in `ProviderExecutionError` is separately bounded to a short preview.

## Startup, execution, encoding, timeout and cancellation outcomes

The provider boundary distinguishes:

- `ProviderStartupError` — executable missing, permission denied, or process creation failure;
- `ProviderExecutionError` — provider process/pipe/non-zero/empty-output execution failure;
- `ProviderEncodingError` — non-UTF-8 stdout/stderr;
- `ProviderOutputLimitError` — stdout/stderr exceeded configured capture;
- `subprocess.TimeoutExpired` — configured execution timeout;
- `ProviderCancelledError` — caller/provider cooperative cancellation.

These distinctions are bounded metadata/error types; raw prompt/context/output is not copied into diagnostics.

## Process groups and termination

The managed process-control behavior introduced in 0.20.4 is retained:

- POSIX providers start in a new session/process group. Cancellation/timeout sends `SIGTERM`, then bounded `SIGKILL` escalation if needed.
- Windows providers start with `CREATE_NEW_PROCESS_GROUP` where available and use terminate/kill escalation.
- reader/writer threads are joined after process termination; a pipe that does not close cleanly is a provider execution failure rather than a silently abandoned background thread.

A pipe-thread failure causes SDAI to terminate the provider process immediately instead of waiting for the normal timeout.

## No shell interpolation

`subprocess.Popen` receives an executable + argument array with `shell=False`. Prompt text and profile `extra_args` are not interpreted by a command shell. Shell metacharacters in prompt content therefore remain literal data.

## Trusted-repository/provider-specific flags

SDAI does **not** automatically add provider-specific repository-trust bypasses. For example, a Codex option such as `--skip-git-repo-check` may be supplied only through the existing explicitly configured profile `extra_args` path after the repository owner has evaluated that provider-specific security tradeoff. Core subprocess hardening does not silently weaken a provider's trust model.

This preserves the extension/profile boundary and avoids coupling SDAI core to one vendor's temporary CLI behavior.

## Environment boundary

Normal provider execution inherits only the existing minimal environment plus provider authentication variables and explicitly policy/profile-allowlisted variables. SDAI does not copy the caller's complete secret-bearing environment into the child process.

## Retry and diagnostics interaction

0.20.5 retry remains above the single governed attempt boundary. Timeout and transient transport failures may retry only under explicit retry policy; cancellation, malformed/encoding/output-limit/local process failures remain fail-closed unless a later policy explicitly proves replay safe. Observability/audit persistence failure is never repaired by executing the provider again.

0.20.7 does not implement or depend on 0.18 Identity-Backed Enterprise Approvals.
