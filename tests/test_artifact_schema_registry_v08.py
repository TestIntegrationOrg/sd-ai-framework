from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from sdai.artifact_schemas import ArtifactSchemaError, load_artifact_schema_graph
from sdai.entrypoint import main as sdai_main


def _manifest(schema_id: str, artifacts: list[dict[str, object]]) -> dict[str, object]:
    return {
        "apiVersion": "sdai/v1",
        "kind": "ArtifactSchema",
        "metadata": {
            "id": schema_id,
            "version": "1.0.0",
            "description": f"Fixture {schema_id}",
        },
        "spec": {"artifacts": artifacts},
    }


def _write_schema(path: Path, schema_id: str, artifacts: list[dict[str, object]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(_manifest(schema_id, artifacts), sort_keys=False),
        encoding="utf-8",
    )
    return path


def _repo_schema(root: Path, filename: str, schema_id: str, artifacts: list[dict[str, object]]) -> Path:
    return _write_schema(root / ".sdai" / "schemas" / filename, schema_id, artifacts)


def _init(root: Path) -> None:
    path = root / ".sdai" / "config.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("version: 1\n", encoding="utf-8")


def test_builtin_schema_is_packaged_file_driven_and_topologically_valid(tmp_path: Path) -> None:
    graph = load_artifact_schema_graph(tmp_path, environ={})
    by_id = graph.by_id()

    assert {"requirements", "architecture", "plan", "tasks", "tests", "verification"} <= set(by_id)
    assert graph.sources == ("builtin:core.yaml",)
    assert graph.topological_order.index("requirements") < graph.topological_order.index("architecture")
    assert graph.topological_order.index("plan") < graph.topological_order.index("tasks")
    assert by_id["requirements"].source_layer.value == "builtin"
    assert by_id["requirements"].source == "core.yaml"

    first = graph.to_json()
    second = load_artifact_schema_graph(tmp_path, environ={}).to_json()
    assert first == second
    assert "\\" not in first


def test_repository_schema_can_add_artifact_and_provenance_is_explainable(tmp_path: Path) -> None:
    _repo_schema(
        tmp_path,
        "operations.yaml",
        "repo-operations",
        [
            {
                "id": "operations",
                "path": "specs/changes/{feature}/operations.md",
                "type": "markdown",
                "required": False,
                "depends_on": ["architecture"],
                "applies_to": ["critical", "regulated"],
            }
        ],
    )

    graph = load_artifact_schema_graph(tmp_path, environ={})
    operations = graph.by_id()["operations"]

    assert operations.depends_on == ("architecture",)
    assert operations.source_layer.value == "repo"
    assert operations.source == ".sdai/schemas/operations.yaml"
    assert operations.history[-1].schema_id == "repo-operations"
    assert "operations" in graph.topological_order


def test_same_layer_duplicate_artifact_definition_fails_closed(tmp_path: Path) -> None:
    _repo_schema(
        tmp_path,
        "a.yaml",
        "repo-a",
        [{"id": "plan", "required": True}],
    )
    _repo_schema(
        tmp_path,
        "b.yaml",
        "repo-b",
        [{"id": "plan", "required": True}],
    )

    with pytest.raises(ArtifactSchemaError, match="SDAI-SCHEMA-003.*plan.*repo"):
        load_artifact_schema_graph(tmp_path, environ={})


def test_missing_dependency_and_cycle_fail_with_stable_codes(tmp_path: Path) -> None:
    missing_root = tmp_path / "missing"
    _repo_schema(
        missing_root,
        "missing.yaml",
        "missing-edge",
        [
            {
                "id": "release",
                "path": "specs/changes/{feature}/release.md",
                "type": "markdown",
                "depends_on": ["does-not-exist"],
            }
        ],
    )
    with pytest.raises(ArtifactSchemaError, match="SDAI-SCHEMA-005.*does-not-exist"):
        load_artifact_schema_graph(missing_root, environ={})

    cycle_root = tmp_path / "cycle"
    _repo_schema(
        cycle_root,
        "cycle.yaml",
        "cycle",
        [
            {
                "id": "cycle-a",
                "path": "specs/changes/{feature}/cycle-a.md",
                "type": "markdown",
                "depends_on": ["cycle-b"],
            },
            {
                "id": "cycle-b",
                "path": "specs/changes/{feature}/cycle-b.md",
                "type": "markdown",
                "depends_on": ["cycle-a"],
            },
        ],
    )
    with pytest.raises(ArtifactSchemaError, match="SDAI-SCHEMA-006.*cycle-a.*cycle-b"):
        load_artifact_schema_graph(cycle_root, environ={})


@pytest.mark.parametrize(
    "bad_path",
    [
        "../outside.md",
        "/absolute/outside.md",
        "C:/outside.md",
        r"specs\\changes\\{feature}\\bad.md",
        "specs/changes/{unknown}/bad.md",
    ],
)
def test_artifact_paths_are_portable_contained_templates(tmp_path: Path, bad_path: str) -> None:
    _repo_schema(
        tmp_path,
        "bad-path.yaml",
        "bad-path",
        [{"id": "bad-path", "path": bad_path, "type": "markdown"}],
    )

    with pytest.raises(ArtifactSchemaError, match="SDAI-SCHEMA-002"):
        load_artifact_schema_graph(tmp_path, environ={})


def test_org_required_artifact_cannot_be_disabled_or_made_optional(tmp_path: Path) -> None:
    org = _write_schema(
        tmp_path / "org.yaml",
        "org-mandates",
        [{"id": "security", "required": True}],
    )
    _repo_schema(
        tmp_path,
        "weaken.yaml",
        "repo-weaken",
        [{"id": "security", "required": False}],
    )

    with pytest.raises(ArtifactSchemaError, match="SDAI-SCHEMA-004.*required by organization"):
        load_artifact_schema_graph(
            tmp_path,
            environ={"SDAI_ORG_SCHEMA_PATH": str(org.resolve())},
        )


def test_org_locked_artifact_rejects_any_lower_layer_override(tmp_path: Path) -> None:
    org = _write_schema(
        tmp_path / "org.yaml",
        "org-locks",
        [{"id": "security", "required": True, "locked": True}],
    )
    _repo_schema(
        tmp_path,
        "override.yaml",
        "repo-override",
        [{"id": "security", "applies_to": ["regulated"]}],
    )

    with pytest.raises(ArtifactSchemaError, match="SDAI-SCHEMA-004.*locked.*org"):
        load_artifact_schema_graph(
            tmp_path,
            environ={"SDAI_ORG_SCHEMA_PATH": str(org.resolve())},
        )


def test_org_mandated_dependency_cannot_be_removed(tmp_path: Path) -> None:
    org = _write_schema(
        tmp_path / "org.yaml",
        "org-dependencies",
        [{"id": "tests", "depends_on": ["requirements", "tasks", "security"]}],
    )
    _repo_schema(
        tmp_path,
        "weaken.yaml",
        "repo-weaken",
        [{"id": "tests", "depends_on": ["requirements", "tasks"]}],
    )

    with pytest.raises(ArtifactSchemaError, match="SDAI-SCHEMA-004.*organization dependency.*security"):
        load_artifact_schema_graph(
            tmp_path,
            environ={"SDAI_ORG_SCHEMA_PATH": str(org.resolve())},
        )


def test_repo_added_dependency_is_not_promoted_into_org_mandate(tmp_path: Path) -> None:
    org = _write_schema(
        tmp_path / "org.yaml",
        "org-dependencies",
        [{"id": "tests", "depends_on": ["requirements", "tasks", "security"]}],
    )
    _repo_schema(
        tmp_path,
        "repo.yaml",
        "repo-addition",
        [{"id": "tests", "depends_on": ["requirements", "tasks", "security", "architecture"]}],
    )
    user = _write_schema(
        tmp_path / "user.yaml",
        "user-adjustment",
        [{"id": "tests", "depends_on": ["requirements", "tasks", "security"]}],
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
    assert [item.layer.value for item in tests.history] == ["builtin", "org", "repo", "user"]


def test_org_external_schema_path_must_be_absolute_and_not_symlink(tmp_path: Path) -> None:
    with pytest.raises(ArtifactSchemaError, match="SDAI-SCHEMA-008.*absolute"):
        load_artifact_schema_graph(tmp_path, environ={"SDAI_ORG_SCHEMA_PATH": "relative.yaml"})


def test_cli_schema_graph_json_is_stable_and_portable_in_unicode_workspace(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "Enterprise Workspace Ω"
    root.mkdir()
    _init(root)
    _repo_schema(
        root,
        "café.yaml",
        "cafe-artifact",
        [
            {
                "id": "operations",
                "path": "specs/changes/{feature}/café-Δ.md",
                "type": "markdown",
                "depends_on": ["architecture"],
            }
        ],
    )

    assert sdai_main(["schema", "graph", "--json", "--path", str(root)]) == 0
    output = capsys.readouterr().out
    payload = json.loads(output)

    assert payload["version"] == 1
    assert any(item["id"] == "operations" for item in payload["artifacts"])
    assert "\\" not in output
