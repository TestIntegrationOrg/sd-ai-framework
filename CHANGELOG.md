# Changelog

All notable SD-AI Framework changes should be recorded here. The project version is controlled by `src/sdai/__init__.py::__version__`; roadmap milestones do not imply that a package release has already been published.

## Unreleased — 0.14 Workflow Engine 2 hardening

### Added
- Canonical nested Workflow Engine 2 overlay edits with `insert-before`, `insert-after`, `replace`, and `remove`, legacy operation compatibility, path/ambiguity handling, transactional source-order-independent application, and operation-level pre/post graph provenance.
- Bounded Workflow Engine 2 execution for sequence, conditional, switch, parallel, fan-out, fan-in, foreach, and bounded-while nodes, with deterministic branch/item/iteration identities and source-ordered aggregation.
- Durable workflow leaf checkpoints on the existing 0.9 execution ledger, including evidence-bound completion, crash-safe dispatch reuse, approval pause/resume, bounded retry history, cancellation, and stale-plan invalidation.
- Versioned `workflow graph`, `workflow resolve`, `workflow validate`, `workflow status`, and `workflow resume` library/CLI surfaces with canonical JSON, stable automation exit classes, effective leaf permission plans, output-redacted durable status, and explainable next work.
- Layered Workflow Engine 2 PluginStep registration across core, verified installed Packs, organization, repository, and user sources, with deterministic SemVer resolution, provenance, authoritative locks, canonical manifest hashes, and an extension-first built-in sample.
- Trusted PluginStep execution adapter for bounded control-flow leaves, binding durable task identity to the exact manifest, executor, publisher, private input hash, effective permissions, and policy sources so changed plans cannot reuse stale completion.

### Security
- Enforce `requiresWorkspaceWrite: false` and `workspace_write: false` as a read-only boundary across the complete project workspace for Integration and safe-command execution, restoring created, modified, deleted, directory, and symlink mutations before returning a stable policy violation.
- Prevent lower overlays from indirectly removing nested organization/core gates and from adding workspace-writing branches beneath concurrent control nodes.
- Reject concurrent Workflow Engine 2 subtrees that can write the workspace until an explicit governed write-permit strategy exists; all runtime item, iteration, and concurrency bounds fail closed.
- Keep manifest discovery separate from executable registration: PluginStep YAML cannot import code, Pack bytes must match managed install evidence, core/org locks cannot be bypassed, and lower policies cannot widen network, environment, filesystem, command, or workspace-write permissions.

## Unreleased — 0.10 traceability graph + typed evidence

### Added
- Canonical provider-independent `sdai.trace-graph/v1` model with typed requirement/scenario/RFC/ADR/component/contract/threat/task/code/test/approval/evidence nodes, typed relationships, deterministic identities, and source:line provenance.
- Canonical `sdai.trace-evidence/v1` records for execution/test/quality/security/approval/review/operational proof, separating provider/model producer metadata from evidence truth hashes.
- Read-only graph construction across specification artifacts, architecture, contracts, tasks, repository code/tests, threats, approvals, and typed evidence, with unresolved/ambiguous relationships preserved as deterministic gaps rather than guessed links.
- Evidence freshness evaluation bound to current Git reachability and SHA-256 content bindings, including integration with 0.8 artifact freshness and durable 0.9 evidence bindings.
- Read-only `sdai trace`, `trace requirement`, `trace missing`, `trace coverage`, and exact canonical `trace export --format json` commands with stable human/JSON output and CI exit semantics.
- Risk-based `sdai trace policy` gates across current requirement proof, task linkage, code linkage, test verification, security evidence, and approval evidence.
- Built-in → organization → repository → user trace-policy layering with monotonic non-weakening thresholds and effective-threshold provenance.

