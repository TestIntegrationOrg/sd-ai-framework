from __future__ import annotations

from pathlib import Path

import yaml

from sdai import __version__
from sdai.version_entrypoint import main as version_main
from sdai.versioning import validate_release_metadata


_REQUIRED_SLICES = ("#270", "#272", "#274", "#276", "#278", "#280", "#282", "#284", "#286")


def _write_release_files(
    root: Path,
    *,
    readme_version: str,
    readiness_version: str | None = None,
    development_status: str = "Development Status :: 5 - Production/Stable",
    held_scope: bool = True,
) -> None:
    (root / "README.md").write_text(
        f"""# SD-AI Framework

<!-- sdai-release-version: {readme_version} -->
> Project status: **{readme_version} / test release status**.

## One framework, same capabilities
""",
        encoding="utf-8",
    )
    (root / "pyproject.toml").write_text(
        f"""[project]
name = "sd-ai-framework"
dynamic = ["version"]
classifiers = ["{development_status}"]

[tool.setuptools.dynamic]
version = {{attr = "sdai.__version__"}}
""",
        encoding="utf-8",
    )
    release_dir = root / "docs" / "releases"
    release_dir.mkdir(parents=True)
    readiness = readiness_version or __version__
    held = (
        "0.18/#25 identity-backed approvals remain held\n"
        if held_scope
        else "identity scope omitted\n"
    )
    (release_dir / "1.0-release-readiness.md").write_text(
        "\n".join(
            [
                "# SDAI 1.0 — Release Readiness",
                "",
                f"<!-- sdai-release-version: {readiness} -->",
                "",
                held.rstrip("\n"),
                "",
                "Completed slices: " + " ".join(_REQUIRED_SLICES),
                "",
            ]
        ),
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


def test_active_development_release_status_fails_validation(tmp_path: Path) -> None:
    _write_release_files(tmp_path, readme_version=__version__)
    path = tmp_path / "README.md"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "test release status",
            "foundation in active development",
        ),
        encoding="utf-8",
    )

    result = validate_release_metadata(tmp_path)

    assert result.valid is False
    assert any("active development" in error for error in result.errors)


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


def test_nonstable_package_classifier_fails_release_validation(tmp_path: Path) -> None:
    _write_release_files(
        tmp_path,
        readme_version=__version__,
        development_status="Development Status :: 3 - Alpha",
    )

    result = validate_release_metadata(tmp_path)

    assert result.valid is False
    assert any("stable development status" in error for error in result.errors)


def test_stale_release_readiness_version_fails_validation(tmp_path: Path) -> None:
    _write_release_files(
        tmp_path,
        readme_version=__version__,
        readiness_version="9.9.9",
    )

    result = validate_release_metadata(tmp_path)

    assert result.valid is False
    assert any("release-readiness version is stale" in error for error in result.errors)


def test_release_readiness_must_preserve_held_identity_scope(tmp_path: Path) -> None:
    _write_release_files(
        tmp_path,
        readme_version=__version__,
        held_scope=False,
    )

    result = validate_release_metadata(tmp_path)

    assert result.valid is False
    assert any("held #25 identity scope" in error for error in result.errors)


def test_release_readiness_must_list_completed_stabilization_slices(
    tmp_path: Path,
) -> None:
    _write_release_files(tmp_path, readme_version=__version__)
    path = tmp_path / "docs" / "releases" / "1.0-release-readiness.md"
    path.write_text(
        path.read_text(encoding="utf-8").replace("#286", "missing-last-slice"),
        encoding="utf-8",
    )

    result = validate_release_metadata(tmp_path)

    assert result.valid is False
    assert any("missing completed release slices" in error for error in result.errors)


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
