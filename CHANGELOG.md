# Changelog

All notable SD-AI Framework changes should be recorded here. The project version is controlled by `src/sdai/__init__.py::__version__`; roadmap milestones do not imply that a package release has already been published.

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
