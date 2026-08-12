from __future__ import annotations

from pathlib import Path

from sdai.evals import EvalExecution, EvalExecutionRequest, run_behavioral_eval
from sdai.extensions.scaffolding import ScaffoldKind, create_extension_scaffold


class _CaptureExecutor:
    def __init__(self) -> None:
        self.candidate_content = ""

    def execute(self, request: EvalExecutionRequest) -> EvalExecution:
        if request.phase == "candidate":
            self.candidate_content = request.target_content
            output = "BLOCK: required review discipline applies."
        else:
            output = "approve as-is"
        return EvalExecution("test-provider", "model-x", output)


def _write_agent_eval(root: Path, agent: str) -> None:
    path = root / ".sdai" / "evals" / "agents" / agent / "review.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        """version: 1
id: attached-skill-review
description: Attached skill must participate in agent behavior identity.
required: true
prompt: Review a change that violates a required review discipline.
assertions:
  must:
    - id: BLOCK_REQUIRED
      contains: BLOCK
  must_not:
    - id: NO_BLIND_APPROVAL
      contains: approve as-is
mock:
  baseline: "approve as-is"
  candidate: "BLOCK: required review discipline applies."
""",
        encoding="utf-8",
    )


def test_agent_eval_content_and_hash_include_attached_skill_instructions(
    tmp_path: Path,
) -> None:
    create_extension_scaffold(tmp_path, ScaffoldKind.SKILL, "review-discipline")
    create_extension_scaffold(tmp_path, ScaffoldKind.AGENT, "code-reviewer")

    agent_path = tmp_path / ".sdai" / "agents" / "code-reviewer.agent.md"
    agent_text = agent_path.read_text(encoding="utf-8")
    agent_path.write_text(
        agent_text.replace("skills: []", "skills:\n  - review-discipline"),
        encoding="utf-8",
    )
    _write_agent_eval(tmp_path, "code-reviewer")

    first_executor = _CaptureExecutor()
    first = run_behavioral_eval(
        tmp_path,
        "agent",
        "code-reviewer",
        executor=first_executor,
    )

    assert "## Agent Instructions" in first_executor.candidate_content
    assert "## Skill: review-discipline" in first_executor.candidate_content
    assert "engineering technique" in first_executor.candidate_content

    skill_path = tmp_path / ".agents" / "skills" / "review-discipline" / "SKILL.md"
    skill_path.write_text(
        skill_path.read_text(encoding="utf-8")
        + "\nNever approve missing required verification evidence.\n",
        encoding="utf-8",
    )

    second_executor = _CaptureExecutor()
    second = run_behavioral_eval(
        tmp_path,
        "agent",
        "code-reviewer",
        executor=second_executor,
    )

    assert first.target_sha256 != second.target_sha256
    assert "Never approve missing required verification evidence" in (
        second_executor.candidate_content
    )
