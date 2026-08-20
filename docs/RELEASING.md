# Releasing SD-AI

SD-AI uses a single authoritative framework/package version:

```text
src/sdai/__init__.py::__version__
```

Do not add a second package version literal to `pyproject.toml`. Setuptools reads the project version dynamically from `sdai.__version__`.

Every stable release must also satisfy
`docs/COMPATIBILITY-AND-RELEASE-GOVERNANCE.md`. That policy defines the public
1.x surface, SemVer/deprecation/security-exception treatment, frozen-candidate
roles and evidence, go/no-go blockers, and publication boundary.

## Version update workflow

1. Update only `src/sdai/__init__.py::__version__` to the intended semantic version.
2. Update the README project-status description if the release scope/status wording changed.
3. Update the README release marker and project-status version to the same value.
4. Update the current release-readiness document's `sdai-release-version` marker and release identity.
5. Keep the `pyproject.toml` development-status classifier aligned with the actual release maturity; do not encode the package version there.
6. Run the full test suite:

   ```bash
   pytest -q
   ```

7. Run the isolated package artifact smoke:

   ```bash
   python tests/package_install_smoke.py
   ```

8. Verify the console reports the intended version:

   ```bash
   sdai --version
   ```

9. Verify a fresh scaffold and an upgraded scaffold write `.sdai/framework-version.yaml` with the same framework version.
10. Freeze the candidate commit and require the complete supported OS/Python matrix to pass on that exact head before merging a release-synchronization PR.
11. Build/publish the package or create a release tag only as a separate explicit release action after the intended release commit is green.

Any commit after the candidate SHA is frozen invalidates earlier final-review
and exact-head CI evidence. Re-review the new diff and rerun the complete matrix.

## SDAI 1.0 release gate

For SDAI 1.0, `.github/workflows/ci.yml` runs both the unfiltered repository test suite and the isolated wheel-install smoke on:

- Ubuntu — Python 3.11 and 3.12;
- Windows — Python 3.11 and 3.12;
- macOS — Python 3.11 and 3.12.

`tests/package_install_smoke.py` builds the repository as a wheel, installs it and its dependencies into a fresh virtual environment with repository `PYTHONPATH` removed, invokes the installed console entrypoint from outside the source tree, and exercises initialization, upgrade/migration, preservation, and rollback behavior.

The current 1.0 release synchronization record is `docs/releases/1.0-release-readiness.md`. Detailed capability evidence remains in the other `docs/releases/` records and their historical release gates.

## Deterministic guard

`tests/test_version_sync_v06.py` calls `validate_release_metadata()` and fails when:

- README's release marker is stale;
- README's project-status version is stale or its current-release header still says active development;
- `pyproject.toml` reintroduces a duplicate static `project.version`;
- setuptools dynamic versioning stops reading `sdai.__version__`;
- package maturity drifts from the intended stable classifier;
- the current 1.0 release-readiness marker drifts from the authoritative version;
- the 1.0 release record loses any completed stabilization-slice reference;
- the 1.0 release record stops explicitly preserving the held #25 identity scope;
- `sdai --version` does not report the authoritative version;
- init/upgrade scaffold metadata does not contain the authoritative version.

The package smoke independently verifies that the version embedded in the built/installed wheel matches the authoritative version and installed CLI output. This makes source metadata drift and artifact metadata drift CI failures rather than manual release-review concerns.

## Schema versions are not package versions

Do not change unrelated schema/protocol versions as part of a package release unless their contract actually changes. Examples include:

- `.sdai/config.yaml` schema `version`;
- workflow definition `version`;
- `apiVersion: sdai/v1`;
- extension `metadata.version`;
- `.sdai/framework-version.yaml` `schema_version`.

Those values represent independent compatibility contracts and must not be mechanically synchronized to the package version.

## Scope boundary

Release synchronization does not expand product authority. In particular, SDAI 1.0 does not claim the held 0.18/#25 GitHub Enterprise/OIDC/SSO identity-backed approval capability. Existing approval, audit, and migration records remain the local integrity/provenance evidence defined by their owning contracts unless a future explicitly authorized release adds identity verification.
