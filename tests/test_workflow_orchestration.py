from pathlib import Path

from sdai.agent_platform.models import AgentInvocation
from sdai.artifacts import write_text
from sdai.orchestrator import Orchestrator
from sdai.scaffold import init_project
from sdai.workflow_templates import install_v03_workflows
from sdai.workflows import grant_approval, load_workflow, load_workflow_state


def _feature(root: Path, feature_id: str = "FLOW-1") -> None:
    write_text(
        root / "specs" / feature_id / "00-intake.md",
        f"# Feature Intake — {feature_id}\n\n## Title\nWorkflow\n\n## Description\nExercise orchestration.\n",
    )


def test_workflow_pauses_at_approval_and_resumes_without_repeating_completed_steps(tmp_path: Path):
    init_project(tmp_path)
    _feature(tmp_path)
    write_text(
        tmp_path / ".sdai" / "workflows" / "reviewed.yaml",
        """version: 3
name: reviewed
validation_mode: standard
steps:
  - id: specification
    type: deterministic
    action: specify
  - id: architecture-approval
    type: approval
    gate: architecture
  - id: architecture
    type: deterministic
    action: architect
""",
    )

    orchestrator = Orchestrator(tmp_path)
    first = orchestrator.run_workflow("FLOW-1", "reviewed")
    assert [item.status for item in first] == ["completed", "paused"]

    state = load_workflow_state(orchestrator.context("FLOW-1"), "reviewed")
    assert state.completed_steps == ["specification"]
    assert state.paused_at == "architecture-approval"

    grant_approval(orchestrator.context("FLOW-1"), "architecture", approved_by="architect@example.com")
    second = orchestrator.run_workflow("FLOW-1", "reviewed")
    assert [item.status for item in second] == ["skipped", "completed", "completed"]
    assert orchestrator.context("FLOW-1").artifact("architecture/architecture.md").exists()

    state = load_workflow_state(orchestrator.context("FLOW-1"), "reviewed")
    assert state.last_status == "completed"


def test_manual_step_can_run_out_of_order_and_force_controls_rerun(tmp_path: Path):
    init_project(tmp_path)
    _feature(tmp_path)
    write_text(
        tmp_path / ".sdai" / "workflows" / "manual.yaml",
        """version: 3
name: manual
validation_mode: standard
steps:
  - id: approval-first
    type: approval
    gate: architecture
  - id: specification
    type: deterministic
    action: specify
""",
    )

    orchestrator = Orchestrator(tmp_path)
    first = orchestrator.run_manual_step("FLOW-1", "manual", "specification")
    assert first.status == "completed"

    protected = orchestrator.run_manual_step("FLOW-1", "manual", "specification")
    assert protected.status == "skipped"
    assert "--force" in protected.message

    rerun = orchestrator.run_manual_step("FLOW-1", "manual", "specification", force=True)
    assert rerun.status == "completed"


def test_manual_external_agent_step_supports_dry_run_without_provider_binary(tmp_path: Path):
    init_project(tmp_path)
    _feature(tmp_path)
    write_text(
        tmp_path / ".sdai" / "workflows" / "ai-review.yaml",
        """version: 3
name: ai-review
validation_mode: light
steps:
  - id: architecture-review
    type: agent
    capability: architecture
    mode: advisory
""",
    )

    execution = Orchestrator(tmp_path).run_manual_step(
        "FLOW-1", "ai-review", "architecture-review", dry_run=True
    )
    assert execution.status == "dry-run"
    assert isinstance(execution.result, AgentInvocation)
    assert execution.result.capability.value == "architecture"


def test_v03_agentic_template_is_installable_and_typed(tmp_path: Path):
    init_project(tmp_path)
    created = install_v03_workflows(tmp_path)
    assert created
    definition = load_workflow(tmp_path, "agentic")
    assert definition.validation_mode.value == "critical"
    assert definition.step("architecture-approval").kind.value == "approval"
    assert definition.step("implementation").profile == "codex"
    assert definition.step("code-review").profile == "copilot"
