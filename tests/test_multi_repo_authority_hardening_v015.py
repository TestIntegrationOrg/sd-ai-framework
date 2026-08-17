from __future__ import annotations

import json
from pathlib import Path

import pytest

from sdai.feature_repositories import (
    FeatureEntityType,
    FeatureRepository,
    FeatureRepositoryError,
    FeatureRepositoryManifest,
    OwnershipSelector,
    RoutableEntity,
    resolve_feature_repositories,
    route_feature_entities,
)
from sdai.multi_repo_run import (
    MultiRepoExitClass,
    MultiRepoRunPlan,
    PlannedRepositoryRun,
    RunParticipantStatus,
    execute_multi_repo_run,
)


_SHA = "sha256:" + ("1" * 64)


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / ".git").mkdir(exist_ok=True)
    return path


def _write_manifest(project: Path, repositories: list[dict[str, object]]) -> None:
    target = project / ".sdai" / "feature-repositories.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "apiVersion: sdai.feature-repositories/v1",
        "kind: FeatureRepositories",
        "repositories:",
    ]
    for repository in repositories:
        lines.extend(
            [
                f"  - id: {repository['id']}",
                f"    path: {repository['path']}",
                f"    required: {'true' if repository.get('required', True) else 'false'}",
                "    capabilities:",
                "      - requirements",
                "    ownership:",
                "      - type: requirement",
                f"        pattern: {repository['pattern']}",
            ]
        )
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_repository_path_must_be_nfc_normalized() -> None:
    with pytest.raises(FeatureRepositoryError, match="SDAI-FEATURE-REPO-002"):
        FeatureRepository(
            id="api",
            path="../cafe\u0301",
            capabilities=("requirements",),
            ownership=(OwnershipSelector(FeatureEntityType.REQUIREMENT, "API-*"),),
        )


def test_manifest_rejects_case_colliding_declared_paths() -> None:
    digest = "sha256:" + ("0" * 64)
    with pytest.raises(FeatureRepositoryError, match="SDAI-FEATURE-REPO-003"):
        FeatureRepositoryManifest(
            (
                FeatureRepository(
                    "api",
                    "../Service",
                    ("requirements",),
                    (OwnershipSelector(FeatureEntityType.REQUIREMENT, "API-*"),),
                ),
                FeatureRepository(
                    "ui",
                    "../service",
                    ("requirements",),
                    (OwnershipSelector(FeatureEntityType.REQUIREMENT, "UI-*"),),
                ),
            ),
            digest,
        )


def test_resolver_rejects_nested_repository_roots(tmp_path: Path) -> None:
    project = tmp_path / "coordinator"
    project.mkdir()
    parent = _git_repo(tmp_path / "platform")
    child = _git_repo(parent / "api")
    _write_manifest(
        project,
        [
            {"id": "platform", "path": parent.as_posix(), "pattern": "PLATFORM-*"},
            {"id": "api", "path": child.as_posix(), "pattern": "API-*"},
        ],
    )

    with pytest.raises(FeatureRepositoryError, match="duplicate or nested local paths"):
        resolve_feature_repositories(project)


def test_resolver_rejects_symlink_redirected_repository(tmp_path: Path) -> None:
    project = tmp_path / "coordinator"
    project.mkdir()
    actual = _git_repo(tmp_path / "actual-api")
    link = tmp_path / "api-link"
    try:
        link.symlink_to(actual, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is unavailable on this platform")
    _write_manifest(
        project,
        [{"id": "api", "path": link.as_posix(), "pattern": "API-*"}],
    )

    with pytest.raises(FeatureRepositoryError, match="SDAI-FEATURE-REPO-002"):
        resolve_feature_repositories(project)


def test_optional_unavailable_repository_cannot_own_required_entity(tmp_path: Path) -> None:
    project = tmp_path / "coordinator"
    project.mkdir()
    missing = tmp_path / "missing-api"
    _write_manifest(
        project,
        [
            {
                "id": "api",
                "path": missing.as_posix(),
                "pattern": "API-*",
                "required": False,
            }
        ],
    )
    resolved = resolve_feature_repositories(project)

    with pytest.raises(FeatureRepositoryError, match="SDAI-FEATURE-REPO-004"):
        route_feature_entities(
            resolved,
            [RoutableEntity(FeatureEntityType.REQUIREMENT, "API-101")],
        )


def test_cross_repository_selector_overlap_fails_closed(tmp_path: Path) -> None:
    project = tmp_path / "coordinator"
    project.mkdir()
    api = _git_repo(tmp_path / "api")
    shared = _git_repo(tmp_path / "shared")
    _write_manifest(
        project,
        [
            {"id": "api", "path": api.as_posix(), "pattern": "API-*"},
            {"id": "shared", "path": shared.as_posix(), "pattern": "*-101"},
        ],
    )
    resolved = resolve_feature_repositories(project)

    with pytest.raises(FeatureRepositoryError, match="SDAI-FEATURE-REPO-005"):
        route_feature_entities(
            resolved,
            [RoutableEntity(FeatureEntityType.REQUIREMENT, "API-101")],
        )


def test_multi_repo_execution_fails_fast_without_touching_later_participants(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    baseline = json.dumps(
        {
            "branch": "main",
            "clean": True,
            "commit": "0" * 40,
            "repositoryIdentity": "repo",
            "statusSha256": _SHA,
            "tree": "0" * 40,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    participants = tuple(
        PlannedRepositoryRun(
            repository_id=repository_id,
            root=tmp_path / repository_id,
            required=True,
            ordinal=index,
            status=RunParticipantStatus.READY,
            baseline_json=baseline,
            command=f"sdai run HARD-101 --repo {repository_id}",
            branch_policy="in-place-current-branch",
        )
        for index, repository_id in enumerate(("api", "ui", "shared"), start=1)
    )
    plan = MultiRepoRunPlan(
        feature_id="HARD-101",
        graph_sha256=_SHA,
        repository_resolution_sha256=_SHA,
        store_resolution_sha256=None,
        workflow="standard",
        isolation="in-place",
        participants=participants,
        blockers=(),
    )
    monkeypatch.setattr("sdai.multi_repo_run.revalidate_run_plan", lambda plan: None)
    calls: list[str] = []

    def runner(participant: PlannedRepositoryRun, workflow: str, isolation: str) -> int:
        calls.append(participant.repository_id)
        return 2 if participant.repository_id == "api" else 0

    result = execute_multi_repo_run(plan, runner)

    assert result.exit_class is MultiRepoExitClass.POLICY_FAILURE
    assert calls == ["api"]
    assert [item.repository_id for item in result.results] == ["api"]
