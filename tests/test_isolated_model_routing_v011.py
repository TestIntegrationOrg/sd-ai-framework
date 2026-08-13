from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import subprocess

from sdai.agent_platform import AgentRuntime, Capability, ExecutionMode
from sdai.isolated_routing import build_routed_isolated_invocation, routing_request_for_isolated_contract
from sdai.isolated_tasks import IsolatedContextSlice, IsolatedStage, IsolatedTaskContract
from sdai.scaffold import init_project
from sdai.v05_scaffold import install_v05_scaffold


FEATURE = "ROUTED-ISOLATED-123"


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=root, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        encoding="utf-8", check=True, shell=False,
    )
    return completed.stdout.strip()


def test_isolated_routing_changes_profile_not_semantic_agent(tmp_path: Path) -> None:
    root = tmp_path / "routed isolated Ω"
    root.mkdir()
    init_project(root)
    install_v05_scaffold(root)
    agents = """version: 1
profiles:
  economical:
    provider: provider-a
    capabilities: [coding]
    prompt: auto
    cost_class: economy
    routing_tier: advanced
    technologies: [python]
  premium:
    provider: provider-b
    capabilities: [coding]
    prompt: auto
    cost_class: premium
    routing_tier: advanced
    technologies: [python]
"""
    (root / ".sdai" / "agents.yaml").write_text(agents, encoding="utf-8", newline="\n")
    (root / ".sdai" / "routing.yaml").write_text("version: 1\nroutes: {}\n", encoding="utf-8", newline="\n")
    requirements = root / "specs" / "changes" / FEATURE / "requirements.md"
    requirements.parent.mkdir(parents=True)
    requirements.write_text("# Requirements\n\n- FR-001: Preserve café behavior.\n", encoding="utf-8", newline="\n")
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "SDAI Routing Test")
    _git(root, "config", "user.email", "sdai@example.test")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "baseline")
    head = _git(root, "rev-parse", "HEAD")
    raw = requirements.read_bytes()
    context = IsolatedContextSlice(
        source=f"specs/changes/{FEATURE}/requirements.md",
        line_start=1,
        line_end=3,
        source_sha256="sha256:" + sha256(raw).hexdigest(),
        text="# Requirements\n\n- FR-001: Preserve café behavior.",
    )
    contract = IsolatedTaskContract(
        feature_id=FEATURE,
        task_id="REMEDIATE-ROUTING-123",
        remediation_task_sha256="sha256:" + "1" * 64,
        round_id="ROUND-ROUTING-123",
        attempt=1,
        stage=IsolatedStage.IMPLEMENT,
        git_commit=head,
        dispatch_id="dispatch-routing-123",
        semantic_agent="developer",
        capability=Capability.CODING,
        mode=ExecutionMode.WORKSPACE_WRITE,
        summary="Implement FR-001.",
        allowed_roots=("src", "tests"),
        forbidden_roots=(f"specs/changes/{FEATURE}/requirements.md", "specs/current"),
        context=(context,),
    )
    request = routing_request_for_isolated_contract(
        contract,
        risk="standard",
        complexity="medium",
        affected_technologies=("python",),
        provider_availability={"provider-a": True, "provider-b": True},
    )

    routed = build_routed_isolated_invocation(AgentRuntime(root), contract, request, environ={})

    assert routed.decision.selected_profile == "economical"
    assert routed.prepared.record.semantic_agent == "developer"
    assert routed.prepared.invocation.agent_name == "developer"
    assert routed.prepared.invocation.profile.name == "economical"
    payload = json.loads(routed.prepared.invocation.routing_decision or "{}")
    assert payload["request"]["semantic_role"] == "developer"
    assert payload["selected_profile"] == "economical"