### Security, compatibility, and hardening
- Stale, missing, blocked, failed, disconnected-history, changed-source, changed-test, and changed-contract proof cannot satisfy current trace coverage.
- Critical and regulated policies require 100% coverage across all six trace dimensions by default; organization minima cannot be weakened by repository or user policy.
- Canonical graph truth remains unchanged when only evidence producer/provider/model metadata changes.
- Repository source identities remain ASCII-stable while UTF-8 paths such as `café`, `Ω`, and `Δ` remain readable in provenance and metadata across Windows/Linux.
- Trace inspection, requirement queries, missing-link queries, coverage, policy evaluation, and export remain byte-for-byte read-only against repository artifacts.
- Existing 0.6, 0.7, 0.8, and 0.9 compatibility gates remain enabled and part of the full 0.10 regression gate.

### Release gate
The 0.10 implementation slice is considered complete only when the entire repository suite passes on the exact candidate head on Ubuntu and Windows for Python 3.11 and 3.12, including `tests/test_v06_release_compatibility.py`, `tests/test_v07_release_compatibility.py`, `tests/test_v08_release_compatibility.py`, `tests/test_v09_release_compatibility.py`, and `tests/test_v010_release_compatibility.py`, with no unresolved actionable review finding. `docs/V010-RELEASE-READINESS.md` records the integrated acceptance evidence. Package/tag publication remains a separate intentional release action.

## Unreleased — 0.9 analysis + durable execution truth

### Added
- Provider-independent `sdai.analysis-index/v1` facts and `sdai.findings/v1` deterministic cross-artifact analysis across requirements, architecture/ADR, contracts, tasks, tests, threats, approvals, and artifact freshness.
- Read-only `sdai analyze FEATURE [--risk ...] [--json]` with source/line evidence and CI-stable exit semantics.
- Append-safe durable execution ledger with canonical hash-chained JSONL events, strict task/run transitions, atomic task/evidence/checkpoint records, crash-safe advisory locking, and current Git/artifact SHA-256 completion bindings.
- Exact `sdai execution status` / `sdai execution resume` semantics that use original task registration order, current Git/evidence identity, compare-and-append reservations, and durable dispatch idempotency tokens instead of chat/model memory.
- Provider-neutral `debugger` semantic agent, strengthened `systematic-debugging` behavioral evals, and deterministic `sdai.debug-record/v1` root-cause evidence.
- Generic required-completion-evidence declarations so a task can require a completion-ready evidence contract from the current attempt before `task.completed` is legal.

### Security, compatibility, and hardening
- Analysis is byte-for-byte read-only; duplicate/conflicting facts remain inspectable and deterministic finding rules do not mutate source-of-truth artifacts.
- Ledger corruption, truncation, sequence/hash mismatch, invalid transitions, forged completion bindings, stale checkpoints, and conflicting terminal events fail closed.
- Resume skips completed work only while the recorded completion commit remains reachable from current `HEAD` and all bound artifact/evidence bytes still match; dirty engineering workspaces block resume.
- Interrupted started tasks reuse their existing dispatch token; competing resume writers use compare-and-append semantics rather than creating independent reservations.
- Debugger completion requires confirmed root cause, supported hypothesis/experiment evidence, a recorded fix, and passing regression evidence; previous-attempt or non-ready evidence cannot authorize completion.
- Semantic debugger identity and evidence schema remain independent from provider/model choice, while existing developer/tester systematic-debugging compatibility is preserved.
- Windows/Linux and UTF-8 behavior is covered by real Git workspaces with spaces, `Ω`, `Δ`, and `café`.
- Existing 0.6, 0.7, and 0.8 release compatibility gates remain enabled and part of the full 0.9 regression gate.

### Release gate
The 0.9 implementation slice is considered complete only when the entire repository suite passes on Ubuntu and Windows for Python 3.11 and 3.12, including `tests/test_v06_release_compatibility.py`, `tests/test_v07_release_compatibility.py`, `tests/test_v08_release_compatibility.py`, and `tests/test_v09_release_compatibility.py`, with no unresolved actionable review finding on the exact merge head. Package/tag publication remains a separate intentional release action.

