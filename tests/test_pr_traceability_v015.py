from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest
import yaml

from sdai.multi_repo_pr_graph import build_multi_repo_feature_graph
from sdai.multi_repo_run import MultiRepoExitClass
from sdai.multi_repo_verify import verify_all_repositories
from sdai.pr_traceability import (
    PR_EVIDENCE_API_VERSION,
    PullRequestEvidenceError,
    load_pull_request_evidence,
    resolve_pull_request_evidence,
)
from sdai.verification import VerificationOutcome


FEATURE = "PRTRACE-101"


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


def _clone(value: object) -> object:
    return json.loads(json.dumps(value, ensure_ascii=False))


def _repository(tmp_path: Path) -> tuple[Path, str]:
    repository = tmp_path / "api"
    repository.mkdir()
    _git(repository, "init", "-b", "main")
    _git(repository, "config", "user.email", "sdai-tests@example.invalid")
    _git(repository, "config", "user.name", "SD-AI Tests")
    _write(repository / ".sdai" / "config.yaml", "{}\n")
    feature = repository / "specs" / "changes" / FEATURE
    _write(
        feature / "requirements.md",
        "# Requirements\n\n- FR-API-001: API behavior is implemented.\n",
    )
    _write(
        feature / "tasks.md",
        "# Tasks\n\n- [ ] TASK-API-001: Implement FR-API-001.\n",
    )
    _write(
        repository / "src" / "api.py",
        "# Explicit trace links: FR-API-001 TASK-API-001\n\ndef run() -> None:\n    pass\n",
    )
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "feature trace")
    return repository, _git(repository, "rev-parse", "HEAD")


