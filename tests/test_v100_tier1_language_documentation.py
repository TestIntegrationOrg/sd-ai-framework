from __future__ import annotations

from pathlib import Path

from sdai.language_packs import TIER1_LANGUAGE_PACK_IDS, validate_tier1_language_packs


ROOT = Path(__file__).resolve().parents[1]
DOC_PATH = ROOT / "docs" / "TIER1-LANGUAGE-PACKS.md"

PACK_GOVERNANCE_DOCS = (
    "PACK-MANIFEST.md",
    "PACK-INTEGRITY.md",
    "PACK-LOCK.md",
    "PACK-LIFECYCLE.md",
    "PACK-CATALOG-POLICY.md",
    "PACK-CERTIFICATION.md",
)

EXPECTED_TIER1_SKILLS = {
    "sdai-java": ("java-engineering", "spring-boot"),
    "sdai-dotnet": ("csharp-engineering", "aspnet-core"),
    "sdai-python": ("python-engineering", "fastapi", "django"),
    "sdai-typescript-javascript": (
        "javascript-engineering",
        "typescript-engineering",
        "nodejs-engineering",
        "react-engineering",
        "angular-engineering",
    ),
    "sdai-go": ("go-engineering",),
    "sdai-powershell": ("powershell-engineering",),
}


def _doc() -> str:
    return DOC_PATH.read_text(encoding="utf-8")


def test_tier1_guide_matches_exact_shipped_pack_inventory() -> None:
    text = _doc()
    packs = validate_tier1_language_packs(ROOT)

    assert tuple(pack.id for pack in packs) == TIER1_LANGUAGE_PACK_IDS
    assert tuple(EXPECTED_TIER1_SKILLS) == TIER1_LANGUAGE_PACK_IDS
    for pack in packs:
        assert f"`{pack.id}`" in text
        actual_skills = (*pack.core_skills, *pack.framework_skills)
        assert actual_skills == EXPECTED_TIER1_SKILLS[pack.id]
        for skill in actual_skills:
            assert f"`{skill}`" in text


def test_tier1_guide_documents_current_machine_surfaces_and_role_invariant() -> None:
    text = _doc()

    for required in (
        "sdai tech detect --path .",
        "sdai tech detect --path . --json",
        "sdai.technology-report/v1",
        "sdai skill resolve",
        "sdai.skill-resolution/v1",
        "semantic role",
        "provider routing remain unchanged",
        "java-developer",
        "dependency-ordered skill set",
    ):
        assert required in text


def test_tier1_guide_points_to_implemented_pack_governance_contracts() -> None:
    text = _doc()

    for name in PACK_GOVERNANCE_DOCS:
        assert (ROOT / "docs" / name).is_file(), name
        assert f"`{name}`" in text

    for command in (
        "sdai pack install <publisher/id>",
        "sdai pack update  <publisher/id>",
        "sdai pack remove  <publisher/id>",
        "sdai pack outdated --lock FILE",
        "sdai pack info <publisher/id>",
        "sdai pack search [QUERY]",
        "sdai pack certification",
    ):
        assert command in text


def test_tier1_guide_does_not_reintroduce_obsolete_07_future_claims() -> None:
    text = _doc()
    forbidden = (
        "SDAI 0.7 ships",
        "Full catalog installation, signing, provenance, locking, publisher trust, and remote update behavior remain",
        "The 0.7 skeleton",
        "does **not** introduce remote `pack install` semantics",
        "The later Pack/Catalog milestone will add",
    )
    for phrase in forbidden:
        assert phrase not in text


def test_library_examples_are_explicitly_not_claimed_as_shipped_tier1() -> None:
    text = _doc()

    assert "specialization examples, not claims that such library-specific skills are built into the current Tier-1 set" in text
    assert "Jsign" in text
    assert "Bouncy Castle" in text
    assert "Authenticode" in text
    assert "Shipped Tier-1:" in text
    assert "Not automatically implied by Tier-1:" in text


def test_tier1_guide_keeps_identity_approval_boundary_explicit() -> None:
    text = _doc()
    assert "0.18/#25" in text
    assert "identity-backed enterprise approval" in text
