# SDAI 1.0 Platform Confidence

SDAI 1.0 treats Windows, Linux, and macOS behavior as a release contract rather than an informal developer assumption. The primary CI workflow executes the same unfiltered test suite on every supported operating-system and Python combination.

## Required CI matrix

The release-confidence matrix is:

| Operating system | Python 3.11 | Python 3.12 |
| --- | --- | --- |
| Ubuntu (`ubuntu-latest`) | required | required |
| Windows (`windows-latest`) | required | required |
| macOS (`macos-latest`) | required | required |

`.github/workflows/ci.yml` keeps `fail-fast: false` so one platform failure does not hide results from the remaining legs. Every matrix job installs the normal development dependencies and executes the unfiltered `pytest -q` suite.

The Ubuntu/Windows legs are historical release gates and remain mandatory. macOS is additive; it does not replace or weaken an existing gate.

## Evidence protocol

A cross-platform release claim applies only to the immutable SHA that actually produced the evidence.

1. Freeze the pull-request head.
2. Require all six PR-head matrix jobs to pass.
3. Review the exact frozen-head diff.
4. Squash-merge using the expected frozen head SHA.
5. Validate the exact squash-merged `main` SHA through the `push: main` CI run.
6. Treat PR-head and merged-main evidence as distinct; a green run for one SHA is not evidence for another SHA.

Later release-evidence documents may record concrete run/job identifiers, but this document defines the platform contract and is not itself executable authority.

## Platform-sensitive behavior covered by the suite

The normal test suite exercises the code paths most likely to vary by operating system:

- provider subprocess startup without `shell=True`;
- POSIX process-session/group termination used by Linux and macOS;
- Windows process-group creation and termination behavior;
- executable discovery through `PATH`;
- strict UTF-8 provider stdin/stdout/stderr handling;
- bounded provider output, cancellation, timeout, and pipe cleanup;
- temporary-directory and provider-environment construction;
- project-relative path containment and traversal rejection;
- protected-path snapshot/restore behavior;
- symlink escape and symlink-replacement defenses where the runner permits symlink creation;
- deterministic artifact/configuration behavior across native path separators.

The same test implementation runs on every matrix leg; SDAI does not maintain a reduced macOS or Windows test profile.

## OS-specific notes

### Linux and macOS

Both use the POSIX subprocess path. Provider processes start in a new session and termination targets the process group so child processes do not remain orphaned after cancellation or timeout.

Symlink-focused tests execute normally on hosted POSIX runners and verify containment/restoration behavior against real links.

### Windows

Windows uses a new process group when the Python runtime exposes `CREATE_NEW_PROCESS_GROUP`. Filesystem tests use native Windows paths. Some Windows environments may deny unprivileged symlink creation; individual symlink tests therefore skip only when the operating system refuses link creation. The rest of the containment, protected-path, subprocess, UTF-8, and environment suite still runs and remains release-blocking.

### Provider authentication and configuration

Platform confidence does not widen enterprise environment authority. The process-runtime baseline and policy-gated credential/configuration variables documented in `EXECUTION-SECURITY.md` apply identically on all three operating systems. Windows-specific discovery variables such as `USERPROFILE`, `APPDATA`, and `LOCALAPPDATA`, and POSIX discovery variables such as `HOME` and `XDG_CONFIG_HOME`, require the same effective-policy treatment.

## Limitations

The hosted CI matrix validates SDAI framework behavior on current GitHub-hosted Ubuntu, Windows, and macOS images. It does not certify every downstream provider CLI, shell, enterprise endpoint policy, filesystem driver, container runtime, or corporate credential store. Provider-specific binaries and organization-managed endpoint controls remain deployment responsibilities.

Identity-backed enterprise approvals (0.18/#25) remain held/deferred and are not part of this platform-confidence claim.
