from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
POLICY = REPO_ROOT / "docs" / "COMPATIBILITY-AND-RELEASE-GOVERNANCE.md"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_release_governance_policy_is_linked_from_active_guidance() -> None:
    relative = "docs/COMPATIBILITY-AND-RELEASE-GOVERNANCE.md"

    assert POLICY.is_file()
    assert f"({relative})" in _read(REPO_ROOT / "README.md")
    assert relative in _read(REPO_ROOT / "CONTRIBUTING.md")
    assert relative in _read(REPO_ROOT / "docs" / "RELEASING.md")
    assert relative in _read(REPO_ROOT / "docs" / "releases" / "1.0-release-readiness.md")


def test_policy_pins_stable_surfaces_and_semver_independence() -> None:
    text = _read(POLICY)

    for marker in (
        "src/sdai/__init__.py::__version__",
        "Python 3.11 and 3.12",
        "Ubuntu, Windows, and macOS",
        "`sdai/v1`",
        "stable `sdai.extensions` exports",
        "Automation JSON",
        "Migration and scaffold safety",
        "authoritative built-in/organization locks",
        "non-exported Python modules",
        "human-readable progress text",
        "package version is independent from manifest, Pack, workflow, schema",
    ):
        assert marker in text


def test_policy_authority_links_resolve() -> None:
    for relative in (
        "docs/RELEASING.md",
        "docs/PLATFORM-CONFIDENCE.md",
        "docs/EXTENSIONS.md",
        "docs/JSON-CONTRACTS.md",
        "docs/MIGRATIONS.md",
        "docs/ENTERPRISE-POLICY.md",
        "README.md",
        "docs/EXECUTION-SECURITY.md",
    ):
        assert (REPO_ROOT / relative).exists(), relative


def test_policy_requires_versioned_breaks_deprecation_and_security_evidence() -> None:
    text = _read(POLICY)

    for marker in (
        "versioned successor and migration guidance",
        "Deprecation and removal do not occur in the same stable release",
        "remains supported for the rest of 1.x",
        "## Security and compliance exception",
        "the narrowest safe change",
        "unsafe case is blocked",
        "cannot bypass CI",
        "must not corrupt cataloged JSON stdout",
    ):
        assert marker in text


def test_policy_requires_frozen_exact_head_matrix_and_separate_publication() -> None:
    text = _read(POLICY)

    for marker in (
        "scope owner",
        "implementer",
        "reviewer",
        "release coordinator",
        "zero unresolved actionable review threads",
        "python -m pytest -q",
        "python tests/package_install_smoke.py",
        "Any new commit invalidates prior exact-head evidence",
        "Ubuntu, Windows, and macOS with Python 3.11 and 3.12",
        "PR-head evidence cannot prove the merged SHA",
        "Record the merged-main SHA, `push: main` CI run, and all six merged-main job",
        "failed merged-main run is a no-go for publication",
        "separate explicit action",
        "Do not force-push `main`",
    ):
        assert marker in text


def test_readiness_records_final_identity_independent_slices_and_held_scope() -> None:
    readiness = _read(REPO_ROOT / "docs" / "releases" / "1.0-release-readiness.md")
    policy = _read(POLICY)

    for issue in ("#288", "#290", "#292"):
        assert issue in readiness
    assert "distinct exact merged-main SHA" in readiness
    assert "PR-head evidence cannot prove the merged SHA" in readiness
    assert "0.18/#25 identity-backed approvals remain held" in policy
    for forbidden_claim in (
        "identity-backed approvals are complete",
        "distinct approvers are enforced",
    ):
        assert forbidden_claim not in policy
