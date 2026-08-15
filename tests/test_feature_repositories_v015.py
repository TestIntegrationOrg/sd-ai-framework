from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from sdai.feature_repositories import (
    FEATURE_REPOSITORIES_API_VERSION,
    FeatureEntityType,
    FeatureRepositoryError,
    RoutableEntity,
    load_feature_repository_manifest,
    resolve_feature_repositories,
    route_feature_entities,
)


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    (path / ".git").mkdir()
    return path


def _write_manifest(project: Path, repositories: list[dict[str, object]]) -> Path:
    declaration = project / ".sdai" / "feature-repositories.yaml"
    declaration.parent.mkdir(parents=True, exist_ok=True)
    declaration.write_text(
        yaml.safe_dump(
            {
                "apiVersion": FEATURE_REPOSITORIES_API_VERSION,
                "kind": "FeatureRepositories",
                "repositories": repositories,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return declaration


def _repo(
    repo_id: str,
    path: Path,
    *,
    selectors: list[tuple[str, str]],
    capabilities: list[str] | None = None,
    required: bool = True,
) -> dict[str, object]:
    derived = sorted(
        capabilities
        or {
            {
                "requirement": "requirements",
                "contract": "contracts",
                "component": "components",
                "task": "tasks",
            }[entity_type]
            for entity_type, _ in selectors
        }
    )
    return {
        "id": repo_id,
        "path": str(path),
        "capabilities": derived,
        "ownership": [
            {"type": entity_type, "pattern": pattern}
            for entity_type, pattern in selectors
        ],
        "required": required,
    }


def test_routes_api_ui_and_shared_entities_deterministically(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    api = _git_repo(tmp_path / "api")
    ui = _git_repo(tmp_path / "ui")
    shared = _git_repo(tmp_path / "shared")
    _write_manifest(
        project,
        [
            _repo(
                "ui",
                ui,
                selectors=[
                    ("requirement", "UI-*"),
                    ("component", "ui:*"),
                    ("task", "UI-*"),
                ],
            ),
            _repo(
                "shared",
                shared,
                selectors=[
                    ("requirement", "SHARED-*"),
                    ("contract", "shared:*"),
                    ("task", "SHARED-*"),
                ],
            ),
            _repo(
                "api",
                api,
                selectors=[
                    ("requirement", "API-*"),
                    ("contract", "api:*"),
                    ("component", "api:*"),
                    ("task", "API-*"),
                ],
            ),
        ],
    )

    resolved = resolve_feature_repositories(project)
    result = route_feature_entities(
        resolved,
        [
            RoutableEntity(FeatureEntityType.TASK, "UI-22"),
            RoutableEntity(FeatureEntityType.CONTRACT, "shared:events/v1"),
            RoutableEntity(FeatureEntityType.REQUIREMENT, "API-10"),
            RoutableEntity(FeatureEntityType.COMPONENT, "api:orders"),
        ],
    )

    assert [(item.entity.identity, item.repository_id) for item in result.decisions] == [
        ("component:api:orders", "api"),
        ("contract:shared:events/v1", "shared"),
        ("requirement:API-10", "api"),
        ("task:UI-22", "ui"),
    ]
    assert all(item.sha256.startswith("sha256:") for item in result.decisions)
    assert result.sha256.startswith("sha256:")
    assert result.unmatched_optional == ()


def test_manifest_and_routing_json_are_order_independent_and_hide_absolute_paths(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    api = _git_repo(tmp_path / "secret" / "api")
    ui = _git_repo(tmp_path / "secret" / "ui")
    repositories = [
        _repo("api", api, selectors=[("task", "API-*"), ("requirement", "API-*")]),
        _repo("ui", ui, selectors=[("task", "UI-*"), ("requirement", "UI-*")]),
    ]
    _write_manifest(project, list(reversed(repositories)))
    first = resolve_feature_repositories(project)
    first_result = route_feature_entities(
        first,
        [
            RoutableEntity(FeatureEntityType.TASK, "UI-1"),
            RoutableEntity(FeatureEntityType.REQUIREMENT, "API-1"),
        ],
    )

    _write_manifest(project, repositories)
    second = resolve_feature_repositories(project)
    second_result = route_feature_entities(
        second,
        [
            RoutableEntity(FeatureEntityType.REQUIREMENT, "API-1"),
            RoutableEntity(FeatureEntityType.TASK, "UI-1"),
        ],
    )

    assert first.manifest_sha256 == second.manifest_sha256
    assert first_result.to_json() == second_result.to_json()
    output = second_result.to_json()
    assert str(api.resolve()) not in output
    assert str(ui.resolve()) not in output
    assert json.dumps(json.loads(output), sort_keys=True, separators=(",", ":"), ensure_ascii=False) == output


def test_ambiguous_overlap_fails_closed_with_explainable_selectors(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    first = _git_repo(tmp_path / "first")
    second = _git_repo(tmp_path / "second")
    _write_manifest(
        project,
        [
            _repo("first", first, selectors=[("task", "API-*")]),
            _repo("second", second, selectors=[("task", "*-42")]),
        ],
    )

    resolved = resolve_feature_repositories(project)
    with pytest.raises(FeatureRepositoryError, match="ambiguous ownership.*first:API-.*second:\\*-42"):
        route_feature_entities(
            resolved,
            [RoutableEntity(FeatureEntityType.TASK, "API-42")],
        )


def test_exact_duplicate_selector_across_repositories_is_rejected_at_load(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    first = _git_repo(tmp_path / "first")
    second = _git_repo(tmp_path / "second")
    _write_manifest(
        project,
        [
            _repo("first", first, selectors=[("requirement", "API-*")]),
            _repo("second", second, selectors=[("requirement", "API-*")]),
        ],
    )

    with pytest.raises(FeatureRepositoryError, match="declared by both"):
        load_feature_repository_manifest(project)


def test_unmatched_required_entity_fails_and_optional_entity_is_reported(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    api = _git_repo(tmp_path / "api")
    _write_manifest(project, [_repo("api", api, selectors=[("task", "API-*")])])
    resolved = resolve_feature_repositories(project)

    with pytest.raises(FeatureRepositoryError, match="required entity 'task:UI-1' is not owned"):
        route_feature_entities(
            resolved,
            [RoutableEntity(FeatureEntityType.TASK, "UI-1")],
        )

    optional = route_feature_entities(
        resolved,
        [RoutableEntity(FeatureEntityType.TASK, "UI-1", required=False)],
    )
    assert optional.decisions == ()
    assert tuple(item.identity for item in optional.unmatched_optional) == ("task:UI-1",)


def test_required_missing_repository_fails_without_filesystem_discovery(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    # A similarly named repository exists nearby, but routing must never discover it.
    _git_repo(tmp_path / "api-discovered")
    declared = tmp_path / "api-explicitly-missing"
    _write_manifest(
        project,
        [_repo("api", declared, selectors=[("task", "API-*")])],
    )

    with pytest.raises(FeatureRepositoryError, match="required repository path is missing"):
        resolve_feature_repositories(project)


def test_optional_missing_repository_is_visible_but_cannot_receive_owned_entity(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    missing = tmp_path / "optional-missing"
    _write_manifest(
        project,
        [_repo("optional", missing, selectors=[("contract", "optional:*")], required=False)],
    )

    resolved = resolve_feature_repositories(project)
    assert resolved.repositories[0].available is False
    with pytest.raises(FeatureRepositoryError, match="routes to unavailable repository 'optional'"):
        route_feature_entities(
            resolved,
            [RoutableEntity(FeatureEntityType.CONTRACT, "optional:v1")],
        )


def test_selector_requires_matching_declared_capability(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    api = _git_repo(tmp_path / "api")
    _write_manifest(
        project,
        [
            _repo(
                "api",
                api,
                selectors=[("contract", "api:*")],
                capabilities=["tasks"],
            )
        ],
    )

    with pytest.raises(FeatureRepositoryError, match="undeclared capability: contracts"):
        load_feature_repository_manifest(project)


def test_duplicate_repository_identity_and_resolved_path_fail_closed(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    first = _git_repo(tmp_path / "first")
    second = _git_repo(tmp_path / "second")
    _write_manifest(
        project,
        [
            _repo("same", first, selectors=[("task", "A-*")]),
            _repo("same", second, selectors=[("task", "B-*")]),
        ],
    )
    with pytest.raises(FeatureRepositoryError, match="ids must be unique"):
        load_feature_repository_manifest(project)

    _write_manifest(
        project,
        [
            _repo("first", first, selectors=[("task", "A-*")]),
            _repo("second", first.resolve(), selectors=[("task", "B-*")]),
        ],
    )
    with pytest.raises(FeatureRepositoryError, match="declared repository paths must be unique|resolve to the same"):
        resolve_feature_repositories(project)


def test_manifest_rejects_yaml_aliases_duplicate_keys_and_unsafe_selector(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    declaration = project / ".sdai" / "feature-repositories.yaml"
    declaration.parent.mkdir()

    declaration.write_text(
        """apiVersion: sdai.feature-repositories/v1
kind: FeatureRepositories
repositories: &repos []
extra: *repos
""",
        encoding="utf-8",
    )
    with pytest.raises(FeatureRepositoryError, match="aliases are not allowed"):
        load_feature_repository_manifest(project)

    declaration.write_text(
        """apiVersion: sdai.feature-repositories/v1
kind: FeatureRepositories
kind: FeatureRepositories
repositories: []
""",
        encoding="utf-8",
    )
    with pytest.raises(FeatureRepositoryError, match="YAML is malformed"):
        load_feature_repository_manifest(project)

    repo = _git_repo(tmp_path / "repo")
    _write_manifest(project, [_repo("api", repo, selectors=[("task", "API-**")])])
    with pytest.raises(FeatureRepositoryError, match="single '\\*'/'\\?' wildcards"):
        load_feature_repository_manifest(project)
