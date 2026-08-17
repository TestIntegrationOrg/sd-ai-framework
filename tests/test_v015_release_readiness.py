from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest
import yaml

from sdai.multi_repo_pr_graph import build_multi_repo_feature_graph
from sdai.multi_repo_run import (
    MultiRepoExitClass,
    build_multi_repo_run_plan,
    execute_multi_repo_run,
)
from sdai.multi_repo_verify import verify_all_repositories
from sdai.specification_store_lifecycle import (
    StoreAutomationExit,
    create_store,
    doctor_stores,
    export_store_context,
    list_stores,
    register_store,
)
from sdai.specification_store_references import resolve_specification_store_references
from sdai.verification import VerificationOutcome


FEATURE = "RELEASE-015"


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


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink() and ".git" not in path.parts
    }


def _store(path: Path) -> Path:
    create_store(path, "release-specs", "0.15.0", description="SD-AI 0.15 release specifications")
    feature = path / "specs" / "changes" / FEATURE
    _write(
        feature / "change.yaml",
        """version: 1
feature_id: RELEASE-015
title: SD-AI 0.15 integrated release gate
description: Prove central store and API UI shared repository orchestration
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
    for domain, requirement in (
        ("api", "FR-API-015"),
        ("shared", "FR-SHARED-015"),
        ("ui", "FR-UI-015"),
    ):
        _write(
            feature / "deltas" / f"{domain}.yaml",
            f"""version: 1
domain: {domain}
baseline_spec_sha256: null
operations:
  - op: ADDED
    requirement_id: {requirement}
    reason: Exercise the {domain} repository in the release gate
    definition: The {domain} repository must implement and verify its owned 0.15 behavior.
""",
        )
    return path


def _repository(path: Path, prefix: str) -> tuple[Path, str]:
    path.mkdir(parents=True)
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.email", "sdai-tests@example.invalid")
    _git(path, "config", "user.name", "SD-AI Tests")
    _write(path / ".sdai" / "config.yaml", "{}\n")
    feature = path / "specs" / "changes" / FEATURE
    requirement = f"FR-{prefix}-015"
    task = f"TASK-{prefix}-015"
    _write(feature / "requirements.md", f"# Requirements\n\n- {requirement}: {prefix} release behavior.\n")
    _write(feature / "tasks.md", f"# Tasks\n\n- [ ] {task}: Implement and test {requirement}.\n")
    _write(
        path / "src" / f"{prefix.casefold()}.py",
        f"# Explicit trace links: {requirement} {task}\n\ndef release_ready() -> bool:\n    return True\n",
    )
    _write(
        path / "tests" / f"test_{prefix.casefold()}.py",
        f"# Evidence links: {requirement} {task}\n\ndef test_release_ready():\n    assert True\n",
    )
    _git(path, "add", ".")
    _git(path, "commit", "-m", f"implement {prefix} release slice")
    implementation_commit = _git(path, "rev-parse", "HEAD")
    evidence = {
        "apiVersion": "sdai.pr-evidence/v1",
        "kind": "PullRequestEvidence",
        "featureId": FEATURE,
        "repositoryId": prefix.casefold(),
        "pullRequests": [
            {
                "id": f"release-{prefix.casefold()}-15",
                "headCommit": implementation_commit,
                "state": "open",
                "links": [f"task:{task}"],
                "provider": {
                    "name": "Provider-neutral release fixture",
                    "reference": f"{prefix}-15",
                    "url": f"https://example.invalid/{prefix.casefold()}/15",
                },
            }
        ],
    }
    _write(feature / "pr-evidence.yaml", yaml.safe_dump(evidence, sort_keys=False, allow_unicode=True))
    _git(path, "add", ".")
    _git(path, "commit", "-m", f"record {prefix} PR evidence")
    return path, implementation_commit


def _repository_declaration(repository_id: str, root: Path) -> dict[str, object]:
    prefix = repository_id.upper()
    return {
        "id": repository_id,
        "path": str(root.resolve()),
        "capabilities": ["requirements", "tasks"],
        "ownership": [
            {"type": "requirement", "pattern": f"FR-{prefix}-*"},
            {"type": "task", "pattern": f"TASK-{prefix}-*"},
        ],
        "required": True,
    }


def test_v015_integrated_central_store_multi_repo_release_journey(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "coordinator"
    project.mkdir()
    _write(project / ".sdai" / "config.yaml", "{}\n")
    store = _store(tmp_path / "central-store")
    repositories = {
        repository_id: _repository(tmp_path / repository_id, repository_id.upper())[0]
        for repository_id in ("api", "shared", "ui")
    }

    registration = register_store(project, store)
    assert registration.registered is True
    assert registration.path_scope == "external"
    listing = list_stores(project)
    context = export_store_context(project)
    doctor = doctor_stores(project)
    assert [item.identity for item in listing.stores] == ["release-specs@0.15.0"]
    assert [item.identity for item in context.stores] == ["release-specs@0.15.0"]
    assert doctor.healthy is True
    assert doctor.exit_code is StoreAutomationExit.SUCCESS

    references = resolve_specification_store_references(project)
    assert references.references[0].reference.identity == "release-specs@0.15.0"
    store_before = _snapshot(store)

    declaration = {
        "apiVersion": "sdai.feature-repositories/v1",
        "kind": "FeatureRepositories",
        "repositories": [
            _repository_declaration("ui", repositories["ui"]),
            _repository_declaration("api", repositories["api"]),
            _repository_declaration("shared", repositories["shared"]),
        ],
    }
    _write(
        project / ".sdai" / "feature-repositories.yaml",
        yaml.safe_dump(declaration, sort_keys=False, allow_unicode=True),
    )

    graph = build_multi_repo_feature_graph(project, FEATURE)
    assert not graph.has_errors
    assert graph.store_resolution_sha256 == references.sha256
    assert {
        "requirement:FR-API-015",
        "requirement:FR-SHARED-015",
        "requirement:FR-UI-015",
        "task:TASK-API-015",
        "task:TASK-SHARED-015",
        "task:TASK-UI-015",
        "pr-reference:api:release-api-15",
        "pr-reference:shared:release-shared-15",
        "pr-reference:ui:release-ui-15",
    } <= {node.node_id for node in graph.nodes}
    edges = {(edge.relation, edge.source, edge.target) for edge in graph.edges}
    for repository_id in ("api", "shared", "ui"):
        prefix = repository_id.upper()
        assert (
            "owned-by",
            f"requirement:FR-{prefix}-015",
            f"repository:{repository_id}",
        ) in edges
        assert (
            "included-in-pr",
            f"task:TASK-{prefix}-015",
            f"pr-reference:{repository_id}:release-{repository_id}-15",
        ) in edges

    plan = build_multi_repo_run_plan(project, FEATURE, isolation="in-place")
    assert plan.ready
    assert plan.graph_sha256.startswith("sha256:")
    assert plan.repository_resolution_sha256 == graph.repository_resolution_sha256
    assert plan.store_resolution_sha256 == graph.store_resolution_sha256
    assert [item.repository_id for item in plan.participants] == ["api", "shared", "ui"]
    calls: list[str] = []

    def runner(participant, workflow: str, isolation: str) -> int:
        calls.append(participant.repository_id)
        assert workflow == "standard"
        assert isolation == "in-place"
        return 0

    execution = execute_multi_repo_run(plan, runner)
    assert execution.exit_class is MultiRepoExitClass.SUCCESS
    assert calls == ["api", "shared", "ui"]

    class PassingReport:
        outcome = VerificationOutcome.PASSED

        def to_json(self) -> str:
            return json.dumps({"outcome": "passed"}, sort_keys=True, separators=(",", ":"))

    def pass_verify(*args: object, **kwargs: object) -> PassingReport:
        return PassingReport()

    monkeypatch.setattr("sdai.multi_repo_verify.verify_feature", pass_verify)
    verification = verify_all_repositories(project, FEATURE, risk="critical")
    assert verification.exit_class is MultiRepoExitClass.SUCCESS
    assert [item.repository_id for item in verification.repositories] == ["api", "shared", "ui"]
    assert not verification.graph_findings

    # Central specifications are read-only during graph/run/verify, and the selected
    # repositories remain clean because orchestration itself performs no hidden Git mutation.
    assert _snapshot(store) == store_before
    assert all(_git(root, "status", "--porcelain=v1") == "" for root in repositories.values())


def test_v015_release_evidence_keeps_historical_compatibility_gates_in_full_ci() -> None:
    root = Path(__file__).resolve().parents[1]
    required = {
        "tests/test_v06_release_compatibility.py",
        "tests/test_v07_release_compatibility.py",
        "tests/test_v08_release_compatibility.py",
        "tests/test_v09_release_compatibility.py",
        "tests/test_v010_release_compatibility.py",
        "tests/test_v011_release_evidence.py",
        "tests/test_pack_signed_lifecycle_gate_v012.py",
        "tests/test_integration_sdk_release_gate_v013.py",
        "tests/test_workflow_engine2_release_gate_v014.py",
        "tests/test_multi_repo_authority_hardening_v015.py",
    }
    assert all((root / path).is_file() for path in required)
    workflow = (root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "os: [ubuntu-latest, windows-latest]" in workflow
    assert 'python-version: ["3.11", "3.12"]' in workflow
    assert "pytest -q" in workflow
    assert "pytest -q tests/" not in workflow
