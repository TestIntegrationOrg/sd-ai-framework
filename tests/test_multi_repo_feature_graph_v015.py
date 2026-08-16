from __future__ import annotations

import json
from pathlib import Path

import yaml

from sdai.multi_repo_feature_graph import (
    MULTI_REPO_FEATURE_GRAPH_API_VERSION,
    FeatureGraphFindingLevel,
    FeatureGraphNodeType,
    build_multi_repo_feature_graph,
)
from sdai.specification_store_lifecycle import create_store, register_store
from sdai.specification_store_references import (
    SPECIFICATION_STORE_REFERENCES_PATH,
    SpecificationStoreContentBinding,
    SpecificationStoreReference,
    SpecificationStoreReferenceSet,
    resolve_specification_store_references,
)
from sdai.version_entrypoint import main as sdai_main


FEATURE = "GRAPH-101"


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")
    return path


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink()
    }


def _initialized_project(path: Path) -> Path:
    path.mkdir(parents=True)
    _write(path / ".sdai" / "config.yaml", "{}\n")
    return path


def _store(path: Path) -> Path:
    create_store(path, "central-specs", "1.0.0", description="Central feature specifications")
    feature = path / "specs" / "changes" / FEATURE
    _write(
        feature / "change.yaml",
        """version: 1
feature_id: GRAPH-101
title: Multi repository graph
description: Route API UI and shared requirements
status: proposed
domains:
  - api
  - shared
  - ui
baselines:
  api: null
  shared: null
  ui: null
""",
    )
    requirements = {
        "api": "FR-API-001",
        "shared": "FR-SHARED-001",
        "ui": "FR-UI-001",
    }
    for domain, requirement in requirements.items():
        _write(
            feature / "deltas" / f"{domain}.yaml",
            f"""version: 1
domain: {domain}
baseline_spec_sha256: null
operations:
  - op: ADDED
    requirement_id: {requirement}
    reason: Add {domain} requirement
    definition: {domain} repository must implement its owned behavior.
""",
        )
    return path


def _repository(path: Path, prefix: str) -> Path:
    path.mkdir(parents=True)
    (path / ".git").mkdir()
    feature = path / "specs" / "changes" / FEATURE
    requirement = f"FR-{prefix}-001"
    task = f"TASK-{prefix}-001"
    contract = f"CONTRACT-{prefix}-001"
    component = f"COMPONENT-{prefix}-001"
    _write(
        feature / "requirements.md",
        f"""# Requirements

- {requirement}: {prefix} behavior is implemented here.
- {component}: {prefix} component
""",
    )
    _write(
        feature / "tasks.md",
        f"""# Tasks

- [ ] {task}: Implement {requirement} and {contract}.
""",
    )
    _write(
        feature / "contracts" / "api.yaml",
        f"""id: {contract}
status: proposed
references: [{requirement}]
""",
    )
    _write(
        path / "src" / f"{prefix.casefold()}.py",
        f"""# Explicit trace links: {requirement} {task} {contract} {component}

def run() -> None:
    pass
""",
    )
    return path


def _ownership_repository(repository_id: str, path: Path, prefix: str) -> dict[str, object]:
    return {
        "id": repository_id,
        "path": str(path.resolve()),
        "capabilities": ["requirements", "contracts", "components", "tasks"],
        "ownership": [
            {"type": "requirement", "pattern": f"FR-{prefix}-*"},
            {"type": "contract", "pattern": f"CONTRACT-{prefix}-*"},
            {"type": "component", "pattern": f"COMPONENT-{prefix}-*"},
            {"type": "task", "pattern": f"TASK-{prefix}-*"},
        ],
        "required": True,
    }


def _write_repository_map(project: Path, repositories: list[dict[str, object]]) -> Path:
    payload = {
        "apiVersion": "sdai.feature-repositories/v1",
        "kind": "FeatureRepositories",
        "repositories": repositories,
    }
    return _write(
        project / ".sdai" / "feature-repositories.yaml",
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
    )


def _fixture(tmp_path: Path) -> tuple[Path, Path, dict[str, Path]]:
    project = _initialized_project(tmp_path / "project")
    store = _store(tmp_path / "store")
    repositories = {
        "api": _repository(tmp_path / "api", "API"),
        "shared": _repository(tmp_path / "shared", "SHARED"),
        "ui": _repository(tmp_path / "ui", "UI"),
    }
    register_store(project, store)
    _write_repository_map(
        project,
        [
            _ownership_repository("ui", repositories["ui"], "UI"),
            _ownership_repository("api", repositories["api"], "API"),
            _ownership_repository("shared", repositories["shared"], "SHARED"),
        ],
    )
    return project, store, repositories


def _edge_set(graph: object) -> set[tuple[str, str, str]]:
    return {
        (edge.relation, edge.source, edge.target)
        for edge in graph.edges  # type: ignore[attr-defined]
    }


