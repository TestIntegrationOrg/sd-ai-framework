from __future__ import annotations

from pathlib import Path

import yaml

from sdai import __version__
from sdai.version_entrypoint import main as version_main
from sdai.versioning import validate_release_metadata


def _write_release_files(root: Path, *, readme_version: str) -> None:
    (root / "README.md").write_text(
        f"""# SD-AI Framework

<!-- sdai-release-version: {readme_version} -->
> Project status: **{readme_version} / test release status**.
""",
        encoding="utf-8",
    )
    (root / "pyproject.toml").write_text(
        """[project]
name = "sd-ai-framework"
dynamic = ["version"]

[tool.setuptools.dynamic]
version = {attr = "sdai.__version__"}
""",
        encoding="utf-8",
    )


def test_repository_release_metadata_is_synchronized() -> None:
    root = Path(__file__).resolve().parents[1]

    result = validate_release_metadata(root)

    assert result.valid, "\n".join(result.errors)


def test_stale_readme_version_fails_release_validation(tmp_path: Path) -> None:
    _write_release_files(tmp_path, readme_version="9.9.9")

    result = validate_release_metadata(tmp_path)

    assert result.valid is False
    assert any("README release marker is stale" in error for error in result.errors)
    assert any("README project-status version is stale" in error for error in result.errors)


def test_static_pyproject_version_fails_release_validation(tmp_path: Path) -> None:
    _write_release_files(tmp_path, readme_version=__version__)
    path = tmp_path / "pyproject.toml"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            'dynamic = ["version"]',
            f'version = "{__version__}"',
        ),
        encoding="utf-8",
    )

    result = validate_release_metadata(tmp_path)

    assert result.valid is False
    assert any("must not duplicate project.version" in error for error in result.errors)
    assert any("project.dynamic must include 'version'" in error for error in result.errors)


def test_console_version_uses_authoritative_package_version(
    capsys,
) -> None:
    assert version_main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == f"sdai {__version__}"


def test_init_and_upgrade_write_authoritative_framework_metadata(
    tmp_path: Path,
    capsys,
) -> None:
    assert version_main(["init", "--path", str(tmp_path)]) == 0
    output = capsys.readouterr().out
    metadata = tmp_path / ".sdai" / "framework-version.yaml"
    payload = yaml.safe_load(metadata.read_text(encoding="utf-8"))

    assert payload == {"schema_version": 1, "framework_version": __version__}
    assert f"SD-AI framework version {__version__}" in output
    assert ".sdai/framework-version.yaml" in output

    metadata.write_text(
        "schema_version: 1\nframework_version: 0.0.0\n",
        encoding="utf-8",
    )

    assert version_main(["upgrade", "--path", str(tmp_path)]) == 0
    output = capsys.readouterr().out
    payload = yaml.safe_load(metadata.read_text(encoding="utf-8"))

    assert payload["framework_version"] == __version__
    assert f"SD-AI framework version {__version__}" in output
