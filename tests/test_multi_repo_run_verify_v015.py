from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest
import yaml

from sdai.multi_repo_run import (
    MULTI_REPO_RUN_PLAN_API_VERSION,
    MultiRepoExitClass,
    MultiRepoRunError,
    build_multi_repo_run_plan,
    execute_multi_repo_run,
    revalidate_run_plan,
)
from sdai.multi_repo_verify import verify_all_repositories
from sdai.verification import VerificationOutcome
from sdai.version_entrypoint import main as sdai_main


FEATURE = "RUN-101"


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")
    return path


def _git(path: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=path,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return (completed.stdout or "").strip()


def _repository(path: Path, prefix: str) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.email", "sdai-tests@example.invalid")
    _git(path, "config", "user.name", "SD-AI Tests")
    _write(path / ".sdai" / "config.yaml", "{}\n")
    feature = path / "specs" / "changes" / FEATURE
    requirement = f"FR-{prefix}-001"
    task = f"TASK-{prefix}-001"
    _write(
        feature / "requirements.md",
        f"# Requirements\n\n- {requirement}: {prefix} participant requirement.\n",
    )
    _write(
        feature / "tasks.md",
        f"# Tasks\n\n- [ ] {task}: Implement {requirement}.\n",
    )
    _write(
        path / "src" / f"{prefix.casefold()}.py",
        f"# Trace links: {requirement} {task}\n",
    )
    _git(path, "add", ".")
    _git(path, "commit", "-m", "fixture")
    return path


def _project(tmp_path: Path) -> tuple[Path, dict[str, Path]]:
    project = tmp_path / "project"
    project.mkdir()
    _write(project / ".sdai" / "config.yaml", "{}\n")
    repositories = {
        "api": _repository(tmp_path / "api", "API"),
        "shared": _repository(tmp_path / "shared", "SHARED"),
        "ui": _repository(tmp_path / "ui", "UI"),
    }
    payload = {
        "apiVersion": "sdai.feature-repositories/v1",
        "kind": "FeatureRepositories",
        "repositories": [
            {
                "id": repository_id,
                "path": str(path.resolve()),
                "capabilities": ["requirements", "tasks"],
                "ownership": [
                    {"type": "requirement", "pattern": f"FR-{repository_id.upper()}-*"},
                    {"type": "task", "pattern": f"TASK-{repository_id.upper()}-*"},
                ],
                "required": True,
            }
            for repository_id, path in reversed(list(repositories.items()))
        ],
    }
    _write(
        project / ".sdai" / "feature-repositories.yaml",
        yaml.safe_dump(payload, sort_keys=False),
    )
    return project, repositories


def test_run_plan_is_canonical_hash_bound_redacted_and_deterministic(tmp_path: Path) -> None:
    project, repositories = _project(tmp_path)

    first = build_multi_repo_run_plan(project, FEATURE, isolation="worktree")
    second = build_multi_repo_run_plan(project, FEATURE, isolation="worktree")

    assert first.ready
    assert first.to_json() == second.to_json()
    assert first.sha256 == second.sha256
    assert first.repository_resolution_sha256 is not None
    assert first.graph_sha256.startswith("sha256:")
    assert [item.repository_id for item in first.participants] == ["api", "shared", "ui"]
    assert all(item.baseline and item.baseline["clean"] for item in first.participants)
    payload = json.loads(first.to_json())
    assert payload["apiVersion"] == MULTI_REPO_RUN_PLAN_API_VERSION
    assert payload["planSha256"] == first.sha256
    assert json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")) == first.to_json()
    rendered = first.to_json()
    for root in repositories.values():
        assert str(root.resolve()) not in rendered


def test_dirty_repository_is_rejected_before_execution(tmp_path: Path) -> None:
    project, repositories = _project(tmp_path)
    _write(repositories["api"] / "dirty.txt", "dirty\n")

    plan = build_multi_repo_run_plan(project, FEATURE, repository_ids=("api",))

    assert not plan.ready
    assert plan.exit_class is MultiRepoExitClass.PARTICIPANT_UNAVAILABLE_OR_DIRTY
    assert plan.participants[0].status.value == "dirty"


def test_plan_revalidation_detects_post_plan_drift(tmp_path: Path) -> None:
    project, repositories = _project(tmp_path)
    plan = build_multi_repo_run_plan(project, FEATURE, repository_ids=("api",))
    _write(repositories["api"] / "after-plan.txt", "changed\n")

    with pytest.raises(MultiRepoRunError, match="no longer clean/compatible|changed after"):
        revalidate_run_plan(plan)


def test_execute_all_is_deterministic_and_stops_before_unrelated_repo_on_failure(tmp_path: Path) -> None:
    project, _ = _project(tmp_path)
    plan = build_multi_repo_run_plan(project, FEATURE)
    calls: list[str] = []

    def runner(participant, workflow: str, isolation: str) -> int:
        calls.append(participant.repository_id)
        return 2 if participant.repository_id == "shared" else 0

    result = execute_multi_repo_run(plan, runner)

    assert calls == ["api", "shared"]
    assert result.exit_class is MultiRepoExitClass.POLICY_FAILURE
    assert [item.repository_id for item in result.results] == ["api", "shared"]
    assert not any(item.repository_id == "ui" for item in result.results)


def test_run_plan_cli_dispatches_only_explicit_repository(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    project, _ = _project(tmp_path)

    code = sdai_main(
        [
            "run",
            FEATURE,
            "--repo",
            "ui",
            "--plan",
            "--json",
            "--path",
            str(project),
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["apiVersion"] == MULTI_REPO_RUN_PLAN_API_VERSION
    assert [item["repositoryId"] for item in payload["participants"]] == ["ui"]
    assert payload["participants"][0]["command"].startswith(f"sdai run {FEATURE} --repo ui")


def test_all_repo_verification_aggregates_local_reports_without_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, repositories = _project(tmp_path)
    calls: list[str] = []

    class FakeReport:
        def __init__(self, root: Path):
            self.outcome = (
                VerificationOutcome.BLOCKED
                if root.name == "shared"
                else VerificationOutcome.PASSED
            )
            self.root = root

        def to_json(self) -> str:
            return json.dumps(
                {"outcome": self.outcome.value, "repository": self.root.name},
                sort_keys=True,
                separators=(",", ":"),
            )

    def fake_verify(
        root: Path,
        feature_id: str,
        *,
        risk: str,
        environ: dict[str, str],
    ) -> FakeReport:
        calls.append(root.name)
        assert feature_id == FEATURE
        assert risk == "critical"
        assert environ == {}
        return FakeReport(root)

    monkeypatch.setattr("sdai.multi_repo_verify.verify_feature", fake_verify)
    report = verify_all_repositories(project, FEATURE, risk="critical")

    assert calls == ["api", "shared", "ui"]
    assert report.exit_class is MultiRepoExitClass.POLICY_FAILURE
    assert [item.repository_id for item in report.repositories] == ["api", "shared", "ui"]
    assert report.repositories[1].status == "blocked"
    assert isinstance(report.graph_findings, list)
    assert all(_git(root, "status", "--porcelain=v1") == "" for root in repositories.values())


def test_stable_multi_repo_exit_classes() -> None:
    assert int(MultiRepoExitClass.SUCCESS) == 0
    assert int(MultiRepoExitClass.POLICY_FAILURE) == 2
    assert int(MultiRepoExitClass.DRIFT) == 4
    assert int(MultiRepoExitClass.PARTICIPANT_UNAVAILABLE_OR_DIRTY) == 5
    assert int(MultiRepoExitClass.INFRASTRUCTURE_OR_TOOL_FAILURE) == 6
