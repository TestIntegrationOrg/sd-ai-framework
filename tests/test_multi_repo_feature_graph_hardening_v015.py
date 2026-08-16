from __future__ import annotations

from pathlib import Path

import yaml

from sdai.multi_repo_feature_graph import (
    FeatureGraphNodeType,
    build_multi_repo_feature_graph,
)
from sdai.specification_store_lifecycle import create_store, register_store


FEATURE = "GRAPH-BAD-101"


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")
    return path


def test_malformed_bound_store_change_is_a_deterministic_finding(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _write(project / ".sdai" / "config.yaml", "{}\n")

    store = tmp_path / "store"
    create_store(store, "central-specs", "1.0.0")
    _write(
        store / "specs" / "changes" / FEATURE / "change.yaml",
        """version: 1
feature_id: GRAPH-BAD-101
title: Malformed graph input
description: This snapshot is internally consistent but semantically invalid
status: definitely-not-a-valid-status
domains: [api]
baselines:
  api: null
""",
    )
    register_store(project, store)

    repository = tmp_path / "api"
    repository.mkdir()
    (repository / ".git").mkdir()
    ownership = {
        "apiVersion": "sdai.feature-repositories/v1",
        "kind": "FeatureRepositories",
        "repositories": [
            {
                "id": "api",
                "path": str(repository.resolve()),
                "capabilities": ["requirements"],
                "ownership": [
                    {"type": "requirement", "pattern": "FR-API-*"},
                ],
                "required": True,
            }
        ],
    }
    _write(
        project / ".sdai" / "feature-repositories.yaml",
        yaml.safe_dump(ownership, sort_keys=False),
    )

    first = build_multi_repo_feature_graph(project, FEATURE)
    second = build_multi_repo_feature_graph(project, FEATURE)

    assert first.to_json() == second.to_json()
    assert first.has_errors
    assert any(
        finding.code == "SDAI-FEATURE-GRAPH-STALE-CONTENT"
        and finding.subject == FEATURE
        and finding.participant == "central-specs@1.0.0"
        for finding in first.findings
    )


def test_pr_reference_is_versioned_but_not_synthesized_by_graph_builder(tmp_path: Path) -> None:
    assert FeatureGraphNodeType.PR_REFERENCE.value == "pr-reference"

    project = tmp_path / "project"
    project.mkdir()
    _write(project / ".sdai" / "config.yaml", "{}\n")
    _write(
        project / ".sdai" / "feature-repositories.yaml",
        yaml.safe_dump(
            {
                "apiVersion": "sdai.feature-repositories/v1",
                "kind": "FeatureRepositories",
                "repositories": [
                    {
                        "id": "api",
                        "path": str((tmp_path / "api").resolve()),
                        "capabilities": ["requirements"],
                        "ownership": [
                            {"type": "requirement", "pattern": "FR-API-*"},
                        ],
                        "required": False,
                    }
                ],
            },
            sort_keys=False,
        ),
    )

    graph = build_multi_repo_feature_graph(project, "GRAPH-EMPTY-101")

    assert not any(node.type is FeatureGraphNodeType.PR_REFERENCE for node in graph.nodes)


def test_missing_repository_error_does_not_expose_declared_absolute_path(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _write(project / ".sdai" / "config.yaml", "{}\n")
    missing = (tmp_path / "secret" / "api-repository").resolve()
    _write(
        project / ".sdai" / "feature-repositories.yaml",
        yaml.safe_dump(
            {
                "apiVersion": "sdai.feature-repositories/v1",
                "kind": "FeatureRepositories",
                "repositories": [
                    {
                        "id": "api",
                        "path": str(missing),
                        "capabilities": ["requirements"],
                        "ownership": [
                            {"type": "requirement", "pattern": "FR-API-*"},
                        ],
                        "required": True,
                    }
                ],
            },
            sort_keys=False,
        ),
    )

    graph = build_multi_repo_feature_graph(project, "GRAPH-MISSING-101")
    rendered = graph.to_json()

    assert graph.has_errors
    assert any(
        finding.code == "SDAI-FEATURE-GRAPH-MISSING-REPOSITORY"
        for finding in graph.findings
    )
    assert str(missing) not in rendered