def test_builds_one_deterministic_api_ui_shared_graph_and_preserves_trace_edges(
    tmp_path: Path,
) -> None:
    project, store, repositories = _fixture(tmp_path)
    roots = (project, store, *repositories.values())
    before = {str(root): _snapshot(root) for root in roots}

    first = build_multi_repo_feature_graph(project, FEATURE)
    second = build_multi_repo_feature_graph(project, FEATURE)

    assert not first.has_errors
    assert first.to_json() == second.to_json()
    assert first.sha256 == second.sha256
    assert first.store_resolution_sha256 is not None
    assert first.repository_resolution_sha256 is not None
    assert {str(root): _snapshot(root) for root in roots} == before

    node_ids = {node.node_id for node in first.nodes}
    assert {
        "store:central-specs@1.0.0",
        "repository:api",
        "repository:shared",
        "repository:ui",
        "requirement:FR-API-001",
        "requirement:FR-SHARED-001",
        "requirement:FR-UI-001",
        "task:TASK-API-001",
        "contract:CONTRACT-UI-001",
        "component:COMPONENT-SHARED-001",
    } <= node_ids

    edges = _edge_set(first)
    assert (
        "declares",
        "store:central-specs@1.0.0",
        "requirement:FR-API-001",
    ) in edges
    assert (
        "owned-by",
        "requirement:FR-API-001",
        "repository:api",
    ) in edges
    assert (
        "owned-by",
        "requirement:FR-UI-001",
        "repository:ui",
    ) in edges
    assert (
        "owned-by",
        "requirement:FR-SHARED-001",
        "repository:shared",
    ) in edges
    assert any(
        relation == "references"
        and target == "requirement:FR-API-001"
        and source.startswith("code:path-sha256:")
        for relation, source, target in edges
    )

    rendered = first.to_json()
    for root in roots:
        assert str(root.resolve()) not in rendered
    payload = json.loads(rendered)
    assert payload["apiVersion"] == MULTI_REPO_FEATURE_GRAPH_API_VERSION
    assert payload["graphSha256"] == first.sha256
    assert json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")) == rendered


def test_graph_reports_ambiguous_cross_repository_ownership_without_inventing_edge(
    tmp_path: Path,
) -> None:
    project, _, repositories = _fixture(tmp_path)
    _write_repository_map(
        project,
        [
            _ownership_repository("api", repositories["api"], "API"),
            {
                **_ownership_repository("shared", repositories["shared"], "SHARED"),
                "ownership": [
                    {"type": "requirement", "pattern": "FR-API-*"},
                    {"type": "contract", "pattern": "CONTRACT-SHARED-*"},
                    {"type": "component", "pattern": "COMPONENT-SHARED-*"},
                    {"type": "task", "pattern": "TASK-SHARED-*"},
                ],
            },
            _ownership_repository("ui", repositories["ui"], "UI"),
        ],
    )

    graph = build_multi_repo_feature_graph(project, FEATURE)

    assert graph.has_errors
    assert any(
        finding.code == "SDAI-FEATURE-GRAPH-AMBIGUOUS-ROUTING"
        and finding.subject == "requirement:FR-API-001"
        for finding in graph.findings
    )
    assert (
        "owned-by",
        "requirement:FR-API-001",
        "repository:api",
    ) not in _edge_set(graph)


def test_graph_reports_missing_required_repository_participant(tmp_path: Path) -> None:
    project, _, repositories = _fixture(tmp_path)
    api = repositories["api"]
    missing = api.with_name("api-missing")
    api.rename(missing)

    graph = build_multi_repo_feature_graph(project, FEATURE)

    assert graph.has_errors
    assert any(
        finding.code == "SDAI-FEATURE-GRAPH-MISSING-REPOSITORY"
        for finding in graph.findings
    )


def test_graph_reports_stale_bound_specification_store_content(tmp_path: Path) -> None:
    project, store, _ = _fixture(tmp_path)
    resolved = resolve_specification_store_references(project)
    current = resolved.references[0]
    bound = SpecificationStoreReference(
        store=current.reference.store,
        version=current.reference.version,
        path=current.reference.path,
        content=SpecificationStoreContentBinding(
            current.manifest.sha256,
            current.snapshot.sha256,
        ),
    )
    declaration = SpecificationStoreReferenceSet(
        (bound,),
        "sha256:" + ("0" * 64),
    )
    _write(
        project / SPECIFICATION_STORE_REFERENCES_PATH,
        yaml.safe_dump(declaration.as_dict(), sort_keys=False, allow_unicode=True),
    )
    delta = store / "specs" / "changes" / FEATURE / "deltas" / "api.yaml"
    delta.write_text(delta.read_text(encoding="utf-8") + "\n# drift\n", encoding="utf-8", newline="\n")

    graph = build_multi_repo_feature_graph(project, FEATURE)

    assert graph.has_errors
    assert any(
        finding.code == "SDAI-FEATURE-GRAPH-STALE-CONTENT"
        and finding.subject == SPECIFICATION_STORE_REFERENCES_PATH
        for finding in graph.findings
    )


def test_version_entrypoint_dispatches_feature_graph_without_weakening_feature_intake(
    tmp_path: Path,
    capsys: object,
) -> None:
    project, _, _ = _fixture(tmp_path)

    code = sdai_main(["feature", "graph", FEATURE, "--json", "--path", str(project)])
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    payload = json.loads(captured.out)

    assert code == 0
    assert payload["apiVersion"] == MULTI_REPO_FEATURE_GRAPH_API_VERSION
    assert payload["featureId"] == FEATURE
    assert payload["graphSha256"].startswith("sha256:")

    legacy = sdai_main(
        [
            "feature",
            "LEGACY-101",
            "--title",
            "Legacy intake",
            "--description",
            "Existing feature command remains intact",
            "--path",
            str(project),
        ]
    )
    assert legacy == 0
    assert (project / "specs" / "LEGACY-101" / "00-intake.md").is_file()
