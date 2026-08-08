from pathlib import Path

import yaml

from sdai.agent_platform.context import collect_feature_context
from sdai.architecture_skills import ARCHITECT_V051, ARCHITECT_V052, ARCHITECT_V053, SKILLS as ARCHITECTURE_SKILLS
from sdai.models import FeatureContext
from sdai.scaffold import init_project
from sdai.v05_scaffold import install_v05_scaffold


SPECIALIZED = {
    "rfc-authoring",
    "adr-authoring",
    "c4-modeling",
    "drawio-architecture",
    "plantuml-sequence",
    "api-contract-design",
    "threat-modeling",
}


def _frontmatter(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    _, raw, _ = text.split("---", 2)
    return yaml.safe_load(raw)


def _project(tmp_path: Path) -> Path:
    init_project(tmp_path)
    install_v05_scaffold(tmp_path)
    return tmp_path


def test_v05_installer_adds_architecture_skill_pack_and_enhanced_architect(tmp_path: Path):
    _project(tmp_path)

    for name in SPECIALIZED | {"architecture-design", "architecture-review"}:
        root = tmp_path / ".agents" / "skills" / name
        assert (root / "SKILL.md").is_file()
        assert (root / "sdai.yaml").is_file()
        assert _frontmatter(root / "SKILL.md")["name"] == name
        metadata = yaml.safe_load((root / "sdai.yaml").read_text(encoding="utf-8"))
        assert metadata["version"] == 1
        assert "architecture" in metadata["capabilities"]

    architect = (tmp_path / ".sdai" / "agents" / "architect.agent.md").read_text(encoding="utf-8")
    for name in SPECIALIZED:
        assert name in architect
    assert "profile: claude" in architect
    assert "execution_mode: advisory" in architect


def test_installer_is_idempotent_and_preserves_custom_skill(tmp_path: Path):
    _project(tmp_path)
    custom = tmp_path / ".agents" / "skills" / "rfc-authoring" / "SKILL.md"
    custom.write_text("---\nname: rfc-authoring\ndescription: custom\n---\n# Team RFC\n", encoding="utf-8")

    created = install_v05_scaffold(tmp_path)

    assert custom.read_text(encoding="utf-8").endswith("# Team RFC\n")
    assert created == []


def test_upgrade_only_rewrites_exact_stock_v051_architect(tmp_path: Path):
    init_project(tmp_path)
    architect = tmp_path / ".sdai" / "agents" / "architect.agent.md"
    architect.parent.mkdir(parents=True)
    architect.write_text(ARCHITECT_V051, encoding="utf-8")

    install_v05_scaffold(tmp_path)
    assert architect.read_text(encoding="utf-8").strip() == ARCHITECT_V053.strip()

    architect.write_text("---\nname: architect\n---\n# Team customized architect\n", encoding="utf-8")
    install_v05_scaffold(tmp_path)
    assert "Team customized architect" in architect.read_text(encoding="utf-8")



def test_upgrade_rewrites_exact_stock_v052_architect(tmp_path: Path):
    init_project(tmp_path)
    architect = tmp_path / ".sdai" / "agents" / "architect.agent.md"
    architect.parent.mkdir(parents=True)
    architect.write_text(ARCHITECT_V052, encoding="utf-8")

    install_v05_scaffold(tmp_path)
    assert architect.read_text(encoding="utf-8").strip() == ARCHITECT_V053.strip()
    assert "architecture-validation.yaml" in architect.read_text(encoding="utf-8")

def test_existing_project_gets_new_skills_without_overwriting_custom_architect(tmp_path: Path):
    init_project(tmp_path)
    architect = tmp_path / ".sdai" / "agents" / "architect.agent.md"
    architect.parent.mkdir(parents=True)
    architect.write_text("---\nname: architect\n---\n# Company architect\n", encoding="utf-8")

    install_v05_scaffold(tmp_path)

    assert "Company architect" in architect.read_text(encoding="utf-8")
    assert (tmp_path / ".agents" / "skills" / "drawio-architecture" / "SKILL.md").exists()
    assert (tmp_path / ".agents" / "skills" / "plantuml-sequence" / "SKILL.md").exists()


def test_specialized_skill_contracts_are_explicit():
    assert "<mxfile><diagram><mxGraphModel>" in str(ARCHITECTURE_SKILLS["drawio-architecture"]["instructions"])
    assert "@startuml" in str(ARCHITECTURE_SKILLS["plantuml-sequence"]["instructions"])
    assert "@enduml" in str(ARCHITECTURE_SKILLS["plantuml-sequence"]["instructions"])
    assert "Draft" in str(ARCHITECTURE_SKILLS["rfc-authoring"]["instructions"])
    assert "Proposed" in str(ARCHITECTURE_SKILLS["adr-authoring"]["instructions"])
    assert "OpenAPI" in str(ARCHITECTURE_SKILLS["api-contract-design"]["instructions"])
    assert "trust" in str(ARCHITECTURE_SKILLS["threat-modeling"]["instructions"]).lower()


def test_architecture_artifacts_are_included_in_downstream_context(tmp_path: Path):
    context = FeatureContext(tmp_path, "ARCH-101")
    context.feature_dir.mkdir(parents=True)
    (context.feature_dir / "rfc").mkdir()
    (context.feature_dir / "architecture" / "diagrams").mkdir(parents=True)
    (context.feature_dir / "contracts").mkdir()
    (context.feature_dir / "security").mkdir()

    (context.feature_dir / "rfc" / "RFC-001-retry.md").write_text("# Retry RFC\n", encoding="utf-8")
    (context.feature_dir / "architecture" / "diagrams" / "retry-sequence.puml").write_text(
        "@startuml\nA -> B: retry\n@enduml\n", encoding="utf-8"
    )
    (context.feature_dir / "architecture" / "diagrams" / "deployment.drawio").write_text(
        "<mxfile><diagram><mxGraphModel/></diagram></mxfile>\n", encoding="utf-8"
    )
    (context.feature_dir / "contracts" / "openapi.yaml").write_text("openapi: 3.1.0\n", encoding="utf-8")
    (context.feature_dir / "security" / "threat-model.md").write_text("# Threat model\n", encoding="utf-8")

    collected = collect_feature_context(context)

    assert "rfc/RFC-001-retry.md" in collected
    assert "architecture/diagrams/retry-sequence.puml" in collected
    assert "contracts/openapi.yaml" in collected
    assert "security/threat-model.md" in collected
    # Draw.io XML is intentionally not injected into every downstream prompt.
    assert "deployment.drawio" not in collected
