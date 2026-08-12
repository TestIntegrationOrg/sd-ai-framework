# Changelog

All notable SD-AI Framework changes should be recorded here. The project version is controlled by `src/sdai/__init__.py::__version__`; roadmap milestones do not imply that a package release has already been published.

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