def _project(tmp_path: Path, repository: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    _write(project / ".sdai" / "config.yaml", "{}\n")
    payload = {
        "apiVersion": "sdai.feature-repositories/v1",
        "kind": "FeatureRepositories",
        "repositories": [
            {
                "id": "api",
                "path": str(repository.resolve()),
                "capabilities": ["requirements", "tasks"],
                "ownership": [
                    {"type": "requirement", "pattern": "FR-API-*"},
                    {"type": "task", "pattern": "TASK-API-*"},
                ],
                "required": True,
            }
        ],
    }
    _write(
        project / ".sdai" / "feature-repositories.yaml",
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
    )
    return project


def _evidence_payload(
    head_commit: str,
    *,
    feature_id: str = FEATURE,
    repository_id: str = "api",
    state: str = "open",
    link: str = "task:TASK-API-001",
    provider_name: str = "Git Service – Café",
) -> dict[str, object]:
    return {
        "apiVersion": PR_EVIDENCE_API_VERSION,
        "kind": "PullRequestEvidence",
        "featureId": feature_id,
        "repositoryId": repository_id,
        "pullRequests": [
            {
                "id": "review-17",
                "headCommit": head_commit,
                "state": state,
                "links": [link],
                "provider": {
                    "name": provider_name,
                    "reference": "17",
                    "url": "https://example.invalid/reviews/17",
                },
            }
        ],
    }


def _write_evidence(repository: Path, payload: dict[str, object], *, commit: bool = True) -> Path:
    path = repository / "specs" / "changes" / FEATURE / "pr-evidence.yaml"
    _write(path, yaml.safe_dump(payload, sort_keys=False, allow_unicode=True))
    if commit:
        _git(repository, "add", path.relative_to(repository).as_posix())
        _git(repository, "commit", "-m", "record PR evidence")
    return path


def _edge_set(graph) -> set[tuple[str, str, str]]:
    return {(edge.relation, edge.source, edge.target) for edge in graph.edges}


def test_valid_reachable_pr_evidence_adds_provider_neutral_node_and_trace_edge(tmp_path: Path) -> None:
    repository, implementation_commit = _repository(tmp_path)
    project = _project(tmp_path, repository)
    _write_evidence(repository, _evidence_payload(implementation_commit))

    first = build_multi_repo_feature_graph(project, FEATURE)
    second = build_multi_repo_feature_graph(project, FEATURE)

    node_id = "pr-reference:api:review-17"
    assert first.to_json() == second.to_json()
    assert any(node.node_id == node_id and node.type.value == "pr-reference" for node in first.nodes)
    assert ("included-in-pr", "task:TASK-API-001", node_id) in _edge_set(first)
    assert not any(finding.code.startswith("SDAI-FEATURE-GRAPH-PR-EVIDENCE-") for finding in first.findings)
    rendered = first.to_json()
    assert str(repository.resolve()) not in rendered
    assert "Git Service – Café" in rendered
    pr_node = next(node for node in first.nodes if node.node_id == node_id)
    payload = next(fact.payload for fact in pr_node.facts if fact.kind == "pr-evidence")
    assert payload["headCommit"] == implementation_commit
    assert payload["source"] == f"specs/changes/{FEATURE}/pr-evidence.yaml"
    assert str(payload["sourceSha256"]).startswith("sha256:")
    assert payload["satisfiesTraceability"] is True


def test_provider_metadata_changes_display_fact_not_canonical_pr_identity(tmp_path: Path) -> None:
    repository, implementation_commit = _repository(tmp_path)
    project = _project(tmp_path, repository)
    path = _write_evidence(repository, _evidence_payload(implementation_commit))
    first = build_multi_repo_feature_graph(project, FEATURE)

    payload = _evidence_payload(implementation_commit, provider_name="Otro proveedor – 東京")
    _write(path, yaml.safe_dump(payload, sort_keys=False, allow_unicode=True))
    _git(repository, "add", path.relative_to(repository).as_posix())
    _git(repository, "commit", "-m", "update provider display metadata")
    second = build_multi_repo_feature_graph(project, FEATURE)

    first_ids = {node.node_id for node in first.nodes if node.type.value == "pr-reference"}
    second_ids = {node.node_id for node in second.nodes if node.type.value == "pr-reference"}
    assert first_ids == second_ids == {"pr-reference:api:review-17"}
    assert first.sha256 != second.sha256
    assert "Otro proveedor – 東京" in second.to_json()


def test_unreachable_local_commit_is_stale_and_cannot_create_pr_edge(tmp_path: Path) -> None:
    repository, _ = _repository(tmp_path)
    project = _project(tmp_path, repository)
    _git(repository, "checkout", "-b", "unrelated")
    _write(repository / "unrelated.txt", "unrelated\n")
    _git(repository, "add", "unrelated.txt")
    _git(repository, "commit", "-m", "unreachable branch commit")
    unreachable = _git(repository, "rev-parse", "HEAD")
    _git(repository, "checkout", "main")
    _write_evidence(repository, _evidence_payload(unreachable))

    graph = build_multi_repo_feature_graph(project, FEATURE)
    node_id = "pr-reference:api:review-17"

    assert any(node.node_id == node_id for node in graph.nodes)
    assert ("included-in-pr", "task:TASK-API-001", node_id) not in _edge_set(graph)
    assert any(
        finding.code == "SDAI-FEATURE-GRAPH-PR-EVIDENCE-STALE"
        and finding.subject == node_id
        for finding in graph.findings
    )


def test_cross_feature_evidence_is_rejected_without_pr_node(tmp_path: Path) -> None:
    repository, implementation_commit = _repository(tmp_path)
    project = _project(tmp_path, repository)
    _write_evidence(
        repository,
        _evidence_payload(implementation_commit, feature_id="OTHER-101"),
    )

    graph = build_multi_repo_feature_graph(project, FEATURE)

    assert not any(node.type.value == "pr-reference" for node in graph.nodes)
    assert any(
        finding.code == "SDAI-FEATURE-GRAPH-PR-EVIDENCE-CROSS-FEATURE"
        for finding in graph.findings
    )


def test_stale_trace_link_cannot_satisfy_pr_traceability(tmp_path: Path) -> None:
    repository, implementation_commit = _repository(tmp_path)
    project = _project(tmp_path, repository)
    _write_evidence(
        repository,
        _evidence_payload(implementation_commit, link="task:TASK-API-DOES-NOT-EXIST"),
    )

    graph = build_multi_repo_feature_graph(project, FEATURE)
    node_id = "pr-reference:api:review-17"

    pr_node = next(node for node in graph.nodes if node.node_id == node_id)
    pr_fact = next(fact for fact in pr_node.facts if fact.kind == "pr-evidence")
    assert pr_fact.payload["satisfiesTraceability"] is False
    assert pr_fact.payload["linksCurrent"] is False
    assert not any(edge.target == node_id and edge.relation == "included-in-pr" for edge in graph.edges)
    assert any(
        finding.code == "SDAI-FEATURE-GRAPH-PR-EVIDENCE-STALE-LINK"
        for finding in graph.findings
    )


def test_duplicate_local_pr_identity_is_conflicting_evidence(tmp_path: Path) -> None:
    repository, implementation_commit = _repository(tmp_path)
    payload = _evidence_payload(implementation_commit)
    original = payload["pullRequests"][0]  # type: ignore[index]
    payload["pullRequests"] = [_clone(original), _clone(original)]
    _write_evidence(repository, payload, commit=False)

    with pytest.raises(PullRequestEvidenceError, match="local ids must be unique"):
        load_pull_request_evidence(repository, FEATURE, "api")


def test_resolved_pr_evidence_is_canonical_and_uses_local_git_only(tmp_path: Path) -> None:
    repository, implementation_commit = _repository(tmp_path)
    payload = _evidence_payload(implementation_commit)
    original = payload["pullRequests"][0]  # type: ignore[index]
    first = _clone(original)
    second = _clone(original)
    assert isinstance(first, dict) and isinstance(second, dict)
    first["id"] = "z-review"
    first["provider"] = {"name": "Proveedor ñ", "reference": "z"}
    second["id"] = "a-review"
    second["provider"] = {"name": "Provider α", "reference": "a"}
    payload["pullRequests"] = [first, second]
    _write_evidence(repository, payload)

    resolved = resolve_pull_request_evidence(repository, FEATURE, "api")

    assert resolved is not None
    assert [item.reference.id for item in resolved.references] == ["a-review", "z-review"]
    assert all(item.commit_exists and item.commit_reachable for item in resolved.references)
    assert resolved.sha256.startswith("sha256:")
    rendered = json.dumps(resolved.as_dict(), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    assert "Provider α" in rendered
    assert "Proveedor ñ" in rendered


def test_missing_or_stale_pr_evidence_fails_all_repo_verification_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, _ = _repository(tmp_path)
    project = _project(tmp_path, repository)

    class PassingReport:
        outcome = VerificationOutcome.PASSED

        def to_json(self) -> str:
            return json.dumps({"outcome": "passed"}, sort_keys=True, separators=(",", ":"))

    def pass_verify(*args: object, **kwargs: object) -> PassingReport:
        return PassingReport()

    monkeypatch.setattr("sdai.multi_repo_verify.verify_feature", pass_verify)
    report = verify_all_repositories(project, FEATURE, risk="standard")

    assert report.exit_class is MultiRepoExitClass.POLICY_FAILURE
    assert any(
        finding["code"] == "SDAI-FEATURE-GRAPH-PR-EVIDENCE-MISSING"
        for finding in report.graph_findings
    )
