from __future__ import annotations

from pathlib import Path
import shutil

import pytest
import yaml

from sdai.agent_platform.models import Capability
from sdai.evals import MockEvalExecutor, run_behavioral_eval
from sdai.execution_excellence import (
    EXECUTION_EXCELLENCE_SKILLS,
    ExecutionExcellenceError,
    load_execution_excellence_pack,
)
from sdai.policy import load_effective_configuration
from sdai.skill_resolution import resolve_skills
from sdai.workflows import StepKind, load_workflow


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _init(root: Path, *, policy: bool = False) -> None:
    path = root / ".sdai" / "config.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {"version": 1, "operating_mode": "individual"}
    if policy:
        payload["policy"] = {"repository": ".sdai/policy.yaml"}
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def _agent(root: Path, name: str, capability: str) -> None:
    path = root / ".sdai" / "agents" / f"{name}.agent.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        + yaml.safe_dump(
            {
                "name": name,
                "description": f"Provider-neutral {name} role.",
                "capabilities": [capability],
                "skills": [],
                "execution_mode": "advisory",
                "providers": {},
            },
            sort_keys=False,
        )
        + "---\n\nOperate within the assigned semantic responsibility.\n",
        encoding="utf-8",
    )


def _copy_skills(root: Path) -> None:
    source = _repo_root()
    for name in EXECUTION_EXCELLENCE_SKILLS:
        src = source / ".agents" / "skills" / name
        dst = root / ".agents" / "skills" / name
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, dst)


def _copy_pack(root: Path) -> None:
    source = _repo_root()
    manifest = root / ".sdai" / "extensions" / "packs" / "sdai-execution-excellence.yaml"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(
        source / ".sdai" / "extensions" / "packs" / "sdai-execution-excellence.yaml",
        manifest,
    )
    for relative in (
        Path("examples/workflows/execution-excellence.yaml"),
        Path("examples/policies/execution-excellence.yaml"),
    ):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / relative, target)


def test_pack_is_valid_provider_neutral_and_all_behavioral_evals_improve() -> None:
    root = _repo_root()

    pack = load_execution_excellence_pack(root)

    assert pack.id == "sdai-execution-excellence"
    assert pack.version == "0.1.0"
    assert pack.skills == EXECUTION_EXCELLENCE_SKILLS
    assert pack.workflow_examples == ("examples/workflows/execution-excellence.yaml",)
    assert pack.policy_examples == ("examples/policies/execution-excellence.yaml",)
    assert "\\" not in pack.source

    for name in pack.skills:
        report = run_behavioral_eval(
            root,
            "skill",
            name,
            executor=MockEvalExecutor(),
            require_improvement=True,
        )
        assert report.passed is True, name
        assert report.candidate_score > report.baseline_score, name


def test_task_context_selects_only_relevant_execution_disciplines(tmp_path: Path) -> None:
    _init(tmp_path)
    _copy_skills(tmp_path)
    for name, capability in (
        ("planner", "planning"),
        ("developer", "coding"),
        ("code-reviewer", "review"),
    ):
        _agent(tmp_path, name, capability)

    planning = resolve_skills(
        tmp_path,
        agent_name="planner",
        capability="planning",
        task="plan implementation migration",
    )
    implementation = resolve_skills(
        tmp_path,
        agent_name="developer",
        capability="coding",
        task="implement feature change",
    )
    debugging = resolve_skills(
        tmp_path,
        agent_name="developer",
        capability="coding",
        task="debug failing regression",
    )
    review = resolve_skills(
        tmp_path,
        agent_name="code-reviewer",
        capability="review",
        task="review completion evidence",
    )

    assert planning.selected == ("implementation-planning",)
    assert implementation.selected == ("test-driven-development",)
    assert debugging.selected == ("systematic-debugging",)
    assert review.selected == ("verification-before-completion",)
    assert all(
        not name.startswith(("java-", "python-", "codex-", "claude-"))
        for name in (planning.agent, implementation.agent, debugging.agent, review.agent)
    )


def test_policy_mandatory_skills_union_with_task_specific_auto_selection(tmp_path: Path) -> None:
    _init(tmp_path, policy=True)
    _copy_skills(tmp_path)
    _agent(tmp_path, "developer", "coding")
    shutil.copyfile(
        _repo_root() / "examples" / "policies" / "execution-excellence.yaml",
        tmp_path / ".sdai" / "policy.yaml",
    )

    policy = load_effective_configuration(tmp_path)
    report = resolve_skills(
        tmp_path,
        agent_name="developer",
        capability="coding",
        task="debug failing regression",
    )

    assert policy.required_skills(Capability.CODING) == (
        "test-driven-development",
        "verification-before-completion",
    )
    assert report.policy_required == (
        "test-driven-development",
        "verification-before-completion",
    )
    assert report.selected == (
        "test-driven-development",
        "verification-before-completion",
        "systematic-debugging",
    )


def test_workflow_example_loads_through_public_v5_workflow_parser(tmp_path: Path) -> None:
    _init(tmp_path)
    source = _repo_root() / "examples" / "workflows" / "execution-excellence.yaml"
    target = tmp_path / ".sdai" / "workflows" / "execution-excellence.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)

    workflow = load_workflow(tmp_path, "execution-excellence")

    assert workflow.name == "execution-excellence"
    assert [step.id for step in workflow.steps] == [
        "implementation-plan",
        "implementation",
        "code-review",
        "test-review",
        "validate",
    ]
    assert [step.agent_name for step in workflow.steps[:-1]] == [
        "planner",
        "developer",
        "code-reviewer",
        "tester",
    ]
    assert all(step.profile is None for step in workflow.steps)
    assert workflow.steps[-1].kind is StepKind.VALIDATE


def test_validator_rejects_provider_pinned_workflow_example(tmp_path: Path) -> None:
    _init(tmp_path)
    _copy_skills(tmp_path)
    _copy_pack(tmp_path)
    workflow = tmp_path / "examples" / "workflows" / "execution-excellence.yaml"
    payload = yaml.safe_load(workflow.read_text(encoding="utf-8"))
    payload["steps"][1]["profile"] = "codex"
    workflow.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")

    with pytest.raises(
        ExecutionExcellenceError,
        match="SDAI-EXEC-005.*must not pin an AI provider profile",
    ):
        load_execution_excellence_pack(tmp_path)


def test_validator_rejects_malformed_or_weakened_policy_example(tmp_path: Path) -> None:
    _init(tmp_path)
    _copy_skills(tmp_path)
    _copy_pack(tmp_path)
    policy = tmp_path / "examples" / "policies" / "execution-excellence.yaml"
    payload = yaml.safe_load(policy.read_text(encoding="utf-8"))
    payload["skills"]["required"]["coding"] = "verification-before-completion"
    policy.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")

    with pytest.raises(
        ExecutionExcellenceError,
        match="SDAI-EXEC-001.*skills.required.coding must be a non-empty string list",
    ):
        load_execution_excellence_pack(tmp_path)

    shutil.copyfile(
        _repo_root() / "examples" / "policies" / "execution-excellence.yaml",
        policy,
    )
    payload = yaml.safe_load(policy.read_text(encoding="utf-8"))
    payload["skills"]["required"]["coding"] = ["verification-before-completion"]
    policy.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")

    with pytest.raises(
        ExecutionExcellenceError,
        match="SDAI-EXEC-004.*coding.*test-driven-development",
    ):
        load_execution_excellence_pack(tmp_path)
