from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from sdai.artifact_schemas import ArtifactSchemaError, load_artifact_schema_graph


def _manifest(schema_id: str, artifacts: list[dict[str, object]]) -> dict[str, object]:
    return {
        "apiVersion": "sdai/v1",
        "kind": "ArtifactSchema",
        "metadata": {"id": schema_id, "version": "1.0.0"},
        "spec": {"artifacts": artifacts},
    }


def _write(path: Path, schema_id: str, artifacts: list[dict[str, object]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(_manifest(schema_id, artifacts), sort_keys=False),
        encoding="utf-8",
    )
    return path


def test_org_dependency_mandate_survives_repo_disable_and_blocks_weak_user_readd(
    tmp_path: Path,
) -> None:
    org = _write(
        tmp_path / "org.yaml",
        "org-mandate",
        [{"id": "tests", "depends_on": ["requirements", "tasks", "security"]}],
    )
    _write(
        tmp_path / ".sdai" / "schemas" / "repo.yaml",
        "repo-disable",
        [{"id": "tests", "disabled": True}],
    )
    user = _write(
        tmp_path / "user.yaml",
        "user-readd",
        [
            {
                "id": "tests",
                "path": "specs/changes/{feature}/tests.md",
                "type": "markdown",
                "depends_on": ["requirements", "tasks"],
            }
        ],
    )

    with pytest.raises(
        ArtifactSchemaError,
        match="SDAI-SCHEMA-004.*organization dependency.*security",
    ):
        load_artifact_schema_graph(
            tmp_path,
            environ={
                "SDAI_ORG_SCHEMA_PATH": str(org.resolve()),
                "SDAI_USER_SCHEMA_PATH": str(user.resolve()),
            },
        )


def test_org_dependency_mandate_survives_repo_disable_and_allows_compliant_user_readd(
    tmp_path: Path,
) -> None:
    org = _write(
        tmp_path / "org.yaml",
        "org-mandate",
        [{"id": "tests", "depends_on": ["requirements", "tasks", "security"]}],
    )
    _write(
        tmp_path / ".sdai" / "schemas" / "repo.yaml",
        "repo-disable",
        [{"id": "tests", "disabled": True}],
    )
    user = _write(
        tmp_path / "user.yaml",
        "user-readd",
        [
            {
                "id": "tests",
                "path": "specs/changes/{feature}/tests.md",
                "type": "markdown",
                "depends_on": ["requirements", "tasks", "security"],
            }
        ],
    )

    graph = load_artifact_schema_graph(
        tmp_path,
        environ={
            "SDAI_ORG_SCHEMA_PATH": str(org.resolve()),
            "SDAI_USER_SCHEMA_PATH": str(user.resolve()),
        },
    )

    tests = graph.by_id()["tests"]
    assert tests.depends_on == ("requirements", "tasks", "security")
    assert tests.organization_dependencies == ("requirements", "security", "tasks")
    assert tests.source_layer.value == "user"


@pytest.mark.parametrize(
    "bad_name",
    ["bad?.md", "bad*.md", 'bad\".md', "bad<.md", "bad>.md", "bad|.md"],
)
def test_schema_rejects_complete_windows_invalid_filename_character_set(
    tmp_path: Path,
    bad_name: str,
) -> None:
    _write(
        tmp_path / ".sdai" / "schemas" / "bad.yaml",
        "bad-path",
        [
            {
                "id": "bad-file",
                "path": f"specs/changes/{{feature}}/{bad_name}",
                "type": "markdown",
            }
        ],
    )

    with pytest.raises(ArtifactSchemaError, match="SDAI-SCHEMA-002"):
        load_artifact_schema_graph(tmp_path, environ={})


@pytest.mark.parametrize("bad_type", [["markdown"], {"name": "markdown"}, 7])
def test_non_string_artifact_type_fails_with_stable_schema_error(
    tmp_path: Path,
    bad_type: object,
) -> None:
    _write(
        tmp_path / ".sdai" / "schemas" / "bad-type.yaml",
        "bad-type",
        [
            {
                "id": "bad-type",
                "path": "specs/changes/{feature}/bad.md",
                "type": bad_type,
            }
        ],
    )

    with pytest.raises(ArtifactSchemaError, match="SDAI-SCHEMA-001.*unsupported type"):
        load_artifact_schema_graph(tmp_path, environ={})
