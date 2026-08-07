from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import threading

import pytest
import yaml

from sdai.agent_platform import Capability, ExecutionMode
from sdai.agent_platform.models import AgentExecutionResult
from sdai.artifacts import write_text
from sdai.conditions import evaluate_condition
from sdai.enterprise_scaffold import install_v04_scaffold
from sdai.governance import GovernanceError, check_workflow_governance
from sdai.integrations.github import GitHubCli, PullRequestRequest
from sdai.integrations.jira import JiraClient
from sdai.orchestrator import Orchestrator
from sdai.quality_gates import QualityGateResult, QualityGateRunner
from sdai.scaffold import init_project
from sdai.workflow_templates import install_current_workflows
from sdai.workflows import grant_approval, is_approved, load_workflow, load_workflow_state


def _project(root: Path, feature_id: str = "ENT-1") -> None:
    init_project(root)
    install_v04_scaffold(root)
    install_current_workflows(root)
    write_text(
        root / "specs" / feature_id / "00-intake.md",
        f"# Feature Intake — {feature_id}\n\n## Title\nEnterprise\n\n## Description\nTest v0.4.\n",
    )


def test_condition_dsl_is_safe_and_feature_aware(tmp_path: Path):
    _project(tmp_path)
    context = Orchestrator(tmp_path).context("ENT-1")
    assert evaluate_condition("always", context=context, workflow="standard").matched
    assert not evaluate_condition("never", context=context, workflow="standard").matched
    assert evaluate_condition(
        "env:FLAG=on", context=context, workflow="standard", environ={"FLAG": "on"}
    ).matched
    assert evaluate_condition(
        "artifact:00-intake.md", context=context, workflow="standard"
    ).matched
    assert not evaluate_condition(
        "not:artifact:00-intake.md", context=context, workflow="standard"
    ).matched


def test_role_backed_approval_preserves_legacy_gate(tmp_path: Path):
    _project(tmp_path)
    context = Orchestrator(tmp_path).context("ENT-1")

    legacy = grant_approval(context, "architecture", approved_by="legacy@example.com")
    assert legacy.satisfied
    assert is_approved(context, "architecture")

    with pytest.raises(GovernanceError):
        grant_approval(context, "enterprise-architecture", approved_by="a@example.com")

    enterprise = grant_approval(
        context,
        "enterprise-architecture",
        approved_by="a@example.com",
        role="architect",
    )
    assert enterprise.satisfied
    assert is_approved(context, "enterprise-architecture")


def test_quality_gate_runner_persists_report(tmp_path: Path):
    _project(tmp_path)
    config = {
        "version": 1,
        "gates": {
            "smoke": {
                "enabled": True,
                "command": [sys.executable, "-c", "print('gate-ok')"],
                "success_exit_codes": [0],
                "timeout_seconds": 10,
            }
        },
    }
    write_text(
        tmp_path / ".sdai" / "quality-gates.yaml",
        yaml.safe_dump(config, sort_keys=False),
    )
    context = Orchestrator(tmp_path).context("ENT-1")
    result = QualityGateRunner(tmp_path).run("smoke", context=context)
    assert result.passed
    assert "gate-ok" in result.output
    assert context.artifact("quality-gates/smoke.md").exists()


class FakeRuntime:
    def __init__(self):
        self.calls: dict[str, int] = {}
        self.lock = threading.Lock()

    def execute(self, feature_id, capability, *, profile_name=None, mode=ExecutionMode.ADVISORY):
        key = profile_name or capability.value
        with self.lock:
            self.calls[key] = self.calls.get(key, 0) + 1
            call = self.calls[key]
        if key == "retry-agent" and call == 1:
            raise RuntimeError("transient")
        return AgentExecutionResult(
            feature_id=feature_id,
            capability=capability,
            profile=key,
            provider="fake",
            output=f"{key}-output-{call}",
            prompt="test",
            skills=(),
        )

    def build_invocation(self, *args, **kwargs):  # pragma: no cover - not used here
        raise AssertionError("not expected")


class AlwaysFailGateRunner:
    def run(self, name, *, context=None):
        return QualityGateResult(
            name=name,
            command=("fake",),
            return_code=1,
            passed=False,
            output="failed",
            artifact=None,
        )


