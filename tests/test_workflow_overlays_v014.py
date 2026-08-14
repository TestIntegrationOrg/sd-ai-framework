from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from sdai.workflow_graph import WorkflowGraphError, load_workflow_graph


def _write_yaml(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def _workflow(root: Path, steps: list[object]) -> None:
    _write_yaml(
        root / ".sdai" / "workflows" / "nested.yaml",
        {
            "version": 9,
            "name": "nested",
            "validation_mode": "standard",
            "steps": steps,
        },
    )


def _overlay(
    path: Path,
    overlay_id: str,
    operations: list[dict[str, object]],
) -> None:
    _write_yaml(
        path,
        {
            "version": 1,
            "id": overlay_id,
            "workflow": "nested",
            "operations": operations,
        },
    )


def _nested_steps() -> list[object]:
    return [
        {
            "id": "pipeline",
            "type": "sequence",
            "steps": [
                {"id": "prepare", "type": "deterministic", "action": "prepare"},
                {
                    "id": "checks",
                    "type": "sequence",
                    "steps": [
                        {"id": "unit", "type": "deterministic", "action": "test"},
                        {"id": "cleanup", "type": "deterministic", "action": "cleanup"},
                    ],
                },
            ],
        }
    ]


def test_nested_insert_replace_remove_are_path_addressed_and_hash_bound(tmp_path: Path) -> None:
    _workflow(tmp_path, _nested_steps())
    _overlay(
        tmp_path / ".sdai" / "workflow-overlays" / "nested.yaml",
        "repo-nested",
        [
            {
                "op": "insert-before",
                "target": "pipeline/checks/unit",
                "step": {"id": "lint", "type": "deterministic", "action": "lint"},
            },
            {
                "op": "replace",
                "target": "unit",
                "step": {
                    "id": "unit",
                    "type": "deterministic",
                    "action": "test",
                    "description": "hardened unit gate",
                },
            },
            {"op": "remove", "target": "pipeline/checks/cleanup"},
        ],
    )

    resolution = load_workflow_graph(tmp_path, "nested", environ={})

    paths = {node.path for node in resolution.graph.nodes}
    assert "pipeline/checks/lint" in paths
    assert "pipeline/checks/unit" in paths
    assert "pipeline/checks/cleanup" not in paths
    assert resolution.graph.node("pipeline/checks/unit").config["description"] == "hardened unit gate"
    overlay = resolution.overlays[0]
    operations = overlay["operation_provenance"]
    assert [item["op"] for item in operations] == ["insert-before", "replace", "remove"]
    assert operations[0]["target"] == "pipeline/checks/unit"
    assert operations[0]["post_graph_sha256"] == operations[1]["pre_graph_sha256"]
    assert operations[1]["post_graph_sha256"] == operations[2]["pre_graph_sha256"]
    assert overlay["pre_graph_sha256"] == operations[0]["pre_graph_sha256"]
    assert overlay["post_graph_sha256"] == operations[-1]["post_graph_sha256"]


def test_ambiguous_bare_nested_anchor_fails_with_canonical_paths(tmp_path: Path) -> None:
    _workflow(
        tmp_path,
        [
            {
                "id": "decision",
                "type": "if",
                "condition": True,
                "then": [{"id": "review", "type": "deterministic", "action": "review"}],
                "else": [{"id": "review", "type": "deterministic", "action": "review"}],
            }
        ],
    )
    _overlay(
        tmp_path / ".sdai" / "workflow-overlays" / "ambiguous.yaml",
        "repo-ambiguous",
        [
            {
                "op": "insert-before",
                "target": "review",
                "step": {"id": "audit", "type": "deterministic", "action": "audit"},
            }
        ],
    )

    with pytest.raises(WorkflowGraphError, match=r"SDAI-WFOVER-003.*ambiguous.*decision/\$then/review"):
        load_workflow_graph(tmp_path, "nested", environ={})


def test_lower_layer_cannot_remove_parent_of_nested_org_mandatory_step(tmp_path: Path) -> None:
    _workflow(
        tmp_path,
        [{"id": "pipeline", "type": "sequence", "steps": [{"id": "build", "type": "deterministic", "action": "build"}]}],
    )
    org = tmp_path / "org.yaml"
    _overlay(
        org,
        "org-gate",
        [
            {
                "op": "insert-after",
                "target": "pipeline/build",
                "step": {"id": "org-approval", "type": "approval", "gate": "security"},
            }
        ],
    )
    _overlay(
        tmp_path / ".sdai" / "workflow-overlays" / "remove.yaml",
        "repo-remove-parent",
        [{"op": "remove", "target": "pipeline"}],
    )

    with pytest.raises(WorkflowGraphError, match="SDAI-WFOVER-004.*organization-mandated step 'org-approval'"):
        load_workflow_graph(
            tmp_path,
            "nested",
            environ={"SDAI_ORG_WORKFLOW_OVERLAY_PATH": str(org.resolve())},
        )


def test_lower_layer_cannot_remove_parent_containing_core_validation(tmp_path: Path) -> None:
    _workflow(
        tmp_path,
        [{"id": "pipeline", "type": "sequence", "steps": [{"id": "validate", "type": "validate"}]}],
    )
    _overlay(
        tmp_path / ".sdai" / "workflow-overlays" / "remove.yaml",
        "repo-remove-validation",
        [{"op": "remove", "target": "pipeline"}],
    )

    with pytest.raises(WorkflowGraphError, match="SDAI-WFOVER-004.*cannot disable protected step 'pipeline'"):
        load_workflow_graph(tmp_path, "nested", environ={})


def test_destructive_aliases_of_same_canonical_target_conflict(tmp_path: Path) -> None:
    _workflow(tmp_path, _nested_steps())
    _overlay(
        tmp_path / ".sdai" / "workflow-overlays" / "conflict.yaml",
        "repo-conflict",
        [
            {"op": "remove", "target": "pipeline/checks/unit"},
            {
                "op": "replace",
                "target": "unit",
                "step": {"id": "unit", "type": "deterministic", "action": "test"},
            },
        ],
    )

    with pytest.raises(WorkflowGraphError, match="SDAI-WFOVER-003.*mutates step 'pipeline/checks/unit' more than once"):
        load_workflow_graph(tmp_path, "nested", environ={})


def test_overlay_cannot_add_writable_concurrent_branch(tmp_path: Path) -> None:
    _workflow(tmp_path, [{"id": "validate", "type": "validate"}])
    _overlay(
        tmp_path / ".sdai" / "workflow-overlays" / "parallel.yaml",
        "repo-parallel-write",
        [
            {
                "op": "append",
                "step": {
                    "id": "parallel-write",
                    "type": "parallel",
                    "steps": [
                        {
                            "id": "writer",
                            "type": "agent",
                            "agent": "developer",
                            "capability": "coding",
                            "mode": "workspace-write",
                        }
                    ],
                },
            }
        ],
    )

    with pytest.raises(WorkflowGraphError, match="SDAI-WFOVER-004.*workspace-writing branches"):
        load_workflow_graph(tmp_path, "nested", environ={})


def test_overlay_cannot_insert_writer_into_existing_parallel(tmp_path: Path) -> None:
    _workflow(
        tmp_path,
        [
            {
                "id": "checks",
                "type": "parallel",
                "steps": [
                    {
                        "id": "review",
                        "type": "agent",
                        "agent": "code-reviewer",
                        "capability": "review",
                        "mode": "advisory",
                    }
                ],
            }
        ],
    )
    _overlay(
        tmp_path / ".sdai" / "workflow-overlays" / "insert-writer.yaml",
        "repo-insert-writer",
        [
            {
                "op": "insert-after",
                "target": "checks/review",
                "step": {
                    "id": "writer",
                    "type": "agent",
                    "agent": "developer",
                    "capability": "coding",
                    "mode": "workspace-write",
                },
            }
        ],
    )

    with pytest.raises(WorkflowGraphError, match="SDAI-WFOVER-004.*workspace-writing concurrent branch"):
        load_workflow_graph(tmp_path, "nested", environ={})


def test_overlay_cannot_add_second_writer_to_already_writable_parallel(tmp_path: Path) -> None:
    _workflow(
        tmp_path,
        [
            {
                "id": "writers",
                "type": "parallel",
                "steps": [
                    {
                        "id": "existing-writer",
                        "type": "agent",
                        "agent": "developer",
                        "capability": "coding",
                        "mode": "workspace-write",
                    }
                ],
            }
        ],
    )
    _overlay(
        tmp_path / ".sdai" / "workflow-overlays" / "second-writer.yaml",
        "repo-second-writer",
        [
            {
                "op": "insert-after",
                "target": "writers/existing-writer",
                "step": {
                    "id": "second-writer",
                    "type": "agent",
                    "agent": "developer",
                    "capability": "coding",
                    "mode": "workspace-write",
                },
            }
        ],
    )

    with pytest.raises(WorkflowGraphError, match="SDAI-WFOVER-004.*workspace-writing concurrent branch"):
        load_workflow_graph(tmp_path, "nested", environ={})


def test_nested_overlay_steps_cannot_hide_provider_or_shell_fields(tmp_path: Path) -> None:
    _workflow(tmp_path, [{"id": "validate", "type": "validate"}])
    _overlay(
        tmp_path / ".sdai" / "workflow-overlays" / "nested-provider.yaml",
        "repo-nested-provider",
        [
            {
                "op": "append",
                "step": {
                    "id": "container",
                    "type": "sequence",
                    "steps": [
                        {
                            "id": "review",
                            "type": "agent",
                            "agent": "code-reviewer",
                            "capability": "review",
                            "mode": "advisory",
                            "provider": "unreviewed-provider",
                        }
                    ],
                },
            }
        ],
    )

    with pytest.raises(WorkflowGraphError, match="SDAI-WFOVER-003.*forbidden provider/shell field"):
        load_workflow_graph(tmp_path, "nested", environ={})


def test_same_layer_output_is_independent_of_overlay_filenames(tmp_path: Path) -> None:
    graph_hashes: list[str] = []
    for index, names in enumerate((("z.yaml", "a.yaml"), ("a.yaml", "z.yaml"))):
        root = tmp_path / str(index)
        _workflow(root, [{"id": "base", "type": "deterministic", "action": "base"}])
        for filename, overlay_id, step_id in zip(names, ("alpha", "beta"), ("alpha-step", "beta-step")):
            _overlay(
                root / ".sdai" / "workflow-overlays" / filename,
                overlay_id,
                [
                    {
                        "op": "insert-before",
                        "target": "base",
                        "step": {"id": step_id, "type": "deterministic", "action": step_id},
                    }
                ],
            )
        resolution = load_workflow_graph(root, "nested", environ={})
        graph_hashes.append(resolution.graph.sha256)
        assert resolution.graph.node("alpha-step").index == 0
        assert resolution.graph.node("beta-step").index == 1

    assert graph_hashes[0] == graph_hashes[1]
