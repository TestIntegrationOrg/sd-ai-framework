from __future__ import annotations

from pathlib import Path
import subprocess

import yaml

from sdai.pr_traceability import PR_EVIDENCE_API_VERSION, resolve_pull_request_evidence


def _git(path: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=path,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip()


def test_unicode_repository_path_and_reversed_pr_order_are_portable(tmp_path: Path) -> None:
    repository = tmp_path / "répo-東京"
    repository.mkdir()
    _git(repository, "init", "-b", "main")
    _git(repository, "config", "user.email", "sdai-tests@example.invalid")
    _git(repository, "config", "user.name", "SD-AI Tests")
    source = repository / "source.txt"
    source.write_text("portable\n", encoding="utf-8", newline="\n")
    _git(repository, "add", "source.txt")
    _git(repository, "commit", "-m", "portable base")
    head = _git(repository, "rev-parse", "HEAD")

    feature = "UTF8-PR-101"
    evidence_path = repository / "specs" / "changes" / feature / "pr-evidence.yaml"
    evidence_path.parent.mkdir(parents=True)
    evidence_path.write_text(
        yaml.safe_dump(
            {
                "apiVersion": PR_EVIDENCE_API_VERSION,
                "kind": "PullRequestEvidence",
                "featureId": feature,
                "repositoryId": "api",
                "pullRequests": [
                    {
                        "id": "z-review",
                        "headCommit": head,
                        "state": "open",
                        "links": ["task:TASK-UTF8-002"],
                        "provider": {"name": "Proveedor 東京", "reference": "z"},
                    },
                    {
                        "id": "a-review",
                        "headCommit": head,
                        "state": "merged",
                        "links": ["task:TASK-UTF8-001"],
                        "provider": {"name": "Proveedor café", "reference": "a"},
                    },
                ],
            },
            sort_keys=False,
            allow_unicode=True,
        ),
        encoding="utf-8",
        newline="\n",
    )
    _git(repository, "add", evidence_path.relative_to(repository).as_posix())
    _git(repository, "commit", "-m", "portable PR evidence")

    resolved = resolve_pull_request_evidence(repository, feature, "api")

    assert resolved is not None
    assert [item.reference.id for item in resolved.references] == ["a-review", "z-review"]
    assert all(item.commit_exists and item.commit_reachable for item in resolved.references)
    assert all(item.satisfies_traceability for item in resolved.references)
    assert resolved.manifest.source == f"specs/changes/{feature}/pr-evidence.yaml"
