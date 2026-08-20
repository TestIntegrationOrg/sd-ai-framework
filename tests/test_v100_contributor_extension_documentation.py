from __future__ import annotations

from pathlib import Path

from sdai.extensions import ExtensionKind
from sdai.extensions.scaffolding import ScaffoldKind


REPO_ROOT = Path(__file__).resolve().parents[1]
CONTRIBUTING = REPO_ROOT / "CONTRIBUTING.md"
AUTHORING = REPO_ROOT / "docs" / "EXTENSION-AUTHORING.md"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_readme_links_contributor_and_extension_author_guidance() -> None:
    readme = _read(REPO_ROOT / "README.md")

    assert "[Contributing to SDAI](CONTRIBUTING.md)" in readme
    assert "[Extension authoring guide](docs/EXTENSION-AUTHORING.md)" in readme
    assert CONTRIBUTING.is_file()
    assert AUTHORING.is_file()


def test_contributor_guide_pins_supported_validation_and_contract_boundaries() -> None:
    text = _read(CONTRIBUTING)

    for marker in (
        "Python 3.11 and 3.12",
        "Ubuntu, Windows, and macOS",
        "python -m pip install -e '.[dev]'",
        "python -m pytest -q",
        "python tests/package_install_smoke.py",
        "src/sdai/__init__.py::__version__",
        "sdai/v1",
        "sdai.extensions",
        "docs/JSON-CONTRACTS.md",
        "executable-and-argument arrays",
        "0.18/#25 identity-backed approval capability remains held",
    ):
        assert marker in text


def test_extension_author_guide_covers_every_stable_and_scaffold_kind() -> None:
    text = _read(AUTHORING)

    for kind in ExtensionKind:
        assert f"`{kind.value}`" in text
    for kind in ScaffoldKind:
        assert f"sdai create {kind.value} " in text


def test_extension_author_guide_preserves_authority_and_testing_contracts() -> None:
    text = _read(AUTHORING)

    for marker in (
        "apiVersion: sdai/v1",
        ".sdai/agents/*.agent.md",
        ".agents/skills/<name>/SKILL.md",
        "builtin(0) < pack(10) < org(20) < repo(30) < user(40)",
        "Only `builtin` and `org` may declare authoritative locks",
        "load_extension_manifest(project_root, path)",
        "sdai.extension-contract/v1",
        "`Validator` and `QualityGate` scaffolds are registry-only",
        "executable quality gates in `.sdai/quality-gates.yaml`",
        "sdai.integration-manifest/v1",
        "sdai.pack-manifest/v1",
        "sdai agents doctor",
        "tests/test_extension_manifests_v06.py",
        "docs/examples/integrations/custom-cli.integration.yaml",
        "0.18/#25 identity-backed approvals remain held",
    ):
        assert marker in text
    assert "sdai providers doctor" not in text


def test_documented_extension_references_exist() -> None:
    for relative in (
        "docs/EXTENSIONS.md",
        "docs/SKILLS.md",
        "docs/AGENT-FILES.md",
        "docs/WORKFLOWS.md",
        "docs/WORKFLOW-COMPONENTS.md",
        "docs/ARTIFACT-SCHEMAS.md",
        "docs/PLUGIN-STEP-REGISTRY-V2.md",
        "docs/ENTERPRISE.md",
        "docs/INTEGRATION-MANIFEST.md",
        "docs/PACK-MANIFEST.md",
        "docs/PROVIDERS.md",
        "docs/examples/integrations/custom-cli.integration.yaml",
        "src/sdai/builtin_integrations",
    ):
        assert (REPO_ROOT / relative).exists(), relative