## Unreleased — 0.8 artifact graph + workflow composition + isolation

### Added
- File-driven `sdai/v1` ArtifactSchema registry with deterministic built-in → organization → repository → user layering, provenance, required/locked controls, dependency mandates, topological ordering, and cycle/missing-edge validation.
- Hash-bound artifact lifecycle state with `fresh`, `stale`, `missing`, and `blocked` states, transitive dependency invalidation, and approval/validation/verification evidence bindings.
- Reusable WorkflowComponent manifests with typed/default/enum/sensitive inputs, deterministic interpolation, component dependency validation, redacted provenance, and v5 compatibility.
- Workflow inheritance plus organization → repository → user overlays and safe lifecycle hooks, with organization-mandated step/hook non-weakening.
- Strict PluginStep permission SDK and workflow v8 integration through trusted registered executors only, including layered policy denial, filesystem/environment/command permissions, structured results/evidence, retry handling, and existing workspace-write approval controls.
- Git worktree execution mode via `sdai run --isolation worktree`, with verified clean baseline commit/tree evidence, dedicated isolated branches, conservative cleanup, and preservation of dirty implementation work.

### Security, compatibility, and hardening
- Organization artifact requirements/dependency edges and organization workflow controls cannot be weakened by repository/user overlays.
- Artifact state records are domain-safe and collision-resistant; malformed dependency/evidence paths fail closed.
- Lifecycle hooks remain advisory/gate/validation-only; provider/shell execution fields are not accepted through overlay configuration.
- Plugin YAML cannot import executable code or introduce a generic shell primitive. Network access fails closed in the cross-platform v1 permission contract.
- Plugin protected-path checks cover symlink resolution, case-insensitive protected namespaces, CODEOWNERS locations, and trusted executable search roots instead of ambient/workspace `PATH`.
- Plugin steps expanded from reusable components remain subject to workflow version and organization permission policy; overlays/hooks remain plugin-free in v1.
- Worktree mode rejects dirty or detached source baselines, strips dangerous Git environment overrides, and records evidence outside tracked source files.
- Worktree creation verifies a detached copy of the exact source commit/tree before creating its dedicated branch; rollback cannot delete a pre-existing branch collision.
- Existing v0.5/v0.6/v0.7 workflow, provider, specification, extension, UTF-8, and upgrade compatibility tests remain part of the full regression gate.

### Release gate
The 0.8 implementation slice is considered complete only when the entire repository suite passes on Ubuntu and Windows for Python 3.11 and 3.12, including `tests/test_v06_release_compatibility.py`, `tests/test_v07_release_compatibility.py`, and `tests/test_v08_release_compatibility.py`, with no unresolved actionable review finding on the exact merge head. Package/tag publication remains a separate intentional release action.

## Unreleased — 0.7 current truth + technology skills

### Added
- Current specification truth under `specs/current/<domain>/specification.md` plus typed per-feature delta changes with `ADDED`, `MODIFIED`, `REMOVED`, and `RENAMED` operations.
- Deterministic whole-spec and per-requirement SHA-256 baseline validation, stale-change findings, and parallel overlapping-change detection.
- Governed current-truth promotion with semantic diff, read-only dry-run, change/current-hash-bound approval, immediate pre-image checks, multi-domain rollback, promotion evidence, and archive history under `specs/archive/changes/**`.
- Provider-independent `sdai tech detect` with evidence for Java/Maven/Gradle, C#/.NET, Python, JavaScript/TypeScript/Node, Go, Rust, PowerShell, Docker, Terraform, frameworks, libraries, platforms, and testing signals.
- Explicit `.sdai/technology.yaml` declarations/version pins with conservative ambiguity/conflict findings.
- Compatibility-aware `sdai skill resolve` with semantic-agent, policy, explicit-request, dependency, role/capability, task/domain, and technology/version inputs plus stable explainability JSON.
- Resolver-ready skill scaffolding with optional `compatible_agents`, `requires`, `compatibility`, and `selection` metadata while preserving existing `version: 1` skill sidecars.
- Six built-in Tier-1 language packs for Java, .NET, Python, JavaScript/TypeScript, Go, and PowerShell, with separate framework skills for Spring Boot, ASP.NET Core, FastAPI, Django, Node.js, React, and Angular.
- Deterministic Tier-1 pack integrity validation and behavioral eval skeletons for every shipped language/framework skill.
- Provider-neutral execution-excellence pack with strengthened implementation planning, test-driven development, systematic debugging, and verification-before-completion disciplines, behavioral evals, and valid current-v5 workflow/policy examples.

