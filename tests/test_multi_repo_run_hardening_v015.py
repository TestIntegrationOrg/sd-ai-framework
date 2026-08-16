from __future__ import annotations

import pytest

from sdai.multi_repo_run import (
    MultiRepoExitClass,
    MultiRepoRunError,
    MultiRepoRunPlan,
    execute_multi_repo_run,
)


_SHA = "sha256:" + ("1" * 64)


def _plan() -> MultiRepoRunPlan:
    return MultiRepoRunPlan(
        feature_id="RACE-101",
        graph_sha256=_SHA,
        repository_resolution_sha256=_SHA,
        store_resolution_sha256=None,
        workflow="standard",
        isolation="worktree",
        participants=(),
        blockers=(),
    )


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            MultiRepoRunError("SDAI-MULTI-RUN-DRIFT: repository changed after planning"),
            MultiRepoExitClass.DRIFT,
        ),
        (
            MultiRepoRunError("SDAI-MULTI-RUN-PARTICIPANT: repository became dirty"),
            MultiRepoExitClass.PARTICIPANT_UNAVAILABLE_OR_DIRTY,
        ),
    ],
)
def test_execute_maps_pre_mutation_revalidation_failure_to_stable_exit(
    monkeypatch: pytest.MonkeyPatch,
    error: MultiRepoRunError,
    expected: MultiRepoExitClass,
) -> None:
    calls: list[str] = []

    def fail_revalidation(plan: MultiRepoRunPlan) -> None:
        raise error

    def runner(*args: object) -> int:
        calls.append("mutated")
        return 0

    monkeypatch.setattr("sdai.multi_repo_run.revalidate_run_plan", fail_revalidation)

    result = execute_multi_repo_run(_plan(), runner)

    assert result.exit_class is expected
    assert result.results == ()
    assert calls == []
