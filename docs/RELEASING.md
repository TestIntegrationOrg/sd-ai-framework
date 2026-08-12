# Releasing SD-AI

SD-AI uses a single authoritative framework/package version:

```text
src/sdai/__init__.py::__version__
```

Do not add a second package version literal to `pyproject.toml`. Setuptools reads the project version dynamically from `sdai.__version__`.

## Version update workflow

1. Update only `src/sdai/__init__.py::__version__` to the intended semantic version.
2. Update the README project-status description if the release scope/status wording changed.
3. Update the README release marker and project-status version to the same value.
4. Run the full test suite:

   ```bash
   pytest -q
   ```

5. Verify the console reports the intended version:

   ```bash
   sdai --version
   ```

6. Verify a fresh scaffold and an upgraded scaffold write `.sdai/framework-version.yaml` with the same framework version.
7. Build/publish the package only after CI is green on the release commit.

## Deterministic guard

`tests/test_version_sync_v06.py` calls `validate_release_metadata()` and fails when:

- README's release marker is stale;
- README's project-status version is stale;
- `pyproject.toml` reintroduces a duplicate static `project.version`;
- setuptools dynamic versioning stops reading `sdai.__version__`;
- `sdai --version` does not report the authoritative version;
- init/upgrade scaffold metadata does not contain the authoritative version.

This makes version drift a CI failure rather than a manual documentation review concern.

## Schema versions are not package versions

Do not change unrelated schema/protocol versions as part of a package release unless their contract actually changes. Examples include:

- `.sdai/config.yaml` schema `version`;
- workflow definition `version`;
- `apiVersion: sdai/v1`;
- extension `metadata.version`;
- `.sdai/framework-version.yaml` `schema_version`.

Those values represent independent compatibility contracts and must not be mechanically synchronized to the package version.
