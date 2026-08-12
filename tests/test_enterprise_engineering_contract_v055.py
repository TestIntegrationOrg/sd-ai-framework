from pathlib import Path

from sdai.agent_platform import AgentRuntime, Capability
from sdai.architecture_skills import _skill_markdown
from sdai.artifacts import write_text
from sdai.scaffold import init_project
from sdai.text import read_utf8_text
from sdai.v05_scaffold import (
    AGENTS,
    AGENTS_V054,
    BASE_SKILLS_V054,
    REQUIREMENTS_PROMPT,
    REQUIREMENTS_PROMPT_V054,
    install_v05_scaffold,
)


def _write_feature(root: Path, feature_id: str = "ENG-1") -> None:
    write_text(
        root / "specs" / feature_id / "00-intake.md",
        """# Feature Intake — ENG-1

## Title
Sign scripts

## Description
Accept an unsigned PowerShell script, sign it, and return the signed script.
""",
    )


def test_engineering_judgment_is_composed_for_requirements_and_architecture(tmp_path: Path):
    init_project(tmp_path)
    install_v05_scaffold(tmp_path)
    _write_feature(tmp_path)

    requirements = AgentRuntime(tmp_path).build_invocation("ENG-1", Capability.REQUIREMENTS)
    architecture = AgentRuntime(tmp_path).build_invocation("ENG-1", Capability.ARCHITECTURE)

    assert "## Skill: engineering-judgment" in requirements.system
    assert "## Skill: engineering-judgment" in architecture.system
    assert "Known" in requirements.system
    assert "Proposed" in requirements.system
    assert "Assumption" in requirements.system
    assert "Open question" in requirements.system
    assert "Blocker" in requirements.system
    assert "Do not turn every uncertainty into a blocker" in requirements.prompt


def test_all_semantic_agents_receive_engineering_judgment():
    assert AGENTS
    assert all("engineering-judgment" in content for content in AGENTS.values())


def test_upgrade_replaces_only_stock_requirements_contract(tmp_path: Path):
    init_project(tmp_path)

    # Simulate a project that already has the exact stock v0.5.4 semantic agent and skill.
    write_text(
        tmp_path / ".sdai" / "agents" / "requirements-analyst.agent.md",
        AGENTS_V054["requirements-analyst"],
    )
    old_skill = BASE_SKILLS_V054["requirements-analysis"]
    write_text(
        tmp_path / ".agents" / "skills" / "requirements-analysis" / "SKILL.md",
        _skill_markdown(
            "requirements-analysis",
            str(old_skill["description"]),
            str(old_skill["instructions"]),
        ),
    )

    assert read_utf8_text(tmp_path / ".sdai" / "prompts" / "requirements.md") == REQUIREMENTS_PROMPT_V054

    install_v05_scaffold(tmp_path)

    assert read_utf8_text(tmp_path / ".sdai" / "prompts" / "requirements.md") == REQUIREMENTS_PROMPT
    assert read_utf8_text(tmp_path / ".sdai" / "agents" / "requirements-analyst.agent.md") == AGENTS["requirements-analyst"]
    assert (tmp_path / ".agents" / "skills" / "engineering-judgment" / "SKILL.md").exists()
    upgraded_skill = read_utf8_text(
        tmp_path / ".agents" / "skills" / "requirements-analysis" / "SKILL.md"
    )
    assert "Do not stop at “requirements are incomplete”" in upgraded_skill


def test_upgrade_preserves_customized_requirements_agent_and_prompt(tmp_path: Path):
    init_project(tmp_path)
    custom_agent = AGENTS_V054["requirements-analyst"] + "\nCustom team rule: preserve this line.\n"
    custom_prompt = REQUIREMENTS_PROMPT_V054 + "\nCustom organization prompt rule.\n"

    write_text(
        tmp_path / ".sdai" / "agents" / "requirements-analyst.agent.md",
        custom_agent,
    )
    write_text(
        tmp_path / ".sdai" / "prompts" / "requirements.md",
        custom_prompt,
        overwrite=True,
    )

    install_v05_scaffold(tmp_path)

    assert read_utf8_text(tmp_path / ".sdai" / "agents" / "requirements-analyst.agent.md") == custom_agent
    assert read_utf8_text(tmp_path / ".sdai" / "prompts" / "requirements.md") == custom_prompt
