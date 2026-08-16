from __future__ import annotations

import json
from pathlib import Path

import pytest

import sdai.feature_repositories as feature_repositories
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


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    (path / ".git").mkdir()
    return path


def test_custom_declaration_path_is_preserved_in_resolution_and_route_provenance(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    api = _git_repo(tmp_path / "api")
    declaration = project / ".sdai" / "ownership" / "repositories.yaml"
    declaration.parent.mkdir(parents=True)
    declaration.write_text(
        "\n".join(
            [
                "apiVersion: sdai.feature-repositories/v1",
                "kind: FeatureRepositories",
                "repositories:",
                "  - id: api",
                f"    path: {api.as_posix()}",
                "    capabilities:",
                "      - requirements",
                "    ownership:",
                "      - type: requirement",
                "        pattern: API-*",
                "",
            ]
        ),
        encoding="utf-8",
    )

    source = Path(".sdai/ownership/repositories.yaml")
    resolved = resolve_feature_repositories(project, source)
    result = route_feature_entities(
        resolved,
        [RoutableEntity(FeatureEntityType.REQUIREMENT, "API-101")],
    )

    assert resolved.source == source.as_posix()
    resolution_payload = json.loads(resolved.to_json())
    assert resolution_payload["source"] == source.as_posix()
    route_payload = json.loads(result.to_json())
    assert route_payload["decisions"][0]["provenance"]["source"] == source.as_posix()
    assert route_payload["decisions"][0]["provenance"]["sourceSha256"] == resolved.source_sha256


def test_manifest_enforces_selector_limit_across_all_repositories(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(feature_repositories, "FEATURE_REPOSITORIES_MAX_SELECTORS", 3)
    digest = "sha256:" + ("0" * 64)
    first = FeatureRepository(
        id="api",
        path="../api",
        capabilities=("requirements", "tasks"),
        ownership=(
            OwnershipSelector(FeatureEntityType.REQUIREMENT, "API-*"),
            OwnershipSelector(FeatureEntityType.TASK, "API-*"),
        ),
    )
    second = FeatureRepository(
        id="ui",
        path="../ui",
        capabilities=("requirements", "tasks"),
        ownership=(
            OwnershipSelector(FeatureEntityType.REQUIREMENT, "UI-*"),
            OwnershipSelector(FeatureEntityType.TASK, "UI-*"),
        ),
    )

    with pytest.raises(FeatureRepositoryError, match="too many ownership selectors"):
        FeatureRepositoryManifest((first, second), digest)
