from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import tomllib

import yaml

from sdai import __version__
from sdai.artifacts import write_text
from sdai.path_safety import ensure_within_project
from sdai.text import read_utf8_text


README_VERSION_MARKER_PREFIX = "<!-- sdai-release-version:"
PYPROJECT_VERSION_ATTR = "sdai.__version__"
FRAMEWORK_METADATA_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class ReleaseMetadataValidation:
    errors: tuple[str, ...]

    @property
    def valid(self) -> bool:
        return not self.errors


def framework_metadata_text() -> str:
    payload = {
        "schema_version": FRAMEWORK_METADATA_SCHEMA_VERSION,
        "framework_version": __version__,
    }
    return "# Managed by SD-AI. Do not edit manually.\n" + yaml.safe_dump(
        payload,
        sort_keys=False,
    )


def write_framework_metadata(project_root: Path) -> Path:
    root = project_root.resolve()
    config = ensure_within_project(
        root,
        root / ".sdai" / "config.yaml",
        label="SD-AI project config",
    )
    if not config.is_file():
        raise FileNotFoundError("Not an SD-AI project. Run `sdai init` first.")
    target = ensure_within_project(
        root,
        root / ".sdai" / "framework-version.yaml",
        label="framework version metadata",
    )
    return write_text(target, framework_metadata_text(), overwrite=True)


def _validate_readme(repo_root: Path, errors: list[str]) -> None:
    readme = repo_root / "README.md"
    if not readme.is_file():
        errors.append("README.md is missing")
        return
    text = read_utf8_text(readme)
    marker = f"{README_VERSION_MARKER_PREFIX} {__version__} -->"
    if text.count(marker) != 1:
        errors.append(
            "README release marker is stale or ambiguous; expected exactly one "
            f"'{marker}'"
        )
    status_pattern = re.compile(
        rf"^> Project status: \*\*{re.escape(__version__)} / .+\*\*\.$",
        re.MULTILINE,
    )
    if not status_pattern.search(text):
        errors.append(
            "README project-status version is stale; expected status to start with "
            f"'{__version__} /'"
        )


def _validate_pyproject(repo_root: Path, errors: list[str]) -> None:
    path = repo_root / "pyproject.toml"
    if not path.is_file():
        errors.append("pyproject.toml is missing")
        return
    with path.open("rb") as handle:
        raw = tomllib.load(handle)
    project = raw.get("project") or {}
    if "version" in project:
        errors.append(
            "pyproject.toml must not duplicate project.version; use dynamic versioning"
        )
    dynamic = project.get("dynamic") or []
    if "version" not in dynamic:
        errors.append("pyproject.toml project.dynamic must include 'version'")
    setuptools = raw.get("tool", {}).get("setuptools", {})
    version_config = setuptools.get("dynamic", {}).get("version", {})
    if version_config.get("attr") != PYPROJECT_VERSION_ATTR:
        errors.append(
            "pyproject.toml setuptools dynamic version must read "
            f"'{PYPROJECT_VERSION_ATTR}'"
        )


def validate_release_metadata(repo_root: Path) -> ReleaseMetadataValidation:
    root = repo_root.resolve()
    errors: list[str] = []
    _validate_readme(root, errors)
    _validate_pyproject(root, errors)
    return ReleaseMetadataValidation(tuple(errors))
