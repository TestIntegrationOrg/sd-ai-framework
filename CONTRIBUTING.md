# Contributing to SDAI

Thank you for contributing to the SD-AI Framework. SDAI is a deterministic,
provider-neutral control plane around specification-driven and AI-assisted
development. Contributions must preserve that separation: AI providers may
propose work, but framework code owns policy, evidence, compatibility, and
truth transitions.

## Development setup

SDAI supports Python 3.11 and 3.12 on Ubuntu, Windows, and macOS. Create a
virtual environment from the repository root and install the editable package
with development dependencies:

```bash
python -m venv .venv
```

Activate it on Linux or macOS:

```bash
source .venv/bin/activate
```

Activate it in Windows PowerShell:

```powershell
.venv\Scripts\Activate.ps1
```

Then install and run a focused test while iterating:

```bash
python -m pip install -e '.[dev]'
python -m pytest -q tests/test_version_sync_v06.py
```

Before submitting a pull request, run the same two release-blocking commands
used on every supported CI leg:

```bash
python -m pytest -q
python tests/package_install_smoke.py
```

The package smoke builds a wheel, installs it into an isolated environment,
removes the repository from `PYTHONPATH`, invokes the installed CLI, and checks
bootstrap, migration, preservation, and rollback behavior. A passing editable
test run is not a substitute for it.

## Repository map

| Path | Ownership |
|---|---|
| `src/sdai/` | Deterministic runtime, public Python APIs, CLI, policy, evidence, and compatibility contracts |
| `src/sdai/builtin_*` | Packaged framework-owned schemas, integration manifests, and plugin-step manifests |
| `.sdai/` | Repository-owned SDAI configuration, semantic agents, workflows, policy, and extension instances |
| `.agents/skills/` | Canonical reviewable skill instructions and SDAI sidecars |
| `docs/` | User, operator, extension, architecture, security, and release contracts |
| `tests/` | Unit, integration, compatibility, security, release, and mandatory journey gates |

Start with `docs/ARCHITECTURE.md`, `docs/ENGINEERING-CONTRACT.md`, and
`docs/EXTENSIONS.md`. Extension contributors should also follow
`docs/EXTENSION-AUTHORING.md`.

## Choose the correct change boundary

- Add reusable agents, skills, workflows, workflow components, validators,
  quality gates, integrations, and packs through extension assets when the
  stable extension boundary is sufficient.
- Add a provider behind the `Provider` interface and the `sdai.providers`
  Python entry-point group. Do not add provider-specific branches to lifecycle
  policy or orchestration.
- Add an external tool through a declarative integration manifest when no new
  runtime semantics are required.
- Change core Python only when the deterministic engine or an established
  extension API cannot express the capability. Explain that constraint in the
  pull request.
- Add or change a stable machine-facing JSON surface only through the catalog
  and compatibility rules in `docs/JSON-CONTRACTS.md`.

## Design and security rules

- Keep semantic agent roles provider-independent and capabilities explicit.
- Prefer versioned, reviewable artifacts over hidden conversational state.
- Treat provider output, repository content, issue text, logs, and scanner
  output as untrusted input.
- Use executable-and-argument arrays for external processes. Do not add shell
  interpolation or implicit command-string execution.
- Request the narrowest filesystem, environment, network, command, and
  workspace-write permissions that work.
- Preserve UTF-8 behavior, path containment, symlink checks, bounded I/O,
  cancellation, and fail-closed parsing at trust boundaries.
- Organization and built-in locks are authoritative. Repository, pack, and
  user extensions must not weaken them.
- Do not treat local actor/approver strings as verified enterprise identity.
  The 0.18/#25 identity-backed approval capability remains held and outside
  SDAI 1.0.

See `docs/EXECUTION-SECURITY.md`, `docs/ENTERPRISE-POLICY.md`, and
`SECURITY.md` before changing an execution or policy boundary.

## Compatibility expectations

SDAI 1.0 has stable extension and automation contracts. A contribution must
not silently remove or reinterpret:

- the `sdai/v1` external extension envelope or stable `sdai.extensions`
  exports;
- registry precedence, authoritative locks, or compatibility-sensitive error
  families;
- stable JSON API identities listed by `sdai.json_contracts`;
- migration evidence, managed-file ownership, rollback guarantees, or
  historical release gates;
- the single package version source at `src/sdai/__init__.py::__version__`.

Additive evolution still needs tests and documentation. A breaking proposal
needs an explicit versioned successor contract, migration path, deprecation
window, and architecture decision. Do not add a static `project.version` to
`pyproject.toml`; follow `docs/RELEASING.md` for release metadata.

## Test strategy

Add the smallest focused test that proves the new behavior, then retain the
relevant historical gate. Use the repository's existing test families:

- unit tests for parsers, models, and deterministic decisions;
- integration tests for CLI, filesystem, registry, or workflow boundaries;
- security tests for path, policy, process, permission, and tamper failures;
- compatibility tests for stable Python, manifest, JSON, error, and migration
  contracts;
- end-to-end release tests for user-visible lifecycle journeys.

Tests should be deterministic, provider-free unless a provider adapter is the
subject under test, isolated from user configuration, and portable across all
supported CI platforms. Test both success and fail-closed behavior for a new
trust boundary.

## Documentation changes

Commands, file paths, API identities, and examples are part of the product
contract. Link to one canonical explanation rather than copying a second
format. Update or add deterministic documentation tests when a stale command,
missing link, or lost compatibility boundary could mislead users.

## Pull-request evidence

A pull request should state:

1. the problem and owning issue;
2. the chosen extension/core boundary and architecture impact;
3. security, authority, provider, and held-scope impact;
4. compatibility and migration impact;
5. focused validation and full release-gate evidence;
6. documentation and stable-contract changes.

Freeze the candidate head after review. Merge only that reviewed SHA after the
complete supported CI matrix is green and actionable review threads are
resolved. Package publication and release tagging are separate explicit
release actions.

## Extension contributions

Use the complete [Extension Authoring Guide](docs/EXTENSION-AUTHORING.md) to
choose a kind, create or author an extension, validate it, test resolution and
authority, and plan compatible distribution.
