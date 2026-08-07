from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Callable


class GitHubIntegrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class GitHubIssue:
    repository: str
    number: int
    title: str
    body: str
    url: str
    labels: tuple[str, ...] = ()
    assignees: tuple[str, ...] = ()


@dataclass(frozen=True)
class PullRequestRequest:
    repository: str
    base: str
    head: str
    title: str
    body: str
    draft: bool = True


Runner = Callable[[list[str], Path | None], subprocess.CompletedProcess[str]]


def _default_runner(command: list[str], cwd: Path | None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


class GitHubCli:
    """Thin adapter around the official `gh` CLI.

    Authentication remains owned by `gh`; SD-AI never stores GitHub tokens in
    project configuration.
    """

    def __init__(self, *, cwd: Path | None = None, runner: Runner | None = None):
        self.cwd = cwd.resolve() if cwd else None
        self.runner = runner or _default_runner

    def availability(self) -> tuple[bool, str]:
        executable = shutil.which("gh")
        return (bool(executable), executable or "gh executable not found")

    def _run(self, args: list[str]) -> str:
        completed = self.runner(["gh", *args], self.cwd)
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "unknown gh error").strip()
            raise GitHubIntegrationError(detail)
        return completed.stdout or ""

    def issue(self, repository: str, number: int) -> GitHubIssue:
        output = self._run(
            [
                "issue",
                "view",
                str(number),
                "--repo",
                repository,
                "--json",
                "number,title,body,url,labels,assignees",
            ]
        )
        try:
            data = json.loads(output)
        except json.JSONDecodeError as exc:
            raise GitHubIntegrationError("gh issue view returned invalid JSON") from exc
        labels = tuple(
            str(item.get("name") or "") for item in (data.get("labels") or []) if item.get("name")
        )
        assignees = tuple(
            str(item.get("login") or "") for item in (data.get("assignees") or []) if item.get("login")
        )
        return GitHubIssue(
            repository=repository,
            number=int(data.get("number") or number),
            title=str(data.get("title") or ""),
            body=str(data.get("body") or ""),
            url=str(data.get("url") or ""),
            labels=labels,
            assignees=assignees,
        )

    def create_pull_request(self, request: PullRequestRequest) -> str:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".md", delete=False) as handle:
            handle.write(request.body)
            body_path = Path(handle.name)
        try:
            args = [
                "pr",
                "create",
                "--repo",
                request.repository,
                "--base",
                request.base,
                "--head",
                request.head,
                "--title",
                request.title,
                "--body-file",
                str(body_path),
            ]
            if request.draft:
                args.append("--draft")
            return self._run(args).strip()
        finally:
            body_path.unlink(missing_ok=True)


def github_issue_intake(issue: GitHubIssue, feature_id: str, workflow: str) -> str:
    labels = ", ".join(issue.labels) or "-"
    assignees = ", ".join(issue.assignees) or "-"
    return f"""# Feature Intake — {feature_id}

## Title
{issue.title}

## Description
{issue.body or '(no issue body)'}

## Requested Lifecycle
{workflow}

## Source
github-issue

## Source Reference
- Repository: {issue.repository}
- Issue: #{issue.number}
- URL: {issue.url}
- Labels: {labels}
- Assignees: {assignees}

## Status
intake
"""


def build_pull_request_body(feature_dir: Path, feature_id: str) -> str:
    sections = [
        f"## SD-AI Feature\n\n`{feature_id}`",
        "## Source of truth\n\nThis pull request was prepared from version-controlled SD-AI specification and architecture artifacts.",
    ]
    for relative, heading in (
        ("specification.md", "Specification"),
        ("architecture/architecture.md", "Architecture"),
        ("plan.md", "Implementation plan"),
    ):
        path = feature_dir / relative
        if path.exists():
            text = path.read_text(encoding="utf-8")
            # Keep generated PR bodies bounded; full artifacts remain in the repository.
            if len(text) > 6000:
                text = text[:6000] + "\n\n[truncated; see repository artifact]"
            sections.append(f"## {heading}\n\n{text}")
    sections.append(
        "## SD-AI traceability\n\nReview the specification, ADRs, tests, security findings, and quality-gate artifacts before merge."
    )
    return "\n\n".join(sections) + "\n"
