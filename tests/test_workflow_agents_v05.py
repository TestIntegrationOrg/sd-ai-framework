from pathlib import Path

from sdai.agent_platform.models import AgentInvocation
from sdai.orchestrator import Orchestrator
from sdai.scaffold import init_project
from sdai.v05_scaffold import install_v05_scaffold
from sdai.workflow_templates import install_current_workflows
from sdai.workflows import load_workflow


def _project(tmp_path: Path) -> None:
    init_project(tmp_path)
    install_v05_scaffold(tmp_path)
    install_current_workflows(tmp_path)


def test_enterprise_workflow_references_semantic_agents(tmp_path: Path):
    _project(tmp_path)
    workflow = load_workflow(tmp_path, "enterprise")

    architecture = workflow.step("architecture-review")
    implementation = workflow.step("implementation")
    assert architecture.agent_name == "architect"
    assert implementation.agent_name == "developer"


def test_manual_step_can_override_semantic_agent_and_provider_independently(tmp_path: Path):
    _project(tmp_path)

    result = Orchestrator(tmp_path).run_manual_step(
        "V05-STEP",
        "enterprise",
        "architecture-review",
        dry_run=True,
        agent_override="architect",
        profile_override="codex",
    )
    assert result.status == "dry-run"
    assert isinstance(result.result, AgentInvocation)
    assert result.result.agent_name == "architect"
    assert result.result.profile.name == "codex"


def test_parallel_dry_run_uses_child_semantic_agents(tmp_path: Path):
    _project(tmp_path)

    result = Orchestrator(tmp_path).run_manual_step(
        "V05-PARALLEL",
        "enterprise",
        "design-reviews",
        dry_run=True,
    )
    assert result.status == "dry-run"
    invocations = [child.result for child in result.result]
    assert {invocation.agent_name for invocation in invocations} == {"architect", "security-reviewer"}
