from __future__ import annotations

from sdai.completion_policy import SPEC_REVIEW_CONTRACT
from sdai.completion_policy_layers import resolve_layered_completion_policy


def test_organization_requirements_cannot_be_removed_by_repo_or_user_layers() -> None:
    result = resolve_layered_completion_policy(
        "standard",
        "task",
        organization_required=("company/release-board/v1",),
        repository_required=("team/performance/v1",),
        user_required=("developer/local-check/v1",),
    )

    assert "company/release-board/v1" in result.required_contracts
    assert "team/performance/v1" in result.required_contracts
    assert "developer/local-check/v1" in result.required_contracts
    assert SPEC_REVIEW_CONTRACT in result.required_contracts
    assert [item.layer.value for item in result.contributions] == [
        "builtin",
        "org",
        "repo",
        "user",
    ]