def test_retry_parallel_conditions_and_continue_failure(tmp_path: Path):
    _project(tmp_path)
    write_text(
        tmp_path / ".sdai" / "workflows" / "advanced.yaml",
        """version: 4
name: advanced
validation_mode: light
steps:
  - id: conditional
    type: agent
    capability: review
    profile: never-agent
    mode: advisory
    if: env:RUN_NEVER

  - id: retry-review
    type: agent
    capability: review
    profile: retry-agent
    mode: advisory
    retry:
      max_attempts: 2
      delay_seconds: 0

  - id: parallel-reviews
    type: parallel
    steps:
      - id: arch
        type: agent
        capability: architecture
        profile: architect-a
        mode: advisory
        save_as: ai/parallel-arch.md
      - id: sec
        type: agent
        capability: security
        profile: security-b
        mode: advisory
        save_as: ai/parallel-security.md

  - id: non-blocking-gate
    type: quality-gate
    gate: fake
    on_failure: continue

  - id: implement-brief
    type: deterministic
    action: implement
""",
    )
    runtime = FakeRuntime()
    orchestrator = Orchestrator(
        tmp_path,
        agent_runtime=runtime,
        quality_gate_runner=AlwaysFailGateRunner(),
        sleeper=lambda _: None,
    )
    results = orchestrator.run_workflow("ENT-1", "advanced")
    by_id = {item.step_id: item for item in results}
    assert by_id["conditional"].status == "condition-skipped"
    assert by_id["retry-review"].status == "completed"
    assert by_id["retry-review"].attempts == 2
    assert by_id["parallel-reviews"].status == "completed"
    assert len(by_id["parallel-reviews"].result) == 2
    assert by_id["non-blocking-gate"].status == "failed"
    assert by_id["implement-brief"].status == "completed"
    assert runtime.calls["retry-agent"] == 2
    assert Orchestrator(tmp_path).context("ENT-1").artifact("ai/parallel-arch.md").exists()


def test_enterprise_workflow_schema_and_policy_check(tmp_path: Path):
    _project(tmp_path)
    definition = load_workflow(tmp_path, "enterprise")
    assert definition.step("design-reviews").kind.value == "parallel"
    assert definition.step("tests").kind.value == "quality-gate"
    assert definition.step("trivy").condition == "env:SDAI_TRIVY"
    assert definition.step("implementation").retry.max_attempts == 2
    assert check_workflow_governance(tmp_path, definition) == []


def test_force_rerun_still_invalidates_downstream_state(tmp_path: Path):
    _project(tmp_path)
    write_text(
        tmp_path / ".sdai" / "workflows" / "invalidate.yaml",
        """version: 4
name: invalidate
validation_mode: standard
steps:
  - id: specification
    type: deterministic
    action: specify
  - id: architecture
    type: deterministic
    action: architect
""",
    )
    orchestrator = Orchestrator(tmp_path)
    orchestrator.run_workflow("ENT-1", "invalidate")
    state = load_workflow_state(orchestrator.context("ENT-1"), "invalidate")
    assert state.completed_steps == ["specification", "architecture"]
    orchestrator.run_manual_step("ENT-1", "invalidate", "specification", force=True)
    state = load_workflow_state(orchestrator.context("ENT-1"), "invalidate")
    assert "architecture" not in state.completed_steps


def test_github_cli_adapter_composes_issue_and_pr_commands(tmp_path: Path):
    calls: list[list[str]] = []

    def runner(command: list[str], cwd: Path | None):
        calls.append(command)
        if command[1:3] == ["issue", "view"]:
            payload = {
                "number": 7,
                "title": "Feature",
                "body": "Body",
                "url": "https://example.invalid/issues/7",
                "labels": [{"name": "feature"}],
                "assignees": [{"login": "dev"}],
            }
            return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")
        return subprocess.CompletedProcess(command, 0, "https://example.invalid/pr/8\n", "")

    client = GitHubCli(cwd=tmp_path, runner=runner)
    issue = client.issue("acme/repo", 7)
    assert issue.title == "Feature"
    assert issue.labels == ("feature",)

    url = client.create_pull_request(
        PullRequestRequest(
            repository="acme/repo",
            base="main",
            head="feature",
            title="Feature",
            body="Body",
            draft=True,
        )
    )
    assert url.endswith("/pr/8")
    assert calls[0][0:3] == ["gh", "issue", "view"]
    assert "--draft" in calls[1]


def test_jira_adapter_parses_adf_and_uses_environment_safe_auth():
    captured = []

    def transport(request):
        captured.append(request)
        payload = {
            "key": "PROJ-12",
            "fields": {
                "summary": "Jira feature",
                "description": {
                    "type": "doc",
                    "content": [
                        {
                            "type": "paragraph",
                            "content": [{"type": "text", "text": "Build it"}],
                        }
                    ],
                },
                "status": {"name": "Open"},
                "issuetype": {"name": "Story"},
                "priority": {"name": "High"},
                "labels": ["ai"],
            },
        }
        return json.dumps(payload).encode("utf-8")

    client = JiraClient(
        "https://jira.example.invalid",
        email="user@example.com",
        api_token="secret-token",
        transport=transport,
    )
    issue = client.issue("PROJ-12")
    assert issue.summary == "Jira feature"
    assert "Build it" in issue.description
    assert issue.priority == "High"
    assert captured[0].get_header("Authorization").startswith("Basic ")