### Compatibility and hardening
- Semantic role identity remains independent from language/framework/platform/library context and from provider selection; no `java-developer`, `dotnet-architect`, `codex-java-*`, or equivalent role proliferation was introduced.
- Existing provider routing/manual-step override behavior remains supported; semantic agent and provider profile can still be selected independently.
- Skill version compatibility fails conservatively when exact evidence cannot be proven; weak dependency bounds and ambiguous versions are not silently treated as installed versions.
- Task-keyword auto-selection now uses Unicode casefolded token/phrase boundaries, preventing arbitrary substring matches such as `bug` inside `debug`.
- Existing v0.5/v0.6 customized canonical agents and legacy `.sdai/skills` remain upgrade-compatible.
- Upgrade preserves new user-owned 0.7 `.sdai/technology.yaml` and `specs/current/**` content.
- Windows/Linux and UTF-8 behavior is covered by integrated workspaces and content containing spaces, `Ω`, `Δ`, and `café`.
- Existing 0.6 release compatibility evidence remains enabled and part of the full 0.7 regression gate.

### Release gate
The 0.7 implementation slice is considered complete only when the entire repository suite passes on Ubuntu and Windows for Python 3.11 and 3.12, including both `tests/test_v06_release_compatibility.py` and `tests/test_v07_release_compatibility.py`, with no unresolved actionable review finding on the exact merge head. Package/tag publication remains a separate intentional release action.

## Unreleased — 0.6 foundation

### Added
- Versioned `sdai/v1` extension manifest model for skills, agents, workflows, workflow components, validators, quality gates, integrations, and packs.
- Layered extension registry with deterministic precedence, provenance, authoritative organization locks, and fail-closed override handling.
- Registry-backed compatibility adapters for existing semantic agent and skill file formats.
- Extension scaffolding and deterministic validation commands.
- Engineering constitution with hash-bound reviewer evidence.
- Requirements clarification and `RQ-001`–`RQ-014` deterministic requirements-quality checks.
- Behavioral skill/agent evaluation with baseline-vs-candidate evidence, must/must-not assertions, regression gates, provider/model identity, attached-skill composition, and CI-safe hashed output evidence.
- Deterministic release/version metadata validation and `sdai --version`.
- Managed `.sdai/framework-version.yaml` generation on init/upgrade.

### Compatibility and hardening
- Existing plural `sdai agents` / `sdai skills` command namespaces remain supported alongside new extension/eval commands.
- Existing v0.5 canonical agent/skill layouts and legacy `.sdai/skills` fallback remain supported.
- Team-customized scaffold assets remain protected from stock upgrade replacement.
- Windows/Linux path rendering remains portable for new extension authoring output.
- Explicit UTF-8 behavior is validated through non-ASCII repository content and paths.
- Existing enterprise workflow and manual nested-step dry-run behavior remains part of the release gate.

### Release gate
The 0.6 foundation is considered implementation-complete only when the full repository test suite passes on Ubuntu and Windows for Python 3.11 and 3.12, including `tests/test_v06_release_compatibility.py`.
