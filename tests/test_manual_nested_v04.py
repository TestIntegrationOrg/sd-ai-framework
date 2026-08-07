from pathlib import Path

from sdai.orchestrator import Orchestrator
from sdai.scaffold import init_project
from sdai.enterprise_scaffold import install_v04_scaffold
from sdai.workflow_templates import install_current_workflows
from sdai.artifacts import write_text
from sdai.workflows import load_workflow_state


def _project(root: Path) -> None:
    init_project(root)
    install_v04_scaffold(root)
    install_current_workflows(root)
    write_text(
        root / "specs" / "NEST-1" / "00-intake.md",
        "# Feature Intake — NEST-1\n\n## Title\nNested manual control\n\n## Description\nTest child steps.\n",
    )


def test_parallel_child_can_be_run_manually_by_unique_id(tmp_path: Path):
    _project(tmp_path)
    execution = Orchestrator(tmp_path).run_manual_step(
        "NEST-1",
        "enterprise",
        "architecture-review",
        dry_run=True,
    )
    assert execution.step_id == "architecture-review"
    assert execution.status == "dry-run"
    assert execution.result.capability.value == "architecture"


def test_parallel_group_dry_run_never_executes_or_marks_state_complete(tmp_path: Path):
    _project(tmp_path)
    orchestrator = Orchestrator(tmp_path)
    execution = orchestrator.run_manual_step(
        "NEST-1",
        "enterprise",
        "design-reviews",
        dry_run=True,
    )
    assert execution.status == "dry-run"
    assert [child.status for child in execution.result] == ["dry-run", "dry-run"]

    state = load_workflow_state(orchestrator.context("NEST-1"), "enterprise")
    assert "design-reviews" not in state.completed_steps
    assert "architecture-review" not in state.completed_steps
    assert "security-review" not in state.completed_steps
