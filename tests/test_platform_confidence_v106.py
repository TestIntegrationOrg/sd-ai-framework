from pathlib import Path

import yaml


ROOT = Path(__file__).parents[1]


def _ci() -> dict:
    return yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))


def test_primary_ci_keeps_complete_platform_python_matrix():
    data = _ci()
    job = data["jobs"]["test"]
    strategy = job["strategy"]
    matrix = strategy["matrix"]

    assert strategy["fail-fast"] is False
    assert matrix["os"] == ["ubuntu-latest", "windows-latest"]
    assert [str(version) for version in matrix["python-version"]] == ["3.11", "3.12"]
    assert matrix["include"] == [
        {"os": "macos-latest", "python-version": "3.11"},
        {"os": "macos-latest", "python-version": "3.12"},
    ]
    assert job["runs-on"] == "${{ matrix.os }}"

    effective = {
        (operating_system, str(version))
        for operating_system in matrix["os"]
        for version in matrix["python-version"]
    }
    effective.update(
        (entry["os"], str(entry["python-version"])) for entry in matrix["include"]
    )
    assert effective == {
        ("ubuntu-latest", "3.11"),
        ("ubuntu-latest", "3.12"),
        ("windows-latest", "3.11"),
        ("windows-latest", "3.12"),
        ("macos-latest", "3.11"),
        ("macos-latest", "3.12"),
    }


def test_every_matrix_leg_runs_unfiltered_pytest_suite():
    data = _ci()
    steps = data["jobs"]["test"]["steps"]
    run_commands = [step["run"] for step in steps if "run" in step]

    assert "pip install -e '.[dev]'" in run_commands
    assert "pytest -q" in run_commands
    assert not any("pytest" in command and ("-k" in command or "--ignore" in command) for command in run_commands)


def test_platform_confidence_reference_pins_exact_sha_evidence_and_boundaries():
    text = (ROOT / "docs" / "PLATFORM-CONFIDENCE.md").read_text(encoding="utf-8")
    lowered = text.lower()

    for expected in (
        "ubuntu (`ubuntu-latest`)",
        "windows (`windows-latest`)",
        "macos (`macos-latest`)",
        "python 3.11",
        "python 3.12",
        "all six pr-head matrix jobs",
        "exact squash-merged `main` sha",
        "unfiltered `pytest -q`",
        "identity-backed enterprise approvals (0.18/#25) remain held/deferred",
    ):
        assert expected in lowered
