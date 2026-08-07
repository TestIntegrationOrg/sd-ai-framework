from pathlib import Path

import pytest
import yaml

from sdai.agent_platform import Capability, ExecutionMode
from sdai.agent_platform.models import AgentExecutionResult
from sdai.artifacts import write_text
from sdai.orchestrator import Orchestrator
from sdai.scaffold import init_project
from sdai.workflows import grant_approval


class FakeRuntime:
    def execute(self, feature_id, capability, *, profile_name=None, mode=ExecutionMode.ADVISORY):
        return AgentExecutionResult(
            feature_id=feature_id,
            capability=capability,
            profile=profile_name or "fake",
            provider="fake",
            output="ok",
            prompt="test",
            skills=(),
        )

    def build_invocation(self, *args, **kwargs):  # pragma: no cover
        raise AssertionError("not expected")


def test_enterprise_policy_can_prohibit_force_bypass_for_manual_step(
    tmp_path: Path, monkeypatch
):
    init_project(tmp_path)
    config_path = tmp_path / ".sdai" / "config.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["operating_mode"] = "enterprise"
    write_text(config_path, yaml.safe_dump(config, sort_keys=False))

    org_policy = tmp_path.parent / f"{tmp_path.name}-org-approval.yaml"
    write_text(
        org_policy,
        """version: 1
execution:
  require_prior_approval_for_workspace_write: true
  allow_force_approval_bypass: false
""",
    )
    monkeypatch.setenv("SDAI_ORG_POLICY_PATH", str(org_policy.resolve()))

    write_text(
        tmp_path / "specs" / "POL-1" / "00-intake.md",
        "# Intake\n",
    )
    write_text(
        tmp_path / ".sdai" / "workflows" / "policy-write.yaml",
        """version: 5
name: policy-write
validation_mode: light
steps:
  - id: architecture-approval
    type: approval
    gate: architecture
  - id: implementation
    type: agent
    capability: coding
    profile: codex
    mode: workspace-write
    save_as: ai/implementation.md
""",
    )

    orchestrator = Orchestrator(tmp_path, agent_runtime=FakeRuntime())
    with pytest.raises(RuntimeError, match="does not allow --force bypass"):
        orchestrator.run_manual_step(
            "POL-1", "policy-write", "implementation", force=True
        )

    grant_approval(
        orchestrator.context("POL-1"), "architecture", approved_by="architect@example.com"
    )
    execution = orchestrator.run_manual_step(
        "POL-1", "policy-write", "implementation"
    )
    assert execution.status == "completed"


def test_enterprise_policy_rejects_workspace_write_workflow_without_prior_gate(
    tmp_path: Path, monkeypatch
):
    init_project(tmp_path)
    config_path = tmp_path / ".sdai" / "config.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["operating_mode"] = "enterprise"
    write_text(config_path, yaml.safe_dump(config, sort_keys=False))

    org_policy = tmp_path.parent / f"{tmp_path.name}-org-gate.yaml"
    write_text(
        org_policy,
        """version: 1
execution:
  require_prior_approval_for_workspace_write: true
""",
    )
    monkeypatch.setenv("SDAI_ORG_POLICY_PATH", str(org_policy.resolve()))
    write_text(tmp_path / "specs" / "POL-2" / "00-intake.md", "# Intake\n")
    write_text(
        tmp_path / ".sdai" / "workflows" / "no-gate.yaml",
        """version: 5
name: no-gate
validation_mode: light
steps:
  - id: implementation
    type: agent
    capability: coding
    profile: codex
    mode: workspace-write
""",
    )
    orchestrator = Orchestrator(tmp_path, agent_runtime=FakeRuntime())
    with pytest.raises(RuntimeError, match="defines none"):
        orchestrator.run_manual_step("POL-2", "no-gate", "implementation")
