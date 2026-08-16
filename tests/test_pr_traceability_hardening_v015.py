from __future__ import annotations

from sdai.multi_repo_pr_graph import _pr_fact
from sdai.pr_traceability import (
    PullRequestEvidenceManifest,
    PullRequestReference,
    PullRequestState,
    ResolvedPullRequestEvidence,
    ResolvedPullRequestReference,
)


def test_reachable_pr_with_stale_trace_link_is_not_marked_satisfying() -> None:
    reference = PullRequestReference(
        id="review-17",
        head_commit="1" * 40,
        state=PullRequestState.OPEN,
        links=("task:TASK-API-001",),
    )
    manifest = PullRequestEvidenceManifest(
        feature_id="PRTRACE-101",
        repository_id="api",
        pull_requests=(reference,),
        source="specs/changes/PRTRACE-101/pr-evidence.yaml",
        source_sha256="sha256:" + ("2" * 64),
    )
    resolved_reference = ResolvedPullRequestReference(
        reference=reference,
        commit_exists=True,
        commit_reachable=True,
        resolved_commit="1" * 40,
    )
    evidence = ResolvedPullRequestEvidence(manifest, (resolved_reference,))

    fact = _pr_fact(
        "api",
        evidence,
        resolved_reference,
        links_current=False,
    )

    assert fact.payload["current"] is True
    assert fact.payload["linksCurrent"] is False
    assert fact.payload["satisfiesTraceability"] is False
