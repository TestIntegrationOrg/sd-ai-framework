from __future__ import annotations

from pathlib import Path

import yaml

from sdai.workflows import load_workflow


def _write_yaml(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def test_lower_layers_may_add_around_org_mandated_step_without_weakening_it(
    tmp_path: Path,
) -> None:
    _write_yaml(
        tmp_path / ".sdai" / "workflows" / "ordered.yaml",
        {
            "version": 7,
            "name": "ordered",
            "validation_mode": "standard",
            "steps": [{"id": "base-validation", "type": "validate"}],
        },
    )
    org = tmp_path / "org.yaml"
    _write_yaml(
        org,
        {
            "version": 1,
            "id": "org-control",
            "workflow": "ordered",
            "operations": [
                {
                    "op": "append",
                    "step": {"id": "org-mandated", "type": "validate"},
                }
            ],
        },
    )
    _write_yaml(
        tmp_path / ".sdai" / "workflow-overlays" / "repo.yaml",
        {
            "version": 1,
            "id": "repo-extension",
            "workflow": "ordered",
            "operations": [
                {
                    "op": "add-before",
                    "target": "org-mandated",
                    "step": {"id": "repo-before", "type": "validate"},
                },
                {
                    "op": "add-after",
                    "target": "org-mandated",
                    "step": {"id": "repo-after", "type": "validate"},
                },
            ],
        },
    )

    definition = load_workflow(
        tmp_path,
        "ordered",
        environ={"SDAI_ORG_WORKFLOW_OVERLAY_PATH": str(org.resolve())},
    )

    assert [step.id for step in definition.steps] == [
        "base-validation",
        "repo-before",
        "org-mandated",
        "repo-after",
    ]
    assert "org-mandated" in definition.mandatory_steps
