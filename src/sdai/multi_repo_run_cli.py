from __future__ import annotations

import argparse
from pathlib import Path
import sys

from sdai.cli import _print_step_execution, _workflow_exit_code
from sdai.multi_repo_run import (
    MultiRepoRunError,
    PlannedRepositoryRun,
    build_multi_repo_run_plan,
    execute_multi_repo_run,
)
from sdai.orchestrator import Orchestrator
from sdai.worktree_isolation import create_worktree_session


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sdai run",
        description="Plan and execute a feature explicitly in selected local repositories",
    )
    parser.add_argument("feature")
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--repo", help="Run only one explicit feature repository id")
    selection.add_argument("--all", action="store_true", help="Run all feature participants in deterministic order")
    parser.add_argument("--workflow", default="standard")
    parser.add_argument("--isolation", choices=["in-place", "worktree"], default="worktree")
    parser.add_argument("--cleanup-worktree", action="store_true")
    parser.add_argument("--plan", action="store_true", help="Print the bound plan without executing")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--path", help="Central SD-AI project root containing feature-repositories.yaml")
    return parser


def _human_plan(plan) -> None:
    print(
        f"Multi-repo run plan feature={plan.feature_id} ready={str(plan.ready).lower()} "
        f"repositories={len(plan.participants)} sha256={plan.sha256}"
    )
    print(f"  graph={plan.graph_sha256}")
    for blocker in plan.blockers:
        print(f"  BLOCK {blocker}")
    for item in plan.participants:
        baseline = item.baseline or {}
        commit = baseline.get("commit", "-")
        branch = baseline.get("branch", "-")
        print(
            f"  {item.repository_id:20} status={item.status.value:12} "
            f"branch={branch} commit={commit} plan={item.branch_policy}"
        )
        if item.reason:
            print(f"    {item.reason}")


def _repository_runner(feature: str, cleanup_worktree: bool):
    def run(participant: PlannedRepositoryRun, workflow: str, isolation: str) -> int:
        root = participant.root
        if root is None:
            raise RuntimeError(f"repository '{participant.repository_id}' has no resolved local root")
        if isolation == "in-place":
            results = Orchestrator(root).run_workflow(feature, workflow)
            for execution in results:
                _print_step_execution(execution, root, indent=f"[{participant.repository_id}] ")
            return _workflow_exit_code(results)

        session = create_worktree_session(root, feature)
        try:
            results = Orchestrator(session.worktree_path).run_workflow(feature, workflow)
            for execution in results:
                _print_step_execution(
                    execution,
                    session.worktree_path,
                    indent=f"[{participant.repository_id}] ",
                )
            code = _workflow_exit_code(results)
            outcome = "failed" if code == 2 else "paused" if code == 3 else "success"
            session.finalize(outcome, cleanup_requested=cleanup_worktree)
            return code
        except KeyboardInterrupt:
            session.finalize("cancelled", error="execution cancelled")
            raise
        except Exception as exc:
            session.finalize("failed", error=str(exc))
            raise

    return run


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root = Path(args.path or ".").resolve()
    selected = (args.repo,) if args.repo else None
    try:
        plan = build_multi_repo_run_plan(
            root,
            args.feature,
            repository_ids=selected,
            workflow=args.workflow,
            isolation=args.isolation,
        )
    except MultiRepoRunError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 6

    if args.plan:
        if args.json:
            print(plan.to_json())
        else:
            _human_plan(plan)
        return int(plan.exit_class)

    # Print/emit the immutable plan before invoking anything that can mutate a
    # repository. This is also the plan hash bound to the execution result.
    if args.json:
        print(plan.to_json())
    else:
        _human_plan(plan)
    if not plan.ready:
        return int(plan.exit_class)

    result = execute_multi_repo_run(
        plan,
        _repository_runner(args.feature, args.cleanup_worktree),
    )
    if args.json:
        print(result.to_json())
    else:
        for item in result.results:
            print(
                f"  RESULT {item.repository_id:20} status={item.status} exit={item.exit_code}"
            )
        print(f"Multi-repo result exit-class={result.exit_class.name.lower().replace('_', '-')} sha256={plan.sha256}")
    return int(result.exit_class)


if __name__ == "__main__":
    raise SystemExit(main())
