from __future__ import annotations

from pathlib import Path


_CONTRACTS = {
    "src/sdai/verification.py": (
        "sdai.verify-report/v1",
        "sdai.semantic-review/v1",
    ),
    "src/sdai/convergence.py": ("sdai.convergence-state/v1",),
    "src/sdai/isolated_tasks.py": (
        "sdai.isolated-task/v1",
        "sdai.isolated-invocation/v1",
        "sdai.isolated-result/v1",
    ),
    "src/sdai/completion_report.py": ("sdai.completion-barrier/v1",),
    "src/sdai/agent_platform/model_routing.py": ("sdai.model-routing/v1",),
}

_EVIDENCE_TESTS = (
    "tests/test_verify_engine_v011.py",
    "tests/test_verify_cli_v011.py",
    "tests/test_verification_v011.py",
    "tests/test_convergence_v011.py",
    "tests/test_convergence_cli_v011.py",
    "tests/test_isolated_tasks_v011.py",
    "tests/test_isolated_review_hardening_v011.py",
    "tests/test_isolated_workspace_edgecases_v011.py",
    "tests/test_completion_barrier_v011.py",
    "tests/test_completion_barrier_hardening_v011.py",
    "tests/test_model_routing_v011.py",
    "tests/test_routed_execution_v011.py",
    "tests/test_isolated_model_routing_v011.py",
    "tests/test_execution_ledger_v09.py",
)


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[1]


def test_v011_public_contract_versions_remain_present() -> None:
    root = _repository_root()

    for relative, versions in _CONTRACTS.items():
        source = root / relative
        assert source.is_file(), relative
        text = source.read_text(encoding="utf-8")
        for version in versions:
            assert version in text, f"{version} missing from {relative}"


def test_v011_release_evidence_suites_remain_in_full_regression_set() -> None:
    root = _repository_root()

    for relative in _EVIDENCE_TESTS:
        path = root / relative
        assert path.is_file(), relative
        assert path.read_text(encoding="utf-8"), relative


def test_v011_release_evidence_document_is_utf8_and_links_all_slices() -> None:
    root = _repository_root()
    document = root / "docs" / "V0.11-RELEASE-EVIDENCE.md"

    text = document.read_text(encoding="utf-8")

    assert "SDAI 0.11 Release Evidence" in text
    for issue in ("#118", "#119", "#120", "#121", "#122", "#123", "#117"):
        assert issue in text
    assert "Windows / Python 3.11" in text
    assert "Ubuntu / Python 3.12" in text
