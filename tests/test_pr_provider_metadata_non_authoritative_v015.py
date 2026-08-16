from __future__ import annotations

from sdai.pr_traceability import (
    PullRequestEvidenceManifest,
    PullRequestProviderMetadata,
    PullRequestReference,
    PullRequestState,
)


def test_duplicate_provider_display_metadata_does_not_conflict_with_local_pr_identity() -> None:
    provider = PullRequestProviderMetadata(
        name="github",
        reference="17",
        url="https://example.invalid/pulls/17",
    )
    first = PullRequestReference(
        id="local-a",
        head_commit="1" * 40,
        state=PullRequestState.OPEN,
        links=("task:TASK-A",),
        provider=provider,
    )
    second = PullRequestReference(
        id="local-b",
        head_commit="2" * 40,
        state=PullRequestState.MERGED,
        links=("task:TASK-B",),
        provider=provider,
    )

    manifest = PullRequestEvidenceManifest(
        feature_id="PROVIDER-101",
        repository_id="api",
        pull_requests=(second, first),
        source="specs/changes/PROVIDER-101/pr-evidence.yaml",
        source_sha256="sha256:" + ("3" * 64),
    )

    assert [item.id for item in manifest.pull_requests] == ["local-a", "local-b"]
    assert manifest.pull_requests[0].provider == manifest.pull_requests[1].provider
    assert manifest.sha256.startswith("sha256:")
