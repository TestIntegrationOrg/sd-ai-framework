# SDAI 0.9 — Analysis + Durable Execution Truth Release Readiness

This document is the acceptance map for issue #95 and the 0.9 parent (#16). It records the evidence required before the 0.9 implementation slice is considered complete. It is **not** a statement that a package, tag, or `0.9.0` release has already been published.

## Release posture

- Package version remains whatever `src/sdai/__init__.py::__version__` declares until an intentional release cut.
- Roadmap implementation completion does not implicitly modify package metadata, tag Git, publish PyPI artifacts, or create a GitHub release.
- The version synchronization guard introduced in 0.6 remains authoritative for a future deliberate version change.
- The full 0.6, 0.7, and 0.8 compatibility suites remain enabled and are part of the 0.9 full regression gate.

## 0.9 capability chain

1. **#89 — Versioned cross-artifact fact/index contract**
   - provider-independent `sdai.analysis-index/v1`
   - normalized entities, relationships, source/line evidence, ArtifactSchema DAG facts, and stable hashes
   - duplicate declarations remain facts rather than being silently collapsed
   - read-only index construction with UTF-8/path containment and symlink fail-closed behavior
2. **#90 — Deterministic cross-artifact finding rules**
   - `sdai.findings/v1`
   - required families: orphan requirement/task, missing NFR, architecture/contract conflict, unresolved ADR, untested scenario, unapproved breaking change, unmitigated threat, and stale artifact
   - deterministic severities/evidence, duplicate suppression, and clean-feature zero-finding behavior
3. **#91 — Read-only `sdai analyze` CLI**
   - human and machine-clean JSON output
   - exit `0` when no blocking finding, `2` when analysis completes with blocking findings, `1` for input/configuration errors
   - no source or workspace mutation
4. **#92 — Append-safe durable execution ledger**
   - provider/chat-independent run/task identity
   - hash-chained canonical JSONL events with monotonic sequence and strict transitions
   - atomic task/evidence/checkpoint records, crash-safe advisory locking, corruption/truncation detection, and current-byte completion bindings
   - terminal truth reconstructable after process restart
5. **#93 — Exact task resume semantics**
   - original task registration order is authoritative
   - completed tasks skip only when recorded commit remains reachable from current HEAD and every bound evidence/artifact hash still matches
   - dirty engineering workspace blocks resume
   - durable `task.reopened` and `task.dispatch_reserved` events
   - compare-and-append prevents competing processes from independently reserving the same resume task
   - interrupted attempts reuse a durable dispatch idempotency token
6. **#94 — Debugger semantic agent + root-cause evidence**
   - provider-neutral semantic `debugger` role using existing coding/testing/review capabilities
   - systematic-debugging pressure evals
   - deterministic `sdai.debug-record/v1`
   - confirmed root cause, supported hypothesis/experiment, fix, and passing regression evidence required before debugger completion
   - generic `required_completion_evidence` ledger mechanism prevents stale/previous-attempt/non-ready evidence from authorizing completion

## Compatibility acceptance matrix

| Area | Required evidence |
|---|---|
| Full regression | Entire `pytest -q` suite succeeds on Ubuntu/Windows × Python 3.11/3.12 on the exact latest merge head. |
| Existing release gates | `tests/test_v06_release_compatibility.py`, `tests/test_v07_release_compatibility.py`, and `tests/test_v08_release_compatibility.py` remain enabled and green. |
| Analysis completeness | Integrated inconsistent feature surfaces all required 0.9 finding families with source/line evidence. |
| Analysis read-only | `sdai analyze --json` leaves every pre-existing workspace file byte-for-byte unchanged. |
| Durable task truth | Completion is reconstructed from the ledger and current evidence, not provider/chat memory. |
| Exact resume | Multi-task interrupted run resumes the first non-skippable task in registration order. |
| Duplicate dispatch prevention | An interrupted started task reuses its existing dispatch reservation; later tasks are not pre-dispatched. |
| Evidence invalidation | Changing a bound artifact after completion invalidates skip even when the recorded completion commit remains an ancestor of current HEAD. |
| Ledger corruption | Truncated/corrupt JSONL fails closed and cannot reconstruct a task as completed. |
| Debugger evidence | Raw debugger completion is rejected; confirmed root-cause + fix + passing regression evidence is durably persisted before terminal completion. |
| Provider neutrality | Debugger semantic role and evidence schema do not depend on provider/model choice. |
| Windows/Linux + UTF-8 | Integrated Git journeys use spaces, `Ω`, `Δ`, and `café`; the same tests execute in the cross-platform CI matrix. |

Primary integrated evidence: `tests/test_v09_release_compatibility.py`.

## Trust boundaries validated

### Cross-artifact analysis

```text
requirements / architecture / contracts / tasks / tests / threats / approvals
                              ↓
                  read-only normalized fact index
                              ↓
                    deterministic rule engine
                              ↓
                      sdai.findings/v1
```

AI providers may help author or interpret artifacts; they do not decide deterministic finding identity, severity, source evidence, or whether analysis mutated the workspace.

### Durable execution and resume

```text
run manifest + hash-chained events + task/evidence files + Git identity
                              ↓
                     deterministic reconstruction
                              ↓
              current commit/artifact/evidence verification
                              ↓
                 exact first non-skippable task
                              ↓
             compare-and-append dispatch reservation
```

Chat history and provider assertions are not completion truth. A task is skipped only from durable, current evidence. Corruption, stale evidence, or an unsafe dirty workspace blocks or invalidates the skip decision rather than silently continuing.

### Debugger completion

```text
reproduction + observations + hypotheses + experiments
                              ↓
                 confirmed causal root cause
                              ↓
                  fix + regression evidence
                              ↓
                   sdai.debug-record/v1
                              ↓
          generic required-completion-evidence gate
                              ↓
                     task.completed
```

The provider proposes and records the investigation. Deterministic SDAI validates cross references, completion readiness, current attempt identity, durable hash bindings, and terminal authorization.

## Merge criteria

The final #95 PR must not merge unless:

- its exact latest head is mergeable;
- no unresolved actionable review finding remains;
- all four CI matrix jobs pass on the exact latest head;
- the entire repository test suite runs with no 0.6/0.7/0.8 compatibility test disabled or weakened;
- `tests/test_v09_release_compatibility.py` passes on Windows and Linux;
- the integrated analyzer journey remains byte-for-byte read-only;
- durable resume truth comes only from ledger/Git/current evidence and does not redispatch an already reserved interrupted attempt;
- any defect found by the integrated gate is fixed in the underlying runtime rather than hidden by weakening release evidence;
- the final release-gate PR remains tests/docs/changelog only unless a real blocker requires a separately reviewed runtime correction.

## After this gate

Once #95 merges cleanly and parent acceptance criteria are verified, issue #16 can close as **0.9 implementation-complete**. Publishing/tagging a package remains a separate intentional release operation.

The next roadmap slice is 0.10 traceability: a durable requirement ↔ scenario ↔ architecture/ADR ↔ contract ↔ task ↔ source ↔ test ↔ threat/approval graph with deterministic coverage and explainability.
